# apps/dashboards/debug.py
import json
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import Dashboard, Widget

@login_required
def debug_dashboard(request, dashboard_id):
    """Debug endpoint to check dashboard data"""
    try:
        dashboard = Dashboard.objects.get(id=dashboard_id, workspace=request.user.current_workspace)
        widgets = dashboard.widgets.all()
        
        debug_info = {
            'dashboard': {
                'id': str(dashboard.id),
                'name': dashboard.name,
                'widget_count': widgets.count(),
            },
            'widgets': []
        }
        
        for widget in widgets:
            # Test get_data method
            try:
                data = widget.get_data(limit=5)
                data_status = 'success'
            except Exception as e:
                data = str(e)
                data_status = 'error'
            
            debug_info['widgets'].append({
                'id': str(widget.id),
                'type': widget.widget_type,
                'title': widget.title,
                'table': widget.table.name if widget.table else None,
                'query_config': widget.query_config,
                'data_status': data_status,
                'data_preview': data if data_status == 'success' else None,
            })
        
        return JsonResponse(debug_info)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)