from django.views.generic import TemplateView, ListView, DetailView, CreateView, UpdateView, DeleteView
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q, Count
from django.utils import timezone
from django.core.serializers.json import DjangoJSONEncoder
import io
import json
import uuid
import pandas as pd
from django.core.cache import cache
from django.conf import settings

import logging

from apps.dashboards.models import ( DataTable,
    Record, Dashboard, Widget, AuditLog, CalculatedField, DataSource
)
from apps.dashboards.services import DataImportService, WorkspaceInsightService
from apps.workspaces.models import Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """Main dashboard landing page"""
    template_name = 'dashboard/index.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        workspace = getattr(self.request.user, 'current_workspace', None)

        # If no workspace in request, try to get from session
        if not workspace:
            workspace_id = self.request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(
                        id=workspace_id,
                        members=self.request.user
                    )
                    self.request.user.current_workspace = workspace
                except Workspace.DoesNotExist:
                    logger.warning(f"Workspace {workspace_id} not found in session")

        # If still no workspace, get first available
        if not workspace:
            workspace = Workspace.objects.filter(members=self.request.user).first()
            if workspace:
                self.request.session['current_workspace_id'] = str(workspace.id)
                self.request.session.save()
                self.request.user.current_workspace = workspace
        
        context['workspace'] = workspace
        
        if workspace:
            # Get recent tables
            context['recent_tables'] = DataTable.objects.filter(
                workspace=workspace,
                is_active=True
            ).order_by('-updated_at')[:5]
            
            # Get workspace overview dashboard if it exists
            context['insights_dashboard'] = Dashboard.objects.filter(
                workspace=workspace,
                slug='workspace-overview',
                is_active=True
            ).first()

            # Get recent dashboards
            context['recent_dashboards'] = Dashboard.objects.filter(
                workspace=workspace,
                is_active=True
            ).order_by('-updated_at')[:5]
            
            # Get usage statistics
            context['stats'] = workspace.get_usage_stats()

            # Get recent activity
            context['recent_activity'] = AuditLog.objects.filter(
                workspace=workspace
            ).select_related('user').order_by('-timestamp')[:10]
        
        return context
    
class WorkspaceListView(LoginRequiredMixin, ListView):
    """List all workspaces for the user"""
    model = Workspace
    template_name = 'dashboard/workspaces.html'
    context_object_name = 'workspaces'
    
    def get_queryset(self):
        return Workspace.objects.filter(members=self.request.user)


class WorkspaceCreateView(LoginRequiredMixin, CreateView):
    """Create a new workspace"""
    model = Workspace
    template_name = 'dashboard/workspace_form.html'
    fields = ['name']
    success_url = reverse_lazy('dashboard:home')
    
    def form_valid(self, form):
        workspace = form.save(commit=False)
        workspace.owner = self.request.user
        workspace.save()

        # Add owner as member
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=self.request.user,
            role='owner'
        )

        # Set as current workspace
        self.request.session['current_workspace_id'] = str(workspace.id)
        self.request.session.save()

        # Send to onboarding wizard instead of bare home
        return redirect('workspaces:onboarding', workspace_id=workspace.id)

@require_POST
def switch_workspace(request, pk):
    """Switch current workspace"""
    workspace = get_object_or_404(Workspace, id=pk, members=request.user)
    request.session['current_workspace_id'] = str(workspace.id)
    messages.success(request, f'Switched to workspace: {workspace.name}')
    return redirect(request.META.get('HTTP_REFERER', 'dashboard:home'))


class TableView(LoginRequiredMixin, ListView):
    """List all tables in current workspace"""
    model = DataTable
    template_name = 'dashboard/tables.html'
    context_object_name = 'tables'
    paginate_by = 20
    
    def get_queryset(self):
        workspace = self.request.user.current_workspace
        if not workspace:
            return DataTable.objects.none()
        
        queryset = DataTable.objects.filter(
            workspace=workspace,
            is_active=True
        ).select_related('created_by').order_by('-updated_at')
        
        # Search
        search_query = self.request.GET.get('search', '')
        if search_query:
            queryset = queryset.filter(
                Q(name__icontains=search_query) |
                Q(description__icontains=search_query)
            )
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_query'] = self.request.GET.get('search', '')
        return context


class TableCreateView(LoginRequiredMixin, CreateView):
    """Create a new data table"""
    model = DataTable
    template_name = 'dashboard/table_form.html'
    fields = ['name', 'description', 'schema']

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['schema'].required = False
        return form

    def get_success_url(self):
        return reverse('dashboard:table_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        workspace = self.request.user.current_workspace

        if not workspace.can_add_table():
            messages.error(self.request, 'Table limit reached.')
            return redirect('dashboard:tables')
        
        # Save the table
        form.instance.workspace = workspace
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        
        # AUTO-GENERATE DASHBOARD! 🎉
        dashboard = self.object.generate_default_dashboard()
        
        messages.success(
            self.request, 
            f'Table "{self.object.name}" created! '
            f'<a href="{reverse("dashboard:dashboard_detail", kwargs={"pk": dashboard.pk})}" '
            f'class="underline font-medium">View auto-generated dashboard</a>'
        )
        
        return response


class TableDetailView(LoginRequiredMixin, DetailView):
    """View table details and data"""
    model = DataTable
    template_name = 'dashboard/table_detail.html'
    context_object_name = 'table'

    def get_queryset(self):
        workspace = getattr(self.request.user, 'current_workspace', None)
        if not workspace:
            workspace_id = self.request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, members=self.request.user)
                    self.request.user.current_workspace = workspace
                except Workspace.DoesNotExist:
                    pass
        if not workspace:
            workspace = Workspace.objects.filter(members=self.request.user).first()
        if not workspace:
            return DataTable.objects.none()
        return DataTable.objects.filter(workspace=workspace, is_active=True)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        records = self.object.records.filter(is_active=True).order_by('-created_at')

        from django.core.paginator import Paginator
        paginator = Paginator(records, 100)
        page_number = self.request.GET.get('page', 1)
        page = paginator.get_page(page_number)
        context['records'] = page
        context['total_records'] = paginator.count

        # Full record list for client-side search (id + data)
        context['records_json'] = json.dumps(
            [{'id': str(r.id), 'data': r.data} for r in page],
            cls=DjangoJSONEncoder,
        )

        # Calculated fields — inline tab context
        cf_qs = CalculatedField.objects.filter(table=self.object).order_by('name')
        context['calculated_fields'] = cf_qs
        context['cf_count'] = cf_qs.count()

        # Column names for expression quick-insert chips
        columns = [f['name'] for f in (self.object.schema or [])]
        if not columns:
            sample = self.object.records.filter(is_active=True).values_list('data', flat=True).first()
            if sample:
                columns = list(sample.keys())
        context['columns'] = columns

        context['cf_api_base']    = f'/api/v1/tables/{self.object.id}/calculated-fields/'
        context['cf_validate_url'] = f'/api/v1/tables/{self.object.id}/calculated-fields/validate/'
        context['format_choices'] = CalculatedField.FORMAT_CHOICES
        context['fn_list']  = ['sum', 'count', 'avg', 'min', 'max']
        context['op_list']  = ['+', '-', '*', '/', '**']

        return context


