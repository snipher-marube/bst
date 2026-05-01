# apps/insights/routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # More-specific routes first to avoid shadowing by the generic dashboard route
    # Ask AI chat panel (per-dashboard, session-scoped)
    re_path(r'ws/dashboard/(?P<dashboard_id>[a-f0-9-]+)/ask-ai/$',
            consumers.AskAIConsumer.as_asgi()),

    # Dashboard WebSocket
    re_path(r'ws/dashboard/(?P<dashboard_id>[a-f0-9-]+)/$',
            consumers.DashboardConsumer.as_asgi()),

    # Workspace-level updates (for multi-dashboard notifications)
    re_path(r'ws/workspace/(?P<workspace_id>[a-f0-9-]+)/$',
            consumers.WorkspaceConsumer.as_asgi()),

    # Table updates (for data change notifications)
    re_path(r'ws/table/(?P<table_id>[a-f0-9-]+)/$',
            consumers.TableConsumer.as_asgi()),
]