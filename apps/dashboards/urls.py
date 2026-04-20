from django.urls import path
from . import views
from . import debug

app_name = 'dashboard'

urlpatterns = [
    # Main dashboard views
    path('analytics/', views.DashboardHomeView.as_view(), name='home'),
    
    # Workspace management
    path('workspaces/', views.WorkspaceListView.as_view(), name='workspaces'),
    path('workspaces/create/', views.WorkspaceCreateView.as_view(), name='workspace_create'),
    path('workspaces/<uuid:pk>/switch/', views.switch_workspace, name='workspace_switch'),
    
    # Table management
    path('tables/', views.TableView.as_view(), name='tables'),
    path('tables/create/', views.TableCreateView.as_view(), name='table_create'),
    path('tables/create/import/', views.TableCreateFromImportView.as_view(), name='table_create_import'),
    path('tables/<uuid:pk>/', views.TableDetailView.as_view(), name='table_detail'),
    path('tables/<uuid:pk>/edit/', views.TableEditView.as_view(), name='table_edit'),
    path('tables/<uuid:pk>/delete/', views.TableDeleteView.as_view(), name='table_delete'),
    path('tables/<uuid:pk>/import/', views.TableImportView.as_view(), name='table_import'),
    
    # Record management (data entry)
    path('tables/<uuid:table_id>/records/', views.RecordListView.as_view(), name='record_list'),
    path('tables/<uuid:table_id>/records/create/', views.RecordCreateView.as_view(), name='record_create'),
    path('tables/<uuid:table_id>/records/<uuid:record_id>/edit/', views.RecordEditView.as_view(), name='record_edit'),
    path('tables/<uuid:table_id>/records/<uuid:record_id>/delete/', views.RecordDeleteView.as_view(), name='record_delete'),
    
    # Dashboard management
    path('dashboards/', views.DashboardListView.as_view(), name='dashboards'),
    path('dashboards/generate-insights/', views.GenerateWorkspaceInsightsView.as_view(), name='generate_insights'),
    path('dashboards/generate/status/', views.GenerationStatusView.as_view(), name='generation_status'),
    path('dashboards/<uuid:pk>/', views.DashboardDetailView.as_view(), name='dashboard_detail'),
    path('dashboards/<uuid:pk>/edit/', views.DashboardEditView.as_view(), name='dashboard_edit'),
    path('dashboards/<uuid:pk>/delete/', views.DashboardDeleteView.as_view(), name='dashboard_delete'),
    path('dashboards/<uuid:pk>/comments/', views.DashboardCommentsView.as_view(), name='dashboard_comments'),
    path('dashboards/<uuid:pk>/comments/<uuid:comment_id>/delete/', views.delete_dashboard_comment, name='dashboard_comment_delete'),
    
    # SSO / SAML
    path('sso/', views.SSOSettingsView.as_view(), name='sso_settings'),

    # Integrations
    path('integrations/', views.IntegrationsView.as_view(), name='integrations'),
    path('integrations/google-sheets/', views.GoogleSheetsConnectView.as_view(), name='google_sheets_connect'),
    path('integrations/chat/save/', views.ChatIntegrationSaveView.as_view(), name='chat_integration_save'),
    path('integrations/chat/<uuid:pk>/delete/', views.ChatIntegrationDeleteView.as_view(), name='chat_integration_delete'),
    path('integrations/chat/test/', views.chat_integration_test, name='chat_integration_test'),

    # Calculated Fields (Gap 14)
    path('tables/<uuid:table_id>/calculated-fields/', views.CalculatedFieldsView.as_view(), name='calculated_fields'),

    # Cohort & Funnel Analysis (Gap 15)
    path('tables/<uuid:table_id>/cohort-funnel/', views.CohortFunnelView.as_view(), name='cohort_funnel'),

    # Dashboard template gallery
    path('templates/', views.TemplateGalleryView.as_view(), name='template_gallery'),
    path('templates/<str:template_key>/apply/', views.apply_template, name='template_apply'),

    # Settings and account
    path('settings/', views.WorkspaceSettingsView.as_view(), name='settings'),
    path('members/', views.TeamMembersView.as_view(), name='members'),
    path('activity/', views.ActivityLogView.as_view(), name='activity'),
    path('billing/', views.BillingView.as_view(), name='billing'),
    path('profile/', views.ProfileView.as_view(), name='profile'),

    # Export (delegates to exports app)
    path('tables/<uuid:table_id>/export/', views.TableExportView.as_view(), name='table_export'),

    # Debug endpoint
    path('debug/dashboard/<uuid:pk>/', debug.debug_dashboard, name='debug_dashboard'),

    
]