class TableEditView(LoginRequiredMixin, UpdateView):
    """Edit table schema"""
    model = DataTable
    template_name = 'dashboard/table_form.html'
    fields = ['name', 'description', 'schema']
    
    def get_queryset(self):
        workspace = self.request.user.current_workspace
        return DataTable.objects.filter(workspace=workspace, is_active=True)
    
    def get_success_url(self):
        return reverse('dashboard:table_detail', kwargs={'pk': self.object.pk})
    
    def form_valid(self, form):
        messages.success(self.request, f'Table "{form.instance.name}" updated successfully!')
        return super().form_valid(form)


class TableDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a table (soft delete)"""
    model = DataTable
    template_name = 'dashboard/table_confirm_delete.html'
    success_url = reverse_lazy('dashboard:tables')

    def get_queryset(self):
        workspace = self.request.user.current_workspace
        return DataTable.objects.filter(workspace=workspace, is_active=True)

    def form_valid(self, form):
        table = self.get_object()
        now = timezone.now()

        # Snapshot which dashboards will be affected before we delete anything
        affected_dashboard_ids = list(
            Widget.objects.filter(table=table)
            .values_list('dashboard_id', flat=True)
            .distinct()
        )

        # Hard-delete every widget belonging to this table.
        # Suppress the post_delete signal so it doesn't race with our own cleanup below.
        from apps.dashboards.models import _skip_dashboard_cleanup
        _skip_dashboard_cleanup.active = True
        try:
            Widget.objects.filter(table=table).delete()
        finally:
            _skip_dashboard_cleanup.active = False

        # Soft-delete each now-empty dashboard (consistent with DashboardDeleteView).
        # Only auto-generated dashboards are touched; manually-created ones are preserved.
        deleted_dash_count = 0
        for dash_id in affected_dashboard_ids:
            if not Widget.objects.filter(dashboard_id=dash_id).exists():
                updated = Dashboard.objects.filter(
                    pk=dash_id, is_active=True,
                ).update(is_active=False, deleted_at=now)
                deleted_dash_count += updated

        # Soft-delete the table itself
        table.is_active = False
        table.deleted_at = now
        table.save(update_fields=['is_active', 'deleted_at'])

        msg = f'Table "{table.name}" deleted.'
        if deleted_dash_count:
            msg += f' {deleted_dash_count} dashboard(s) with no remaining widgets were also removed.'
        messages.success(self.request, msg)
        return redirect(self.success_url)


class TableImportMixin:
    """Shared multi-step file-import logic for ``TableImportView`` and
    ``TableCreateFromImportView``.

    The import flow has two POST steps:

    1. **upload** — user submits a CSV/XLSX file.  The mixin parses it with
       ``DataImportService``, stores the cleaned ``DataFrame`` in the cache
       under a random ``import_id`` UUID, and renders the preview/mapping
       template with column name → field type suggestions.

    2. **map** — user confirms or adjusts the column→field mapping and
       submits a second form.  The mixin retrieves the cached ``DataFrame``,
       runs ``DataImportService.import_data()``, and returns
       ``(table, result_dict)`` so the calling view can redirect with a
       success/warning message.

    Cache TTL for the staged ``DataFrame`` is 3600 seconds (1 hour).
    """
    """Shared logic for multi-step file imports"""

    def handle_upload_step(self, request, table=None):
        service = DataImportService()
        if 'file' not in request.FILES:
            messages.error(request, 'No file uploaded.')
            return None

        file_obj = request.FILES['file']
        try:
            df = service.parse_file(file_obj)
            df, cleaning_report = service.clean_dataframe(df)
            import_id = str(uuid.uuid4())
            # Use 'split' orientation for better schema preservation
            serialized_df = df.to_json(orient='split')
            cache.set(f'import_df_{import_id}', serialized_df, 3600)
            suggested_schema = service._detect_schema_from_df(df)

            return render(request, 'dashboard/table_import_preview.html', {
                'table': table,
                'import_id': import_id,
                'filename': file_obj.name,
                'columns': df.columns.tolist(),
                'preview_data': df.head(5).to_dict('records'),
                'suggested_schema': suggested_schema,
                'field_types': DataTable.FIELD_TYPES,
                'is_new_table': table is None,
                'cleaning_report': cleaning_report,
            })
        except Exception as e:
            messages.error(request, f'Error parsing file: {str(e)}')
            return None

    def handle_map_step(self, request, table=None):
        import_id = request.POST.get('import_id')
        if not import_id:
            messages.error(request, 'Invalid import request.')
            return None

        serialized_df = cache.get(f'import_df_{import_id}')
        if not serialized_df:
            messages.error(request, 'Import session expired. Please upload the file again.')
            return None

        df = pd.read_json(io.StringIO(serialized_df), orient='split', convert_dates=False)
        service = DataImportService()

        # If no table, create a new one first
        if table is None:
            workspace = request.user.current_workspace
            if not workspace.can_add_table():
                messages.error(request, 'Table limit reached.')
                return None

            table_name = request.POST.get('table_name', 'Imported Table')
            table = DataTable.objects.create(
                workspace=workspace,
                name=table_name,
                created_by=request.user
            )

        # 1. Prepare mapping and update table schema if it was empty
        mapping = {}
        if not table.schema:
            new_schema = []
            for col in df.columns:
                field_type = request.POST.get(f'type_{col}', 'text')
                new_schema.append({
                    'name': col,
                    'type': field_type,
                    'required': False
                })
                mapping[col] = col
            table.schema = new_schema
            table.save()
            # Generate default dashboard for new tables (non-fatal if it fails)
            try:
                table.generate_default_dashboard()
            except Exception:
                pass
        else:
            for field in table.schema:
                field_name = field['name']
                source_col = request.POST.get(f'map_{field_name}')
                if source_col:
                    mapping[field_name] = source_col

        # 2. Perform import
        result = service.import_data(
            table=table,
            df=df,
            user=request.user,
            mapping=mapping
        )

        cache.delete(f'import_df_{import_id}')
        return table, result


class TableImportView(LoginRequiredMixin, TableImportMixin, TemplateView):
    """Import data into an existing table"""
    template_name = 'dashboard/table_import.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        table_id = self.kwargs.get('pk')
        context['table'] = get_object_or_404(
            DataTable, 
            id=table_id,
            workspace=self.request.user.current_workspace,
            is_active=True
        )
        return context
    
    def post(self, request, *args, **kwargs):
        table = get_object_or_404(
            DataTable, 
            id=kwargs.get('pk'),
            workspace=request.user.current_workspace,
            is_active=True
        )
        
        step = request.POST.get('step', 'upload')
        if step == 'upload':
            response = self.handle_upload_step(request, table)
            return response or redirect('dashboard:table_import', pk=table.id)
        elif step == 'map':
            result_data = self.handle_map_step(request, table)
            if not result_data:
                return redirect('dashboard:table_import', pk=table.id)

            table, result = result_data
            if result['errors'] == 0:
                messages.success(request, f'Successfully imported {result["success"]} records!')
            else:
                messages.warning(request, f'Imported {result["success"]} records with {result["errors"]} errors.')
                for detail in result.get('error_details', [])[:3]:
                    messages.error(request, detail)
            return redirect('dashboard:table_detail', pk=table.id)

        return redirect('dashboard:table_import', pk=table.id)


class DashboardListView(LoginRequiredMixin, ListView):
    """All dashboards in the current workspace."""
    template_name = 'dashboard/dashboards_list.html'
    context_object_name = 'dashboards'

    def get_queryset(self):
        workspace = self.request.user.current_workspace
        if not workspace:
            return Dashboard.objects.none()
        return (
            Dashboard.objects
            .filter(workspace=workspace, is_active=True)
            .prefetch_related('widgets')
            .order_by('-updated_at')
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        workspace = self.request.user.current_workspace
        ctx['workspace'] = workspace
        if workspace:
            ctx['has_tables_with_data'] = (
                DataTable.objects.filter(workspace=workspace, is_active=True, record_count__gt=0).exists()
            )
            ctx['overview_dashboard'] = Dashboard.objects.filter(
                workspace=workspace,
                slug='workspace-overview',
                is_active=True,
            ).first()
        return ctx


class GenerateWorkspaceInsightsView(LoginRequiredMixin, View):
    """Async-first insight generation.

    POST: Dispatches the Celery task and returns JSON ``{status, workspace_id}``.
    The client subscribes to the workspace WebSocket channel to receive
    ``generation_progress`` / ``generation_complete`` events in real time.
    Falls back to a traditional redirect for non-AJAX callers.
    """

    def post(self, request, *args, **kwargs):
        from apps.insights.tasks import analyze_workspace_tables, _get_gen_state, _set_gen_state
        is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '')
        )
        workspace = request.user.current_workspace
        if not workspace:
            if is_ajax:
                return JsonResponse({'error': 'No active workspace'}, status=400)
            messages.error(request, 'No active workspace found.')
            return redirect('dashboard:home')

        ws_id = str(workspace.id)
        current = _get_gen_state(ws_id)
        if current.get('status') == 'running':
            if is_ajax:
                return JsonResponse({'status': 'already_running', **current})
            messages.info(request, 'Insight generation is already running — check the progress banner.')
            return redirect('dashboard:dashboards')

        task_id = f'gen:{ws_id}'
        _set_gen_state(ws_id, status='queued', task_id=task_id, progress=1, message='Queued…')
        try:
            analyze_workspace_tables.apply_async(args=[ws_id], task_id=task_id)
            logger.info('analyze_workspace_tables dispatched task_id=%s workspace=%s', task_id, ws_id)
        except Exception as exc:
            _set_gen_state(ws_id, status='failed', error=str(exc))
            logger.warning('Could not dispatch analyze_workspace_tables: %s', exc)
            if is_ajax:
                return JsonResponse({'error': str(exc)}, status=500)
            messages.error(request, 'Could not start generation. Please try again.')
            return redirect('dashboard:dashboards')

        if is_ajax:
            return JsonResponse({'status': 'queued', 'workspace_id': ws_id, 'task_id': task_id})
        messages.info(request, 'Generating insights… watch the progress banner for updates.')
        return redirect('dashboard:dashboards')


class GenerationStatusView(LoginRequiredMixin, View):
    """GET the current generation state for the user's active workspace.

    Used by the dashboards list page to show / restore the progress banner
    on page load (in case the user navigated away during generation).
    """

    def get(self, request, *args, **kwargs):
        from apps.insights.tasks import _get_gen_state
        workspace = request.user.current_workspace
        if not workspace:
            return JsonResponse({'status': 'idle', 'error': 'No active workspace'})
        state = _get_gen_state(str(workspace.id))
        return JsonResponse(state)


class SSOSettingsView(LoginRequiredMixin, TemplateView):
    """
    GET  /dashboard/sso/   — Show SSO configuration form
    POST /dashboard/sso/   — Save / update SSO configuration

    Only workspace owners and admins may access this page.
    """
    template_name = 'dashboard/sso_settings.html'

    def _require_admin(self, request):
        ws = request.user.current_workspace
        if not ws:
            return None, redirect('dashboard:home')
        try:
            m = WorkspaceMembership.objects.get(workspace=ws, user=request.user)
        except WorkspaceMembership.DoesNotExist:
            return None, redirect('dashboard:home')
        if m.role not in ('owner', 'admin'):
            messages.error(request, 'Only workspace admins can manage SSO settings.')
            return None, redirect('dashboard:settings')
        return ws, None

    def get_context_data(self, **kwargs):
        from apps.workspaces.models import SSOConfiguration
        context    = super().get_context_data(**kwargs)
        workspace  = self.request.user.current_workspace
        base_url   = f"{self.request.scheme}://{self.request.get_host()}"
        ws_id      = str(workspace.id) if workspace else ''

        sso = None
        try:
            sso = SSOConfiguration.objects.get(workspace=workspace)
        except (SSOConfiguration.DoesNotExist, Exception):
            pass

        sp_entity_id = (sso.sp_entity_id if sso and sso.sp_entity_id
                        else f"{base_url}/sso/{ws_id}/metadata/")

        context.update({
            'sso':          sso,
            'metadata_url': f"{base_url}/sso/{ws_id}/metadata/",
            'acs_url':      f"{base_url}/sso/{ws_id}/acs/",
            'sp_entity_id': sp_entity_id,
            'sso_login_url': f"{base_url}/sso/{ws_id}/login/",
        })
        return context

    def get(self, request, *args, **kwargs):
        ws, err = self._require_admin(request)
        if err:
            return err
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from apps.workspaces.models import SSOConfiguration
        ws, err = self._require_admin(request)
        if err:
            return err

        d = request.POST
        defaults = {
            'idp_entity_id':       d.get('idp_entity_id', '').strip(),
            'idp_sso_url':         d.get('idp_sso_url', '').strip(),
            'idp_slo_url':         d.get('idp_slo_url', '').strip(),
            'idp_x509_cert':       d.get('idp_x509_cert', '').strip(),
            'sp_entity_id':        d.get('sp_entity_id', '').strip(),
            'attribute_email':     d.get('attribute_email', 'email').strip() or 'email',
            'attribute_first_name': d.get('attribute_first_name', 'first_name').strip(),
            'attribute_last_name':  d.get('attribute_last_name', 'last_name').strip(),
            'is_active':      bool(d.get('is_active')),
            'require_sso':    bool(d.get('require_sso')),
            'auto_provision': bool(d.get('auto_provision')),
        }

        if not defaults['idp_entity_id'] or not defaults['idp_sso_url'] or not defaults['idp_x509_cert']:
            messages.error(request, 'IdP Entity ID, SSO URL, and X.509 Certificate are required.')
            return self.get(request, *args, **kwargs)

        SSOConfiguration.objects.update_or_create(workspace=ws, defaults=defaults)
        messages.success(request, 'SSO configuration saved.')
        return redirect('dashboard:sso_settings')


class GoogleSheetsConnectView(LoginRequiredMixin, TemplateView):
    """
    Step-by-step setup page for connecting a Google Sheets data source.
    Serves the guide + connection form at /dashboard/integrations/google-sheets/.
    """
    template_name = 'dashboard/google_sheets_connect.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workspace = self.request.user.current_workspace
        context['workspace_id'] = str(workspace.id) if workspace else ''
        context['back_url']     = reverse('dashboard:settings')
        context['steps'] = [
            'Create a GCP project & enable Sheets API',
            'Create a service account & download JSON key',
            'Share your sheet with the service account email',
            'Paste credentials below and save',
        ]
        return context


class IntegrationsView(LoginRequiredMixin, TemplateView):
    """
    Integrations management page — shows all connected data sources
    (Google Sheets, PostgreSQL, MySQL) with live status indicators.
    Users can test connections, browse schema, add new sources, or delete.
    """
    template_name = 'dashboard/integrations.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workspace = getattr(self.request.user, 'current_workspace', None)
        if not workspace:
            workspace_id = self.request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, members=self.request.user)
                    self.request.user.current_workspace = workspace
                except Workspace.DoesNotExist:
                    pass
        if workspace:
            from apps.dashboards.models import ChatIntegration
            context['data_sources'] = DataSource.objects.filter(
                workspace=workspace, is_active=True
            ).order_by('connector_type', 'name')
            context['chat_integrations'] = ChatIntegration.objects.filter(workspace=workspace)
        else:
            context['data_sources'] = DataSource.objects.none()
            context['chat_integrations'] = []
        context['workspace_id'] = str(workspace.id) if workspace else ''
        return context


class ChatIntegrationSaveView(LoginRequiredMixin, View):
    """Create or update a Slack/Teams webhook integration for the current workspace."""

    def post(self, request, *args, **kwargs):
        from apps.dashboards.models import ChatIntegration
        workspace = getattr(request.user, 'current_workspace', None) or \
            Workspace.objects.filter(members=request.user).first()
        if not workspace:
            messages.error(request, "No active workspace.")
            return redirect('dashboard:integrations')

        provider     = request.POST.get('provider', '').strip()
        name         = request.POST.get('name', '').strip()
        webhook_url  = request.POST.get('webhook_url', '').strip()

        if provider not in (ChatIntegration.PROVIDER_SLACK, ChatIntegration.PROVIDER_TEAMS):
            messages.error(request, "Invalid provider.")
            return redirect('dashboard:integrations')
        if not webhook_url:
            messages.error(request, "Webhook URL is required.")
            return redirect('dashboard:integrations')
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(webhook_url)
        if _parsed.scheme not in ('http', 'https') or not _parsed.netloc:
            messages.error(request, "Webhook URL must be a valid http/https URL.")
            return redirect('dashboard:integrations')
        if not name:
            name = "Slack alerts" if provider == ChatIntegration.PROVIDER_SLACK else "Teams alerts"

        ci, _ = ChatIntegration.objects.get_or_create(
            workspace=workspace,
            provider=provider,
            defaults={'name': name},
        )
        ci.name = name
        ci.is_enabled = True
        ci.set_webhook_url(webhook_url)
        ci.save()
        messages.success(request, f"{ci.get_provider_display()} integration saved.")
        return redirect('dashboard:integrations')


class ChatIntegrationDeleteView(LoginRequiredMixin, View):
    """Delete a ChatIntegration owned by the current workspace."""

    def post(self, request, pk, *args, **kwargs):
        from apps.dashboards.models import ChatIntegration
        workspace = getattr(request.user, 'current_workspace', None) or \
            Workspace.objects.filter(members=request.user).first()
        ci = get_object_or_404(ChatIntegration, pk=pk, workspace=workspace)
        ci.delete()
        messages.success(request, "Integration removed.")
        return redirect('dashboard:integrations')


@login_required
@require_POST
def chat_integration_test(request):
    """Send a test message to a webhook URL without saving it."""
    from apps.dashboards.chat_notifications import test_webhook
    try:
        body   = json.loads(request.body)
        url    = body.get('webhook_url', '').strip()
        provider = body.get('provider', 'slack').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON.'}, status=400)

    if not url:
        return JsonResponse({'error': 'webhook_url is required.'}, status=400)

    ok = test_webhook(url, provider)
    if ok:
        return JsonResponse({'success': True, 'message': 'Test message sent successfully.'})
    return JsonResponse({'success': False, 'message': 'Webhook did not respond successfully. Check the URL and try again.'}, status=400)


class TableCreateFromImportView(LoginRequiredMixin, TableImportMixin, TemplateView):
    """Create a brand new table from an imported file"""
    template_name = 'dashboard/table_import.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_new_table'] = True
        return context

    def post(self, request, *args, **kwargs):
        step = request.POST.get('step', 'upload')
        if step == 'upload':
            response = self.handle_upload_step(request)
            return response or redirect('dashboard:table_create_import')
        elif step == 'map':
            result_data = self.handle_map_step(request)
            if not result_data:
                return redirect('dashboard:table_create_import')

            table, result = result_data
            if result['errors'] == 0:
                messages.success(request, f'Table "{table.name}" created with {result["success"]} records!')
            else:
                messages.warning(request, f'Table "{table.name}" created. Imported {result["success"]} records with {result["errors"]} errors.')
                for detail in result.get('error_details', [])[:3]:
                    messages.error(request, detail)
            return redirect('dashboard:table_detail', pk=table.id)

        return redirect('dashboard:table_create_import')


class RecordListView(LoginRequiredMixin, ListView):
    """List records in a table"""
    model = Record
    template_name = 'dashboard/record_list.html'
    context_object_name = 'records'
    paginate_by = 100
    
    def get_queryset(self):
        table_id = self.kwargs.get('table_id')
        return Record.objects.filter(
            table_id=table_id,
            is_active=True
        ).order_by('-created_at')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['table'] = get_object_or_404(
            DataTable,
            id=self.kwargs.get('table_id'),
            workspace=self.request.user.current_workspace,
            is_active=True
        )
        return context


class RecordCreateView(LoginRequiredMixin, TemplateView):
    """Render the per-field data-entry form and save a new ``Record``.

    GET renders ``dashboard/record_form.html`` with the parent table in
    context so the template can build one ``<input>`` per schema field.

    POST reads ``field_<name>`` keys from ``request.POST``, assembles the
    ``data`` dict, and calls ``Record.objects.create()``.  Validation errors
    from ``Record.save()`` (schema type checking) are caught and shown as
    Django messages rather than raising an unhandled exception.

    URL: ``/dashboard/tables/<uuid:table_id>/records/create/``
    """
    template_name = 'dashboard/record_form.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        table_id = self.kwargs.get('table_id')
        context['table'] = get_object_or_404(
            DataTable,
            id=table_id,
            workspace=self.request.user.current_workspace,
            is_active=True
        )
        return context
    
    def post(self, request, *args, **kwargs):
        table = get_object_or_404(
            DataTable,
            id=kwargs.get('table_id'),
            workspace=request.user.current_workspace,
            is_active=True
        )
        
        # Get form data
        data = {}
        for key, value in request.POST.items():
            if key.startswith('field_'):
                field_name = key.replace('field_', '')
                data[field_name] = value
        
        # Validate against table schema before persisting
        is_valid, schema_errors = table.validate_record(data)
        if not is_valid:
            for err in schema_errors:
                messages.error(request, err)
            return redirect('dashboard:record_create', table_id=table.id)

        # Create record
        try:
            Record.objects.create(table=table, data=data, created_by=request.user)
            messages.success(request, 'Record created successfully!')
            return redirect('dashboard:table_detail', pk=table.id)
        except Exception as e:
            messages.error(request, f'Error creating record: {str(e)}')
            return redirect('dashboard:record_create', table_id=table.id)


class RecordEditView(LoginRequiredMixin, TemplateView):
    """Render an edit form pre-populated with existing record data, then save changes.

    GET returns ``dashboard/record_form.html`` with ``editing=True`` so the
    template can adjust the form heading and submit label.

    POST follows the same ``field_<name>`` convention as ``RecordCreateView``:
    it rebuilds the entire ``data`` dict from POST keys, assigns it to
    ``record.data``, and calls ``record.save()``.  The record's full history
    is not retained — if audit history is needed, see ``AuditLog``.

    URL: ``/dashboard/tables/<uuid:table_id>/records/<uuid:record_id>/edit/``
    """
    template_name = 'dashboard/record_form.html'

    def get_record(self, **kwargs):
        return get_object_or_404(
            Record,
            id=kwargs.get('record_id'),
            table_id=kwargs.get('table_id'),
            is_active=True
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = self.get_record(**self.kwargs)
        context['record'] = record
        context['table'] = record.table
        context['editing'] = True
        return context

    def post(self, request, *args, **kwargs):
        record = self.get_record(**kwargs)
        data = {}
        for key, value in request.POST.items():
            if key.startswith('field_'):
                field_name = key.replace('field_', '')
                data[field_name] = value
        try:
            record.data = data
            record.updated_by = request.user
            record.save()
            messages.success(request, 'Record updated successfully!')
        except Exception as e:
            messages.error(request, f'Error updating record: {str(e)}')
        return redirect('dashboard:table_detail', pk=record.table.id)


class RecordDeleteView(LoginRequiredMixin, TemplateView):
    """Delete a record (soft delete)"""
    template_name = 'dashboard/record_confirm_delete.html'

    def get_record(self, **kwargs):
        return get_object_or_404(
            Record,
            id=kwargs.get('record_id'),
            table_id=kwargs.get('table_id'),
            is_active=True
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = self.get_record(**self.kwargs)
        context['record'] = record
        context['object'] = record
        context['table'] = record.table
        return context

    def post(self, request, *args, **kwargs):
        record = self.get_record(**kwargs)
        table_id = record.table.id
        record.is_active = False
        record.deleted_at = timezone.now()
        record.save()
        messages.success(request, 'Record deleted successfully!')
        return redirect('dashboard:table_detail', pk=table_id)


class DashboardDetailView(LoginRequiredMixin, DetailView):
    """View and interact with a dashboard"""
    model = Dashboard
    template_name = 'dashboard/dashboard_detail.html'
    context_object_name = 'dashboard'
    
    def get_queryset(self):
        # Allow access to any dashboard in any workspace the user belongs to.
        # Filtering by a single current_workspace is too narrow and causes 404s
        # after redirects where the session workspace may not match.
        from apps.workspaces.models import WorkspaceMembership
        user_workspace_ids = WorkspaceMembership.objects.filter(
            user=self.request.user
        ).values_list('workspace_id', flat=True)
        return Dashboard.objects.filter(
            workspace_id__in=user_workspace_ids,
            is_active=True,
        ).prefetch_related(
            'widgets__table',
            'widgets__table__workspace',
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get all tables for widget creation
        context['tables'] = DataTable.objects.filter(
            workspace=self.object.workspace,
            is_active=True
        )

        # Load LLM-generated Insight records when this is the workspace overview dashboard
        if self.object.slug == 'workspace-overview':
            from apps.insights.models import Insight
            context['ai_insights'] = list(
                Insight.objects.filter(workspace=self.object.workspace)
                .order_by('insight_type', '-created_at')
            )

        # Serialize dashboard data with proper positions
        dashboard_data = {
            'id': str(self.object.id),
            'name': self.object.name,
            'description': self.object.description,
            'slug': self.object.slug,
            'layout_config': self.object.layout_config,
            'filter_config': self.object.filter_config or {},
            'is_public': self.object.is_public,
            'public_uuid': str(self.object.public_uuid),
            'created_at': self.object.created_at.isoformat(),
            'updated_at': self.object.updated_at.isoformat(),
            'widgets': []
        }
        
        # Manually build widget data with proper positions
        for widget in self.object.widgets.all().select_related('table'):
            widget_data = {
                'id': str(widget.id),
                'dashboard': str(widget.dashboard_id),
                'widget_type': widget.widget_type,
                'title': widget.title,
                'table': str(widget.table_id) if widget.table else None,
                'table_name': widget.table.name if widget.table else None,
                'query_config': widget.query_config,
                'viz_config': widget.viz_config,
                'position': widget.position or {'x': 0, 'y': 0, 'w': 4, 'h': 4},
                'widget_data': None,
                'created_at': widget.created_at.isoformat(),
                'updated_at': widget.updated_at.isoformat()
            }
            dashboard_data['widgets'].append(widget_data)
        
        logger.debug(f"Dashboard {self.object.id} has {len(dashboard_data['widgets'])} widgets")

        # Pass the raw dict — the template uses |json_script which handles serialization.
        # Do NOT pre-serialize with json.dumps(); that causes double-encoding and
        # makes dashboardData a string instead of an object in JavaScript.
        context['dashboard_data'] = dashboard_data

        return context
    
    def _get_widget_data(self, widget):
        """Get widget data with error handling"""
        try:
            return widget.get_data(limit=100)
        except Exception as e:
            logger.error(f"Error getting data for widget {widget.id}: {e}")
            return {"error": str(e)}
         
class DashboardEditView(LoginRequiredMixin, UpdateView):
    """Edit dashboard settings"""
    model = Dashboard
    template_name = 'dashboard/dashboard_form.html'
    fields = ['name', 'description', 'is_public']

    def get_queryset(self):
        from apps.workspaces.models import WorkspaceMembership
        user_workspace_ids = WorkspaceMembership.objects.filter(
            user=self.request.user
        ).values_list('workspace_id', flat=True)
        return Dashboard.objects.filter(workspace_id__in=user_workspace_ids, is_active=True)

    def get_success_url(self):
        return reverse('dashboard:dashboard_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        messages.success(self.request, f'Dashboard "{form.instance.name}" updated successfully!')
        return super().form_valid(form)


class DashboardDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a dashboard"""
    model = Dashboard
    template_name = 'dashboard/dashboard_confirm_delete.html'
    success_url = reverse_lazy('dashboard:dashboards')

    def get_queryset(self):
        from apps.workspaces.models import WorkspaceMembership
        user_workspace_ids = WorkspaceMembership.objects.filter(
            user=self.request.user
        ).values_list('workspace_id', flat=True)
        return Dashboard.objects.filter(workspace_id__in=user_workspace_ids, is_active=True)
    
    def form_valid(self, form):
        dashboard = self.get_object()
        dashboard.is_active = False
        dashboard.deleted_at = timezone.now()
        dashboard.save()
        messages.success(self.request, f'Dashboard "{dashboard.name}" deleted successfully!')
        return redirect(self.success_url)


