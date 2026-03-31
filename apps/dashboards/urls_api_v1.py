"""
API v1 URL patterns – all under /api/v1/
"""
from django.urls import path
from . import api_v1

urlpatterns = [
    # Auth / Profile
    path('auth/register/', api_v1.RegisterAPIView.as_view(), name='api_v1_register'),
    path('auth/profile/', api_v1.ProfileAPIView.as_view(), name='api_v1_profile'),

    # Workspaces
    path('workspaces/', api_v1.WorkspaceListCreateAPIView.as_view(), name='api_v1_workspaces'),
    path('workspaces/<uuid:pk>/', api_v1.WorkspaceDetailAPIView.as_view(), name='api_v1_workspace_detail'),

    # Tables (workspace-scoped)
    path('workspaces/<uuid:workspace_id>/tables/', api_v1.TableListCreateAPIView.as_view(), name='api_v1_tables'),
    path('tables/<uuid:pk>/', api_v1.TableDetailAPIView.as_view(), name='api_v1_table_detail'),

    # Records (table-scoped)
    path('tables/<uuid:table_id>/records/', api_v1.RecordListCreateAPIView.as_view(), name='api_v1_records'),
    path('records/<uuid:pk>/', api_v1.RecordDetailAPIView.as_view(), name='api_v1_record_detail'),

    # Import / Export
    path('tables/<uuid:table_id>/import/', api_v1.TableImportAPIView.as_view(), name='api_v1_table_import'),
    path('tables/<uuid:table_id>/export/', api_v1.TableExportAPIView.as_view(), name='api_v1_table_export'),
    path('imports/<uuid:job_id>/status/', api_v1.ImportJobStatusAPIView.as_view(), name='api_v1_import_status'),

    # Dashboards (workspace-scoped)
    path('workspaces/<uuid:workspace_id>/dashboards/', api_v1.DashboardListCreateAPIView.as_view(), name='api_v1_dashboards'),
    path('dashboards/<uuid:pk>/', api_v1.DashboardDetailAPIView.as_view(), name='api_v1_dashboard_detail'),

    # Widgets (dashboard-scoped)
    path('dashboards/<uuid:dashboard_id>/widgets/', api_v1.WidgetListCreateAPIView.as_view(), name='api_v1_widgets'),
    path('widgets/<uuid:pk>/', api_v1.WidgetDetailAPIView.as_view(), name='api_v1_widget_detail'),

    # Insights
    path('workspaces/<uuid:workspace_id>/insights/', api_v1.InsightsListAPIView.as_view(), name='api_v1_insights'),
    path('workspaces/<uuid:workspace_id>/insights/generate/', api_v1.InsightsGenerateAPIView.as_view(), name='api_v1_insights_generate'),

    # Team
    path('workspaces/<uuid:workspace_id>/members/', api_v1.TeamMemberListAPIView.as_view(), name='api_v1_members'),
    path('workspaces/<uuid:workspace_id>/members/<int:user_id>/', api_v1.TeamMemberDetailAPIView.as_view(), name='api_v1_member_detail'),
]
