"""
apps/insights/views.py
======================
REST views for AI features:

  POST /api/v1/dashboards/<id>/ai-suggest-widgets/
      Queues advise_and_build_dashboard Celery task.
      Returns {"task_id": "...", "status": "queued"}.

  GET  /api/v1/dashboards/<id>/ask-ai/history/
      Returns past Ask AI Insight records for this dashboard's workspace.
"""

import logging

from django.http import JsonResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from apps.dashboards.models import Dashboard, Widget
from apps.workspaces.models import WorkspaceMembership
from apps.insights.models import Insight

logger = logging.getLogger(__name__)


class AISuggestWidgetsView(APIView):
    """
    POST /api/v1/dashboards/<dashboard_id>/ai-suggest-widgets/

    Body (JSON, optional):
        { "table_id": "<uuid>" }

    Queues advise_and_build_dashboard. AI advises field focus / chart types /
    axis labels; the engine builds the actual Widget records. Result delivered
    via WebSocket (dashboard_ready event on the dashboard group).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, dashboard_id):
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                pk=dashboard_id, is_active=True)
        except Dashboard.DoesNotExist:
            return Response({'error': 'Dashboard not found'}, status=status.HTTP_404_NOT_FOUND)

        # Permission: editor or above
        is_editor = WorkspaceMembership.objects.filter(
            workspace=dashboard.workspace,
            user=request.user,
            role__in=['owner', 'admin', 'editor'],
        ).exists()
        if not is_editor:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

        # Resolve table_id: explicit in body, or infer from dashboard's first widget
        table_id = request.data.get('table_id')
        if not table_id:
            first_widget = (
                Widget.objects
                .filter(dashboard=dashboard, table__isnull=False)
                .values_list('table_id', flat=True)
                .first()
            )
            if not first_widget:
                return Response(
                    {'error': 'No table associated with this dashboard. Provide table_id.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            table_id = str(first_widget)

        # Mark the dashboard as pending so the detail view shows the loading overlay
        dashboard.layout_config = {**dashboard.layout_config, 'ai_pending': True}
        dashboard.save(update_fields=['layout_config'])

        from apps.insights.tasks import advise_and_build_dashboard
        task = advise_and_build_dashboard.delay(
            str(dashboard_id), str(table_id), request.user.id
        )

        logger.info(
            'advise_and_build_dashboard queued dashboard=%s table=%s task=%s user=%s',
            dashboard_id, table_id, task.id, request.user.email,
        )
        return Response({'task_id': task.id, 'status': 'queued'}, status=status.HTTP_202_ACCEPTED)


class AIPendingStatusView(APIView):
    """
    GET /api/v1/dashboards/<dashboard_id>/ai-status/

    Returns {"ai_pending": true/false} so the overlay polling loop can tell
    when the background task has finished without relying on WebSocket delivery.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, dashboard_id):
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                pk=dashboard_id, is_active=True)
        except Dashboard.DoesNotExist:
            return Response({'error': 'Dashboard not found'}, status=status.HTTP_404_NOT_FOUND)

        has_access = WorkspaceMembership.objects.filter(
            workspace=dashboard.workspace, user=request.user
        ).exists()
        if not has_access:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

        return Response({'ai_pending': bool(dashboard.layout_config.get('ai_pending'))})


class AskAIHistoryView(APIView):
    """
    GET /api/v1/dashboards/<dashboard_id>/ask-ai/history/

    Returns the 20 most recent Ask AI Insight records for the workspace,
    used to populate the history log panel on first load.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, dashboard_id):
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                pk=dashboard_id, is_active=True)
        except Dashboard.DoesNotExist:
            return Response({'error': 'Dashboard not found'}, status=status.HTTP_404_NOT_FOUND)

        has_access = WorkspaceMembership.objects.filter(
            workspace=dashboard.workspace, user=request.user
        ).exists()
        if not has_access:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

        insights = (
            Insight.objects
            .filter(workspace=dashboard.workspace, insight_type='ask_ai')
            .order_by('-created_at')[:20]
            .values('id', 'question', 'description', 'created_at', 'created_by__email')
        )
        return Response({'history': list(insights)})