class PublicDashboardView(TemplateView):
    """
    Unauthenticated, read-only view for a shared dashboard.

    URL: ``/d/<public_uuid>/``
    Renders only when ``dashboard.is_public`` is True; returns 404 otherwise.
    """

    template_name = 'dashboard/public_dashboard.html'

    def get(self, request, public_uuid, **kwargs):
        dashboard = get_object_or_404(
            Dashboard,
            public_uuid=public_uuid,
            is_public=True,
            is_active=True,
        )
        # Build the same widget-enriched dict used by DashboardDetailView so the
        # template can reuse the same Plotly rendering logic.
        widgets_data = []
        for widget in dashboard.widgets.all().select_related('table').order_by('created_at'):
            try:
                wdata = widget.get_data(limit=100)
            except Exception as exc:
                logger.warning("Public dashboard widget %s data error: %s", widget.id, exc)
                wdata = {"error": str(exc)}
            widgets_data.append({
                'id': str(widget.id),
                'widget_type': widget.widget_type,
                'title': widget.title,
                'query_config': widget.query_config,
                'viz_config': widget.viz_config,
                'position': widget.position or {'x': 0, 'y': 0, 'w': 4, 'h': 4},
                'widget_data': wdata,
            })

        dashboard_data = {
            'id': str(dashboard.id),
            'name': dashboard.name,
            'description': dashboard.description,
            'workspace_name': dashboard.workspace.name,
            'layout_config': dashboard.layout_config,
            'widgets': widgets_data,
        }

        context = self.get_context_data(**kwargs)
        context['dashboard'] = dashboard
        context['dashboard_data'] = dashboard_data
        return self.render_to_response(context)


