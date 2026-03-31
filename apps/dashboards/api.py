from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.db import transaction
import logging

from .models import Dashboard, Widget, DataTable, Workspace
from .serializers import WidgetSerializer, DashboardSerializer
from .permissions import HasWorkspaceAccess, CanEditData

logger = logging.getLogger(__name__)

class WidgetViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing widgets
    """
    serializer_class = WidgetSerializer
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    
    def get_queryset(self):
        """Filter widgets by user's workspace"""
        workspace = self.request.user.current_workspace
        if not workspace:
            return Widget.objects.none()
        
        return Widget.objects.filter(
            dashboard__workspace=workspace,
            dashboard__is_active=True
        ).select_related('dashboard', 'table')
    
    def perform_create(self, serializer):
        """Create a new widget"""
        serializer.save()
        logger.info(f"Widget created by {self.request.user.email}")


class DashboardWidgetsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    
    def post(self, request, dashboard_id):
        """Add a new widget to a dashboard"""
        try:
            workspace = request.user.current_workspace
            if not workspace:
                return Response(
                    {'error': 'No active workspace found'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            dashboard = get_object_or_404(
                Dashboard, 
                id=dashboard_id, 
                workspace=workspace,
                is_active=True
            )
            
            # Get the table
            table_id = request.data.get('table_id')
            table = None
            if table_id:
                table = get_object_or_404(
                    DataTable,
                    id=table_id,
                    workspace=workspace,
                    is_active=True
                )
            
            # Validate widget type
            widget_type = request.data.get('widget_type')
            if not widget_type:
                return Response(
                    {'error': 'widget_type is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create the widget
            widget = Widget.objects.create(
                dashboard=dashboard,
                widget_type=widget_type,
                title=request.data.get('title', 'New Widget'),
                table=table,
                query_config=request.data.get('query_config', {}),
                viz_config=request.data.get('viz_config', {}),
                position=request.data.get('position', {'x': 0, 'y': 0, 'w': 4, 'h': 4})
            )
            
            # Return the widget with its data
            serializer = WidgetSerializer(widget, context={'request': request})
            response_data = serializer.data
            
            # Log success
            logger.info(f"Widget created: {widget.id} for dashboard {dashboard.id}")
            
            return Response(response_data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating widget: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to create widget: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class DashboardLayoutAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    
    def post(self, request, dashboard_id):
        """Update dashboard layout (widget positions)"""
        try:
            workspace = request.user.current_workspace
            if not workspace:
                return Response(
                    {'error': 'No active workspace found'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            dashboard = get_object_or_404(
                Dashboard, 
                id=dashboard_id, 
                workspace=workspace,
                is_active=True
            )
            
            widgets_data = request.data.get('widgets', [])
            
            if not widgets_data:
                return Response(
                    {'error': 'No widgets data provided'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Update each widget's position
            updated_count = 0
            for widget_data in widgets_data:
                widget_id = widget_data.get('id')
                position = widget_data.get('position', {})
                
                if not widget_id or not position:
                    continue
                
                try:
                    position_data = {
                        'x': position.get('x', 0),
                        'y': position.get('y', 0),
                        'w': position.get('w', 4),
                        'h': position.get('h', 4)
                    }
                    
                    updated = Widget.objects.filter(
                        id=widget_id,
                        dashboard=dashboard
                    ).update(position=position_data)
                    
                    if updated:
                        updated_count += 1
                        
                except Exception as e:
                    logger.error(f"Error updating widget {widget_id}: {e}")
            
            return Response({
                'success': True,
                'message': f'Updated {updated_count} widget positions',
                'updated_count': updated_count
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error updating layout: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to update layout: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class WidgetDataAPIView(APIView):
    """
    API endpoint for refreshing widget data
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]
    
    def get(self, request, widget_id):
        """Get fresh data for a widget"""
        try:
            workspace = request.user.current_workspace
            widget = get_object_or_404(
                Widget,
                id=widget_id,
                dashboard__workspace=workspace
            )
            
            # Clear cache and get fresh data
            from .services import QueryEngine
            from django.core.cache import cache
            
            # Generate and delete cache key
            engine = QueryEngine()
            cache_key = engine._generate_cache_key(widget)
            cache.delete(cache_key)
            
            # Get fresh data
            data = widget.get_data(limit=100)
            
            return Response({'data': data}, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error refreshing widget data: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to refresh data: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DashboardListAPIView(APIView):
    """
    API endpoint for listing dashboards
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]
    
    def get(self, request):
        """Get all dashboards in current workspace"""
        try:
            workspace = request.user.current_workspace
            dashboards = Dashboard.objects.filter(
                workspace=workspace,
                is_active=True
            ).order_by('-updated_at')
            
            serializer = DashboardSerializer(
                dashboards, 
                many=True, 
                context={'request': request}
            )
            return Response(serializer.data)
            
        except Exception as e:
            logger.error(f"Error listing dashboards: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to list dashboards: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )