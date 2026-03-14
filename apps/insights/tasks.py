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
        from .services import QueryEngine
        
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