class WorkspaceSettingsView(LoginRequiredMixin, TemplateView):
    """
    Workspace settings page — GET to view, POST to mutate.

    URL: ``/dashboard/settings/``

    Supported POST actions
    ----------------------
    The template submits two separate forms, each including a hidden
    ``action`` field to distinguish them server-side:

    ``action = 'update_workspace'``
        Rename the workspace.  Requires a non-empty ``workspace_name`` field.
        Redirects back to the settings page with a success/error message.

    ``action = 'delete_workspace'``
        Permanently delete the workspace and all cascading data (tables,
        records, dashboards, memberships, M-Pesa transactions, etc.).
        Requires a ``confirm_name`` field that must exactly match the current
        workspace name — this acts as a second factor to prevent accidental
        deletion.

        On success:

        1. The workspace DB row is deleted (cascades via FK constraints).
        2. ``current_workspace_id`` is removed from the session.
        3. The transient ``request.user.current_workspace`` attribute is
           cleared so the next request doesn't attempt to load a deleted row.
        4. Redirects to ``dashboard:workspaces`` so the user can create or
           switch to another workspace.

    Authorization
    -------------
    Only the workspace **owner** (``workspace.owner == request.user``) can
    submit either action.  Members with other roles see an error message and
    are redirected back.  This check is applied before routing by action so
    it cannot be bypassed by sending an unexpected ``action`` value.

    Context (GET)
    -------------
    ``workspace``
        The user's currently active ``Workspace`` object, or ``None`` if the
        user has no workspace.  The template handles the ``None`` case with a
        "No workspace selected" empty state.
    """

    template_name = 'dashboard/settings.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['workspace'] = self.request.user.current_workspace
        return context

    def post(self, request, *args, **kwargs):
        workspace = request.user.current_workspace
        if not workspace:
            messages.error(request, 'No active workspace.')
            return redirect('dashboard:settings')

        # Only the owner can modify or delete — members with other roles cannot.
        if workspace.owner != request.user:
            messages.error(request, 'Only the workspace owner can make changes.')
            return redirect('dashboard:settings')

        action = request.POST.get('action')

        if action == 'update_workspace':
            name = request.POST.get('workspace_name', '').strip()
            if not name:
                messages.error(request, 'Workspace name cannot be empty.')
                return redirect('dashboard:settings')
            workspace.name = name
            workspace.save(update_fields=['name'])
            messages.success(request, 'Workspace updated successfully.')
            return redirect('dashboard:settings')

        if action == 'delete_workspace':
            confirm_name = request.POST.get('confirm_name', '').strip()
            if confirm_name != workspace.name:
                messages.error(request, 'Workspace name did not match. Deletion cancelled.')
                return redirect('dashboard:settings')

            workspace_name = workspace.name

            # Deleting the workspace cascades via FK to all child data.
            workspace.delete()

            # Clear the workspace reference from the session and from the
            # transient user attribute so the next request starts clean.
            request.session.pop('current_workspace_id', None)
            request.session.modified = True
            # SimpleLazyObject (request.user) doesn't support __delattr__,
            # so set to None rather than del.
            request.user.current_workspace = None

            messages.success(request, f'Workspace "{workspace_name}" has been permanently deleted.')
            return redirect('dashboard:workspaces')

        messages.error(request, 'Unknown action.')
        return redirect('dashboard:settings')


class TeamMembersView(LoginRequiredMixin, ListView):
    """List current workspace members and pending invitations.

    Renders ``dashboard/members.html`` with:

    ``memberships``
        All ``WorkspaceMembership`` rows for the active workspace,
        with ``user`` pre-fetched to avoid N+1 queries.
    ``pending_invitations``
        ``WorkspaceInvitation`` rows that have not been accepted or
        revoked — shown in a separate section so owners/admins can
        revoke stale invites.
    ``workspace``
        The active ``Workspace`` instance (used by the template to
        check the viewer's role before showing management actions).

    URL: ``/dashboard/members/``
    """
    model = WorkspaceMembership
    template_name = 'dashboard/members.html'
    context_object_name = 'memberships'

    def get_queryset(self):
        workspace = self.request.user.current_workspace
        return WorkspaceMembership.objects.filter(
            workspace=workspace
        ).select_related('user')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workspace = self.request.user.current_workspace
        if workspace:
            from apps.workspaces.models import WorkspaceInvitation
            context['pending_invitations'] = WorkspaceInvitation.objects.filter(
                workspace=workspace,
                is_accepted=False,
                is_revoked=False,
            )
            context['workspace'] = workspace
        return context


class ActivityLogView(LoginRequiredMixin, ListView):
    """Paginated activity feed combining Notifications + AuditLog entries.

    Notifications (the source of the sidebar badge) are shown first and
    marked as read on page load.  AuditLog entries (widget/export ops) are
    also included so the full audit trail is visible in one place.

    URL: ``/dashboard/activity/``
    """
    template_name = 'dashboard/activity.html'
    context_object_name = 'logs'
    paginate_by = 50

    def _resolve_workspace(self):
        workspace = getattr(self.request.user, 'current_workspace', None)
        if not workspace:
            workspace_id = self.request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, members=self.request.user)
                    self.request.user.current_workspace = workspace
                except Workspace.DoesNotExist:
                    pass
        if not workspace:
            workspace = Workspace.objects.filter(members=self.request.user).first()
        return workspace

    def get_queryset(self):
        from apps.notifications.models import Notification
        workspace = self._resolve_workspace()
        if not workspace:
            return Notification.objects.none()

        # Notifications for this user in this workspace (or workspace-agnostic)
        return Notification.objects.filter(
            user=self.request.user,
        ).filter(
            Q(workspace=workspace) | Q(workspace__isnull=True)
        ).order_by('-created_at')

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        # Mark all unread notifications as read now that user has visited the page
        from apps.notifications.models import Notification
        workspace = self._resolve_workspace()
        if workspace:
            Notification.objects.filter(
                user=request.user,
                is_read=False,
            ).filter(
                Q(workspace=workspace) | Q(workspace__isnull=True)
            ).update(is_read=True)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Also pass recent AuditLog entries for the workspace (widget/export ops)
        workspace = self._resolve_workspace()
        if workspace:
            context['audit_logs'] = AuditLog.objects.filter(
                workspace=workspace
            ).select_related('user').order_by('-timestamp')[:20]
        return context


class DashboardCommentsView(LoginRequiredMixin, View):
    """
    GET  — return JSON list of comments for a dashboard.
    POST — create a new comment; parse @mentions and notify.

    URL: ``/dashboard/dashboards/<uuid>/comments/``
    """

    def _get_dashboard(self, request, pk):
        from apps.workspaces.models import WorkspaceMembership
        user_workspace_ids = WorkspaceMembership.objects.filter(
            user=request.user
        ).values_list('workspace_id', flat=True)
        return get_object_or_404(Dashboard, pk=pk, workspace_id__in=user_workspace_ids, is_active=True)

    def get(self, request, pk):
        from apps.dashboards.models import DashboardComment
        dashboard = self._get_dashboard(request, pk)
        qs = DashboardComment.objects.filter(
            dashboard=dashboard, is_deleted=False
        ).select_related('user').order_by('created_at')
        data = [
            {
                'id':         str(c.id),
                'user_name':  c.user.get_full_name() or c.user.email if c.user else 'Unknown',
                'user_email': c.user.email if c.user else '',
                'body':       c.body,
                'widget_id':  str(c.widget_id) if c.widget_id else None,
                'created_at': c.created_at.isoformat(),
                'is_mine':    c.user_id == request.user.id,
            }
            for c in qs
        ]
        return JsonResponse({'comments': data})

    def post(self, request, pk):
        from apps.dashboards.models import DashboardComment
        dashboard = self._get_dashboard(request, pk)
        try:
            body = json.loads(request.body).get('body', '').strip()
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({'error': 'Invalid JSON.'}, status=400)
        if not body:
            return JsonResponse({'error': 'Comment body is required.'}, status=400)
        if len(body) > 2000:
            return JsonResponse({'error': 'Comment too long (max 2000 chars).'}, status=400)

        comment = DashboardComment.objects.create(
            dashboard=dashboard,
            user=request.user,
            body=body,
        )

        # Parse @mentions — match @word or @email patterns
        import re
        from apps.notifications.models import Notification
        from apps.workspaces.models import WorkspaceMembership
        from django.contrib.auth import get_user_model
        AuthUser = get_user_model()

        mentions = re.findall(r'@([\w.+-]+)', body)
        if mentions:
            members = WorkspaceMembership.objects.filter(
                workspace=dashboard.workspace
            ).select_related('user')
            member_map = {}
            for m in members:
                member_map[m.user.email.split('@')[0].lower()] = m.user
                member_map[m.user.email.lower()] = m.user
                if m.user.get_full_name():
                    member_map[m.user.get_full_name().replace(' ', '').lower()] = m.user

            notified = set()
            for handle in mentions:
                target = member_map.get(handle.lower())
                if target and target.id != request.user.id and target.id not in notified:
                    notified.add(target.id)
                    Notification.notify(
                        user=target,
                        title=f"{request.user.get_full_name() or request.user.email} mentioned you",
                        message=f'In "{dashboard.name}": {body[:200]}',
                        notif_type='info',
                        workspace=dashboard.workspace,
                        action_url=f"/dashboard/dashboards/{dashboard.id}/",
                    )

        return JsonResponse({
            'id':         str(comment.id),
            'user_name':  request.user.get_full_name() or request.user.email,
            'user_email': request.user.email,
            'body':       comment.body,
            'widget_id':  None,
            'created_at': comment.created_at.isoformat(),
            'is_mine':    True,
        }, status=201)


