from django.contrib import admin
from django.utils.html import format_html
from .models import ( DataTable, 
    Record, Dashboard, Widget, AuditLog
)

@admin.register(DataTable)
class DataTableAdmin(admin.ModelAdmin):
    list_display = ['name', 'workspace', 'record_count', 'created_at', 'is_active']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'workspace__name']
    readonly_fields = ['id', 'schema_hash', 'record_count']

@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ['name', 'workspace', 'is_public', 'created_at']
    list_filter = ['is_public', 'created_at']
    search_fields = ['name', 'workspace__name']
    readonly_fields = ['public_uuid']

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'user', 'action', 'content_type', 'object_repr']
    list_filter = ['action', 'content_type', 'timestamp']
    search_fields = ['user__email', 'object_repr']
    readonly_fields = ['timestamp', 'ip_address', 'user_agent']
    
    def has_add_permission(self, request):
        return False  # Audit logs should only be created by the system
    
    def has_change_permission(self, request, obj=None):
        return False  # Audit logs should be immutable