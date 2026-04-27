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
    path('tables/<uuid:table_id>/profile/', api_v1.TableProfileAPIView.as_view(), name='api_v1_table_profile'),

    # Records (table-scoped)
    path('tables/<uuid:table_id>/records/', api_v1.RecordListCreateAPIView.as_view(), name='api_v1_records'),
    path('tables/<uuid:table_id>/records/batch/', api_v1.RecordBatchAPIView.as_view(), name='api_v1_records_batch'),
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

    # Data Alerts
    path('workspaces/<uuid:workspace_id>/alerts/', api_v1.DataAlertListCreateAPIView.as_view(), name='api_v1_alerts'),
    path('alerts/<uuid:pk>/', api_v1.DataAlertDetailAPIView.as_view(), name='api_v1_alert_detail'),

    # Webhook Endpoints
    path('workspaces/<uuid:workspace_id>/webhooks/', api_v1.WebhookEndpointListCreateAPIView.as_view(), name='api_v1_webhooks'),
    path('webhooks/<uuid:pk>/', api_v1.WebhookEndpointDetailAPIView.as_view(), name='api_v1_webhook_detail'),
    path('webhooks/<uuid:pk>/regenerate-secret/', api_v1.WebhookEndpointRegenerateSecretAPIView.as_view(), name='api_v1_webhook_regenerate_secret'),

    # Audit Log
    path('workspaces/<uuid:workspace_id>/audit-logs/', api_v1.AuditLogListAPIView.as_view(), name='api_v1_audit_logs'),

    # Workspace Usage Analytics
    path('workspaces/<uuid:workspace_id>/usage/', api_v1.WorkspaceUsageAPIView.as_view(), name='api_v1_workspace_usage'),

    # Data Sources (direct DB connectors — Gap 7)
    path('workspaces/<uuid:workspace_id>/data-sources/', api_v1.DataSourceListCreateAPIView.as_view(), name='api_v1_data_sources'),
    path('data-sources/<uuid:pk>/', api_v1.DataSourceDetailAPIView.as_view(), name='api_v1_data_source_detail'),
    path('data-sources/<uuid:pk>/test/', api_v1.DataSourceTestAPIView.as_view(), name='api_v1_data_source_test'),
    path('data-sources/<uuid:pk>/schema/', api_v1.DataSourceSchemaAPIView.as_view(), name='api_v1_data_source_schema'),

    # Calculated Fields (Gap 14)
    path('tables/<uuid:table_id>/calculated-fields/', api_v1.CalculatedFieldListCreateAPIView.as_view(), name='api_v1_calculated_fields'),
    path('tables/<uuid:table_id>/calculated-fields/validate/', api_v1.CalculatedFieldValidateAPIView.as_view(), name='api_v1_calculated_field_validate'),
    path('calculated-fields/<uuid:pk>/', api_v1.CalculatedFieldDetailAPIView.as_view(), name='api_v1_calculated_field_detail'),
    path('calculated-fields/<uuid:pk>/preview/', api_v1.CalculatedFieldPreviewAPIView.as_view(), name='api_v1_calculated_field_preview'),

    # Cohort & Funnel Analysis (Gap 15)
    path('tables/<uuid:table_id>/cohort-analysis/', api_v1.CohortAnalysisAPIView.as_view(), name='api_v1_cohort_analysis'),
    path('tables/<uuid:table_id>/funnel-analysis/', api_v1.FunnelAnalysisAPIView.as_view(), name='api_v1_funnel_analysis'),
]
