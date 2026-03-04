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
    """
    API endpoint for managing widgets on a specific dashboard
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    
    def post(self, request, dashboard_id):
        """Add a new widget to a dashboard"""
        try:
            print("\n" + "="*50)
            print("DEBUG: Creating new widget")
            print(f"Dashboard ID: {dashboard_id}")
            print(f"User: {request.user.email}")
            print(f"Request data: {request.data}")
            
            # Get the dashboard using current workspace from user
            workspace = request.user.current_workspace
            if not workspace:
                print("ERROR: No active workspace found")
                return Response(
                    {'error': 'No active workspace found'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            print(f"Workspace: {workspace.name} (ID: {workspace.id})")
            
            dashboard = get_object_or_404(
                Dashboard, 
                id=dashboard_id, 
                workspace=workspace,
                is_active=True
            )
            print(f"Dashboard found: {dashboard.name}")
            
            # Get the table if specified
            table_id = request.data.get('table_id')
            table = None
            if table_id:
                table = get_object_or_404(
                    DataTable,
                    id=table_id,
                    workspace=workspace,
                    is_active=True
                )
                print(f"Table found: {table.name} with {table.record_count} records")
                
                # Debug: Check if table has records
                record_count = table.records.filter(is_active=True).count()
                print(f"Actual record count: {record_count}")
                
                # Show first few records for debugging
                sample_records = table.records.filter(is_active=True)[:3]
                for i, record in enumerate(sample_records):
                    print(f"Sample record {i+1}: {record.data}")
            
            # Create widget data
            widget_data = {
                'dashboard': dashboard.id,
                'widget_type': request.data.get('widget_type', 'metric'),
                'title': request.data.get('title', 'New Widget'),
                'table': table.id if table else None,
                'query_config': request.data.get('query_config', {}),
                'viz_config': request.data.get('viz_config', {}),
                'position': request.data.get('position', {'x': 0, 'y': 0, 'w': 4, 'h': 4})
            }
            
            print(f"Widget data to create: {widget_data}")
            
            # Create widget
            serializer = WidgetSerializer(data=widget_data, context={'request': request})
            if serializer.is_valid():
                widget = serializer.save()
                print(f"Widget created with ID: {widget.id}")
                
                # Test the widget data immediately
                test_data = widget.get_data(limit=10)
                print(f"Test data from new widget: {test_data}")
                
                # Get the serialized widget with data - create a new serializer with the widget instance
                response_serializer = WidgetSerializer(widget, context={'request': request})
                response_data = response_serializer.data
                print(f"Response data widget_data: {response_data.get('widget_data')}")
                
                return Response(response_data, status=status.HTTP_201_CREATED)
            else:
                print(f"Widget validation failed: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            print(f"ERROR creating widget: {str(e)}")
            import traceback
            traceback.print_exc()
            return Response(
                {'error': f'Failed to create widget: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DashboardLayoutAPIView(APIView):
    """
    API endpoint for updating dashboard layout
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    
    def post(self, request, dashboard_id):
        """Update widget positions/layout"""
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
            
            with transaction.atomic():
                for widget_data in widgets_data:
                    widget_id = widget_data.get('id')
                    position = widget_data.get('position', {})
                    
                    if widget_id and position:
                        Widget.objects.filter(
                            id=widget_id,
                            dashboard=dashboard
                        ).update(position=position)
            
            logger.info(f"Layout updated for dashboard {dashboard_id} by {request.user.email}")
            return Response({'status': 'success'}, status=status.HTTP_200_OK)
            
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