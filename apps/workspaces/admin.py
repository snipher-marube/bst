from django.contrib import admin
from django.utils.html import format_html
from .models import Workspace, WorkspaceMembership

@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'tier', 'created_at', 'is_active']
    list_filter = ['tier', 'is_active', 'created_at']
    search_fields = ['name', 'owner__email']
    readonly_fields = ['id', 'created_at', 'updated_at']
    fieldsets = (
        ('Basic Info', {
            'fields': ('id', 'name', 'owner', 'tier')
        }),
        ('Limits', {
            'fields': ('max_tables', 'max_records_per_table', 'max_team_members')
        }),
        ('Status', {
            'fields': ('is_active', 'created_at', 'updated_at')
        }),
    )

@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(admin.ModelAdmin):
    list_display = ['workspace', 'user', 'role', 'joined_at']
    list_filter = ['role', 'joined_at']
    search_fields = ['workspace__name', 'user__email']
