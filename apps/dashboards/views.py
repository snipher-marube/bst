from django.views.generic import TemplateView, ListView, DetailView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin
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
    Record, Dashboard, AuditLog
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
        workspace = self.request.user.current_workspace
        return DataTable.objects.filter(
            workspace=workspace,
            is_active=True
        )
    
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
    
    def delete(self, request, *args, **kwargs):
        table = self.get_object()
        table.is_active = False
        table.deleted_at = timezone.now()
        table.save()
        messages.success(request, f'Table "{table.name}" deleted successfully!')
        return redirect(self.success_url)


class TableImportMixin:
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


class GenerateWorkspaceInsightsView(LoginRequiredMixin, TemplateView):
    """Automatically generate insights for the current workspace"""

    def post(self, request, *args, **kwargs):
        workspace = request.user.current_workspace
        if not workspace:
            messages.error(request, 'No active workspace found.')
            return redirect('dashboard:home')

        service = WorkspaceInsightService()
        dashboard = service.generate_workspace_overview(workspace, request.user)

        messages.success(request, f'Successfully generated insights in "{dashboard.name}"!')
        return redirect('dashboard:dashboard_detail', pk=dashboard.pk)


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
    """Create a new record (data entry form)"""
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
        
        # Create record
        try:
            record = Record.objects.create(
                table=table,
                data=data,
                created_by=request.user
            )
            messages.success(request, 'Record created successfully!')
            return redirect('dashboard:table_detail', pk=table.id)
        except Exception as e:
            messages.error(request, f'Error creating record: {str(e)}')
            return redirect('dashboard:record_create', table_id=table.id)


class RecordEditView(LoginRequiredMixin, TemplateView):
    """Edit an existing record"""
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
        context['record'] = self.get_record(**self.kwargs)
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
        workspace = self.request.user.current_workspace
        return Dashboard.objects.filter(
            workspace=workspace,
            is_active=True
        ).prefetch_related(
            'widgets__table',
            'widgets__table__workspace'
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get all tables for widget creation
        context['tables'] = DataTable.objects.filter(
            workspace=self.object.workspace,
            is_active=True
        )
        
        # Serialize dashboard data with proper positions
        dashboard_data = {
            'id': str(self.object.id),
            'name': self.object.name,
            'description': self.object.description,
            'slug': self.object.slug,
            'layout_config': self.object.layout_config,
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
                'widget_data': self._get_widget_data(widget),
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
        workspace = self.request.user.current_workspace
        return Dashboard.objects.filter(workspace=workspace, is_active=True)
    
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
        workspace = self.request.user.current_workspace
        return Dashboard.objects.filter(workspace=workspace, is_active=True)
    
    def delete(self, request, *args, **kwargs):
        dashboard = self.get_object()
        dashboard.is_active = False
        dashboard.deleted_at = timezone.now()
        dashboard.save()
        messages.success(request, f'Dashboard "{dashboard.name}" deleted successfully!')
        return redirect(self.success_url)


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
    """Team members management"""
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
    """View workspace activity log"""
    model = AuditLog
    template_name = 'dashboard/activity.html'
    context_object_name = 'logs'
    paginate_by = 50
    
    def get_queryset(self):
        workspace = self.request.user.current_workspace
        return AuditLog.objects.filter(
            workspace=workspace
        ).select_related('user').order_by('-timestamp')


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
    """User profile settings"""
    template_name = 'dashboard/profile.html'


class TableExportView(LoginRequiredMixin, TemplateView):
    """Proxy to the exports app – supports ?format=csv|json|excel"""
    template_name = None  # no template; returns file download

    def get(self, request, *args, **kwargs):
        from apps.exports.views import export_table
        return export_table(request, kwargs['table_id'])