@login_required
@require_POST
def delete_dashboard_comment(request, pk, comment_id):
    """Soft-delete a comment owned by the requesting user."""
    from apps.dashboards.models import DashboardComment
    workspace = getattr(request.user, 'current_workspace', None)
    if not workspace:
        wid = request.session.get('current_workspace_id')
        if wid:
            try:
                workspace = Workspace.objects.get(id=wid, members=request.user)
            except Workspace.DoesNotExist:
                pass
    get_object_or_404(Dashboard, pk=pk, workspace=workspace, is_active=True)
    comment = get_object_or_404(
        DashboardComment, pk=comment_id, dashboard_id=pk, user=request.user
    )
    comment.is_deleted = True
    comment.save(update_fields=['is_deleted'])
    return JsonResponse({'deleted': True})


class TemplateGalleryView(LoginRequiredMixin, TemplateView):
    """
    Gallery of pre-built dashboard starter templates.

    Shows all 8 templates with icon, description, and an "Apply" button.
    Applying creates the DataTable + sample Records + auto-generated Dashboard
    in the current workspace via OnboardingService.seed_workspace().

    URL: ``/dashboard/templates/``
    """
    template_name = 'dashboard/template_gallery.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.workspaces.onboarding import INDUSTRY_LABELS, TEMPLATE_META
        templates = []
        for key, label in INDUSTRY_LABELS.items():
            meta = TEMPLATE_META.get(key, {})
            templates.append({
                'key':   key,
                'label': label,
                'icon':  meta.get('icon', 'fa-table'),
                'color': meta.get('color', 'bg-gray-100 text-gray-600'),
                'desc':  meta.get('desc', ''),
            })
        context['templates'] = templates
        return context


@login_required
@require_POST
def apply_template(request, template_key):
    """
    Seed the current workspace with a starter template and redirect to the
    generated dashboard.
    """
    from apps.workspaces.onboarding import INDUSTRY_LABELS, OnboardingService

    workspace = getattr(request.user, 'current_workspace', None)
    if not workspace:
        workspace_id = request.session.get('current_workspace_id')
        if workspace_id:
            try:
                workspace = Workspace.objects.get(id=workspace_id, members=request.user)
            except Workspace.DoesNotExist:
                pass
    if not workspace:
        workspace = Workspace.objects.filter(members=request.user).first()

    if not workspace:
        messages.error(request, "No active workspace found.")
        return redirect('dashboard:template_gallery')

    if template_key not in INDUSTRY_LABELS:
        messages.error(request, "Unknown template.")
        return redirect('dashboard:template_gallery')

    dashboard = OnboardingService.seed_workspace(workspace, template_key, request.user)
    messages.success(request, f"Template applied! Your dashboard is ready.")
    return redirect('dashboard:dashboard_detail', pk=dashboard.id)


