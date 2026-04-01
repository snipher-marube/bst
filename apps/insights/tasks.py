# apps/insights/tasks.py
from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.core.cache import cache
import json
import logging

logger = logging.getLogger(__name__)

@shared_task
def broadcast_widget_update(widget_id, dashboard_id):
    """
    Broadcast widget update to all connected clients
    """
    try:
        from apps.dashboards.models import Widget
        from apps.dashboards.services import QueryEngine
        
        # Get fresh data
        widget = Widget.objects.get(id=widget_id)
        engine = QueryEngine()
        
        # Clear cache
        cache_key = engine._generate_cache_key(widget)
        cache.delete(cache_key)
        
        # Get fresh data
        data = widget.get_data()
        
        # Send via WebSocket
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'dashboard_{dashboard_id}',
            {
                'type': 'widget_update',
                'widget_id': widget_id,
                'data': data
            }
        )
        
        logger.info(f"Broadcast update for widget {widget_id}")
        return {'status': 'success'}
        
    except Exception as e:
        logger.error(f"Broadcast failed: {str(e)}")
        return {'status': 'error', 'error': str(e)}

@shared_task
def notify_table_change(table_id, action):
    """
    Notify all dashboards that a table has changed
    """
    try:
        from apps.dashboards.models import DataTable
        
        table = DataTable.objects.get(id=table_id)
        workspace_id = str(table.workspace.id)
        
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'workspace_{workspace_id}',
            {
                'type': 'table_update',
                'table_id': table_id,
                'action': action
            }
        )
        
        logger.info(f"Notified table change: {table.name} - {action}")
        
    except Exception as e:
        logger.error(f"Table notification failed: {str(e)}")


@shared_task(bind=True)
def run_async_import(self, import_job_id):
    """
    Celery task that processes a large CSV/Excel import asynchronously.
    The ImportJob row must already exist with status='pending' before calling this task.
    """
    from django.utils import timezone
    from apps.dashboards.models import ImportJob
    from apps.dashboards.services import DataImportService
    import pandas as pd

    try:
        job = ImportJob.objects.get(id=import_job_id)
    except ImportJob.DoesNotExist:
        logger.error(f"ImportJob {import_job_id} not found")
        return {'status': 'error', 'error': 'ImportJob not found'}

    job.status = 'running'
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id
    job.save(update_fields=['status', 'started_at', 'celery_task_id'])

    try:
        service = DataImportService()
        df = pd.read_json(job.file_path, orient='split')

        job.total_rows = len(df)
        job.save(update_fields=['total_rows'])

        result = service.import_data(
            table=job.table,
            df=df,
            user=job.created_by,
        )

        job.status = 'completed'
        job.success_rows = result['success']
        job.error_rows = result['errors']
        job.error_log = result.get('error_details', [])
        job.processed_rows = result['success'] + result['errors']
        job.completed_at = timezone.now()
        job.save()

        # Notify workspace
        notify_table_change.delay(str(job.table.id), 'import_completed')

        logger.info(f"ImportJob {import_job_id} completed: {result['success']} rows imported")
        return {'status': 'completed', 'success': result['success'], 'errors': result['errors']}

    except Exception as e:
        logger.error(f"ImportJob {import_job_id} failed: {str(e)}")
        job.status = 'failed'
        job.error_log = [str(e)]
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_log', 'completed_at'])
        return {'status': 'error', 'error': str(e)}


@shared_task
def analyze_workspace_tables(workspace_id):
    """
    Background task to generate AI insights for a workspace.
    """
    try:
        from apps.dashboards.models import DataTable, Dashboard, Widget
        from apps.workspaces.models import Workspace
        from apps.dashboards.services import WorkspaceInsightService
        from django.contrib.auth import get_user_model

        User = get_user_model()
        workspace = Workspace.objects.get(id=workspace_id)
        owner = workspace.owner

        service = WorkspaceInsightService()
        dashboard = service.generate_workspace_overview(workspace, owner)

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'workspace_{workspace_id}',
            {
                'type': 'dashboard_update',
                'data': {
                    'action': 'insights_generated',
                    'dashboard_id': str(dashboard.id),
                }
            }
        )
        logger.info(f"Insights generated for workspace {workspace_id}")
        return {'status': 'completed', 'dashboard_id': str(dashboard.id)}

    except Exception as e:
        logger.error(f"analyze_workspace_tables failed: {str(e)}")
        return {'status': 'error', 'error': str(e)}