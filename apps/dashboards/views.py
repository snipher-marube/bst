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
import json

from apps.dashboards.models import (
    Workspace, WorkspaceMembership, DataTable, 
    Record, Dashboard, Widget, AuditLog
)
from apps.dashboards.services import DataImportService, AuditService
from apps.dashboards.serializers import DataTableSerializer, DashboardSerializer


class DashboardHomeView(LoginRequiredMixin, TemplateView):
    """Main dashboard landing page"""
    template_name = 'dashboard/index.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workspace = self.request.user.current_workspace
        
        if workspace:
            # Get recent tables
            context['recent_tables'] = DataTable.objects.filter(
                workspace=workspace,
                is_active=True
            ).order_by('-updated_at')[:5]
            
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
        
        messages.success(self.request, f'Workspace "{workspace.name}" created successfully!')
        return redirect('dashboard:home')


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
        
        # Check workspace limits
        if not workspace.can_add_table():
            messages.error(self.request, 'You have reached the maximum number of tables for your workspace.')
            return redirect('dashboard:tables')
        
        form.instance.workspace = workspace
        form.instance.created_by = self.request.user
        messages.success(self.request, f'Table "{form.instance.name}" created successfully!')
        return super().form_valid(form)


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
        
        # Get records for this table
        records = self.object.records.filter(is_active=True).order_by('-created_at')
        
        # Pagination
        from django.core.paginator import Paginator
        paginator = Paginator(records, 50)
        page_number = self.request.GET.get('page', 1)
        context['records'] = paginator.get_page(page_number)
        
        # For API/data export
        context['records_json'] = json.dumps([r.data for r in records[:100]])
        
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


class TableImportView(LoginRequiredMixin, TemplateView):
    """Import data into table"""
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
        
        if 'file' not in request.FILES:
            messages.error(request, 'No file uploaded.')
            return redirect('dashboard:table_import', pk=table.id)
        
        service = DataImportService()
        result = service.import_csv(
            table=table,
            file_obj=request.FILES['file'],
            user=request.user
        )
        
        if result['errors'] == 0:
            messages.success(request, f'Successfully imported {result["success"]} records!')
        else:
            messages.warning(request, f'Imported {result["success"]} records with {result["errors"]} errors.')
        
        return redirect('dashboard:table_detail', pk=table.id)


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


class RecordEditView(LoginRequiredMixin, UpdateView):
    """Edit an existing record"""
    model = Record
    template_name = 'dashboard/record_form.html'
    fields = ['data']
    
    def get_queryset(self):
        return Record.objects.filter(
            table_id=self.kwargs.get('table_id'),
            is_active=True
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['table'] = self.object.table
        return context
    
    def get_success_url(self):
        return reverse('dashboard:table_detail', kwargs={'pk': self.object.table.id})
    
    def form_valid(self, form):
        form.instance.updated_by = self.request.user
        messages.success(self.request, 'Record updated successfully!')
        return super().form_valid(form)


class RecordDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a record"""
    model = Record
    template_name = 'dashboard/record_confirm_delete.html'
    
    def get_queryset(self):
        return Record.objects.filter(
            table_id=self.kwargs.get('table_id'),
            is_active=True
        )
    
    def get_success_url(self):
        return reverse('dashboard:table_detail', kwargs={'pk': self.object.table.id})
    
    def delete(self, request, *args, **kwargs):
        record = self.get_object()
        record.is_active = False
        record.deleted_at = timezone.now()
        record.save()
        messages.success(request, 'Record deleted successfully!')
        return redirect(self.get_success_url())


class DashboardListView(LoginRequiredMixin, ListView):
    """List all dashboards"""
    model = Dashboard
    template_name = 'dashboard/dashboards.html'
    context_object_name = 'dashboards'
    
    def get_queryset(self):
        workspace = self.request.user.current_workspace
        if not workspace:
            return Dashboard.objects.none()
        
        return Dashboard.objects.filter(
            workspace=workspace,
            is_active=True
        ).order_by('-updated_at')


class DashboardCreateView(LoginRequiredMixin, CreateView):
    """Create a new dashboard"""
    model = Dashboard
    template_name = 'dashboard/dashboard_form.html'
    fields = ['name', 'description']
    
    def get_success_url(self):
        return reverse('dashboard:dashboard_detail', kwargs={'pk': self.object.pk})
    
    def form_valid(self, form):
        workspace = self.request.user.current_workspace
        form.instance.workspace = workspace
        form.instance.created_by = self.request.user
        form.instance.slug = form.instance.name.lower().replace(' ', '-')
        messages.success(self.request, f'Dashboard "{form.instance.name}" created successfully!')
        return super().form_valid(form)


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
        ).prefetch_related('widgets')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get all tables for widget creation
        context['tables'] = DataTable.objects.filter(
            workspace=self.object.workspace,
            is_active=True
        )
        
        # Serialize dashboard data for frontend
        from apps.dashboards.serializers import DashboardSerializer
        serializer = DashboardSerializer(self.object, context={'request': self.request})
        context['dashboard_json'] = json.dumps(serializer.data)
        
        return context


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
    """Workspace settings page"""
    template_name = 'dashboard/settings.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['workspace'] = self.request.user.current_workspace
        return context


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
    """Billing and subscription management"""
    template_name = 'dashboard/billing.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['workspace'] = self.request.user.current_workspace
        return context


class ProfileView(LoginRequiredMixin, TemplateView):
    """User profile settings"""
    template_name = 'dashboard/profile.html'