class BillingView(LoginRequiredMixin, TemplateView):
    """
    Billing and subscription management page for the active workspace.

    URL: ``/dashboard/billing/``

    Also the destination for the public ``/pricing/`` page when a logged-in
    user visits it — ``core.views.PricingView.dispatch`` issues a 302 redirect
    here so authenticated users skip the marketing copy and land directly where
    they can pay.

    This view is **read-only** (GET only).  All mutations — initiating a
    payment, upgrading a plan — are handled by the M-Pesa API endpoints in
    ``apps/subscriptions/views.py``.

    Template
    --------
    ``dashboard/billing.html`` renders:

    * Current usage progress bars (tables, records, members).
    * Four plan cards (Free / Starter / Professional / Enterprise) with
      M-Pesa payment buttons.
    * A sandbox warning banner (only when ``debug=True``).
    * Payment history table (last 10 ``MpesaTransaction`` rows for the workspace).
    * An Alpine.js M-Pesa modal that drives the STK Push → polling flow.

    Context variables
    -----------------
    ``workspace``
        The user's active ``Workspace``, or ``None``.
    ``debug``
        ``True`` when ``settings.DEBUG`` is set.  The template uses this to
        show the amber *"Sandbox mode: KES 1 charged"* banner so developers
        are never surprised by sandbox amounts.
    ``plans``
        ``QuerySet[Plan]`` of all active plans ordered by ``price_monthly``.
        Used by the template to render plan cards dynamically.  Falls back to
        an empty queryset if the subscriptions app tables don't exist yet
        (e.g. before migrations are run).
    ``subscription``
        The workspace's current ``Subscription`` object, or ``None`` if no
        subscription row exists yet (free tier workspaces may not have one).
    ``plan_limits``
        The ``PLAN_LIMITS`` dict from ``apps.subscriptions.models`` — passed
        to the template for building feature comparison tooltips.
    """

    template_name = 'dashboard/billing.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workspace = self.request.user.current_workspace
        context['workspace'] = workspace
        # Passed to the template to conditionally render the sandbox warning banner.
        context['debug'] = settings.DEBUG
        if workspace:
            try:
                from apps.subscriptions.models import Plan, Subscription, PLAN_LIMITS
                context['plans']       = Plan.objects.filter(is_active=True).order_by('price_monthly')
                context['subscription'] = Subscription.objects.filter(workspace=workspace).first()
                context['plan_limits'] = PLAN_LIMITS
            except Exception:
                # Gracefully degrade if migration hasn't been run yet — the billing
                # page will render without plan cards rather than raising a 500.
                pass
        return context


class ProfileView(LoginRequiredMixin, TemplateView):
    """User profile and notification-preference settings.

    GET renders ``dashboard/profile.html`` with the user's current
    ``NotificationPreference`` row (created on first visit if absent).

    POST dispatches on the hidden ``action`` field:

    ``update_profile``
        Updates ``first_name`` and ``last_name`` using ``save(update_fields=…)``
        so only those two columns hit the database.

    ``update_notification_prefs``
        Reads checkbox presence for ``email_invites``, ``email_imports``,
        ``email_insights``, and ``email_system``; saves the preference row.

    URL: ``/dashboard/profile/``
    """
    template_name = 'dashboard/profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.notifications.models import NotificationPreference
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.request.user)
        context['notif_prefs'] = prefs
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')

        if action == 'update_profile':
            request.user.first_name = request.POST.get('first_name', '').strip()
            request.user.last_name  = request.POST.get('last_name', '').strip()
            request.user.save(update_fields=['first_name', 'last_name'])
            messages.success(request, 'Profile updated successfully.')
            return redirect('dashboard:profile')

        if action == 'update_notification_prefs':
            from apps.notifications.models import NotificationPreference
            prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
            prefs.email_invites  = 'email_invites'  in request.POST
            prefs.email_imports  = 'email_imports'  in request.POST
            prefs.email_insights = 'email_insights' in request.POST
            prefs.email_system   = 'email_system'   in request.POST
            prefs.whatsapp_number = request.POST.get('whatsapp_number', '').strip()
            prefs.whatsapp_alerts = 'whatsapp_alerts' in request.POST
            prefs.save()
            messages.success(request, 'Notification preferences saved.')
            return redirect('dashboard:profile')

        messages.error(request, 'Unknown action.')
        return redirect('dashboard:profile')


class TableExportView(LoginRequiredMixin, TemplateView):
    """Thin proxy to ``apps.exports.views.export_table``.

    Delegates the actual file generation to the exports app so that
    export logic can be tested and reused independently of the dashboard
    URL namespace.  Accepts the same ``?format=csv|json|excel`` query
    parameter as the underlying view.

    URL: ``/dashboard/tables/<uuid:pk>/export/``
    """
    template_name = None  # no template; returns file download

    def get(self, request, *args, **kwargs):
        from apps.exports.views import export_table
        return export_table(request, kwargs['table_id'])

class CalculatedFieldsView(LoginRequiredMixin, TemplateView):
    """
    Manage calculated fields for a DataTable.

    URL: /dashboard/tables/<uuid:table_id>/calculated-fields/
    """
    template_name = 'dashboard/calculated_fields.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        table_id = self.kwargs['table_id']
        table = get_object_or_404(DataTable, pk=table_id)

        # Workspace guard
        workspace = getattr(self.request.user, 'current_workspace', None)
        if workspace and table.workspace_id != workspace.id:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied()

        fields = CalculatedField.objects.filter(table=table).order_by('name')
        # Schema: infer column names from first record
        columns = []
        try:
            from apps.dashboards.models import Record
            sample = Record.objects.filter(table=table, is_active=True).values_list('data', flat=True).first()
            if sample:
                columns = list(sample.keys())
        except Exception:
            pass

        context.update({
            'table': table,
            'calculated_fields': fields,
            'columns': columns,
            'format_choices': CalculatedField.FORMAT_CHOICES,
            'api_base': f'/api/v1/tables/{table_id}/calculated-fields/',
            'validate_url': f'/api/v1/tables/{table_id}/calculated-fields/validate/',
        })
        return context


class CohortFunnelView(LoginRequiredMixin, TemplateView):
    """
    Cohort retention & funnel analysis UI for a DataTable.

    URL: /dashboard/tables/<uuid:table_id>/cohort-funnel/
    """
    template_name = 'dashboard/cohort_funnel.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        table_id = self.kwargs['table_id']

        workspace = getattr(self.request.user, 'current_workspace', None)
        if not workspace:
            workspace_id = self.request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, members=self.request.user)
                    self.request.user.current_workspace = workspace
                except Workspace.DoesNotExist:
                    pass
        if not workspace:
            workspace = Workspace.objects.filter(members=self.request.user).first()

        table = get_object_or_404(DataTable, pk=table_id, workspace=workspace, is_active=True)
        context['table'] = table
        return context
