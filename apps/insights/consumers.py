# apps/insights/consumers.py
import json
from .utils import InsightJSONEncoder
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.core.cache import cache
from django.contrib.auth import get_user_model
from apps.dashboards.models import Dashboard, Widget, Workspace, DataTable
import logging

logger = logging.getLogger(__name__)

User = get_user_model()

class DashboardConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time dashboard updates
    Handles:
    - Live widget data updates
    - Layout changes
    - User collaboration
    - Auto-refresh on data changes
    """
    
    async def connect(self):
        """Handle new WebSocket connection"""
        self.dashboard_id = self.scope['url_route']['kwargs']['dashboard_id']
        self.user = self.scope['user']
        self.workspace_id = None
        
        print(f"\n🔌 WebSocket connection attempt for dashboard {self.dashboard_id}")
        print(f"User authenticated: {self.user.is_authenticated}")
        
        # Check authentication
        if not self.user.is_authenticated:
            print("❌ User not authenticated")
            await self.close(code=4001)
            return
        
        # Check dashboard access
        has_access = await self.check_dashboard_access()
        if not has_access:
            print(f"❌ User {self.user.email} no access to dashboard {self.dashboard_id}")
            await self.close(code=4003)
            return
        
        # Get workspace for this dashboard
        self.workspace_id = await self.get_workspace_id()
        
        # Join room groups
        self.dashboard_group = f'dashboard_{self.dashboard_id}'
        self.workspace_group = f'workspace_{self.workspace_id}'
        
        await self.channel_layer.group_add(
            self.dashboard_group,
            self.channel_name
        )
        
        await self.channel_layer.group_add(
            self.workspace_group,
            self.channel_name
        )
        
        await self.accept()
        print(f"✅ WebSocket connected for dashboard {self.dashboard_id}")
        
        # Send initial dashboard state
        await self.send_dashboard_state()
        
        # Notify others that user joined
        await self.channel_layer.group_send(
            self.dashboard_group,
            {
                'type': 'user_joined',
                'user': self.user.email,
                'user_id': str(self.user.id)
            }
        )
    
    async def disconnect(self, close_code):
        """Handle disconnection"""
        print(f"🔌 WebSocket disconnected for dashboard {self.dashboard_id} (code: {close_code})")
        
        # Leave groups
        if hasattr(self, 'dashboard_group'):
            await self.channel_layer.group_discard(
                self.dashboard_group,
                self.channel_name
            )
        
        if hasattr(self, 'workspace_group'):
            await self.channel_layer.group_discard(
                self.workspace_group,
                self.channel_name
            )
        
        # Notify others
        if self.user.is_authenticated:
            await self.channel_layer.group_send(
                self.dashboard_group,
                {
                    'type': 'user_left',
                    'user': self.user.email
                }
            )
    
    async def receive(self, text_data):
        """Handle messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            print(f"📨 Received message: {message_type}")
            
            handlers = {
                'refresh_widget': self.handle_refresh_widget,
                'update_layout': self.handle_update_layout,
                'add_widget': self.handle_add_widget,
                'delete_widget': self.handle_delete_widget,
                'update_widget_data': self.handle_update_widget_data,
                'request_insights': self.handle_request_insights,
                'ping': self.handle_ping,
            }
            
            handler = handlers.get(message_type)
            if handler:
                await handler(data)
            else:
                print(f"⚠️ Unknown message type: {message_type}")
                
        except json.JSONDecodeError:
            print("❌ Invalid JSON received")
        except Exception as e:
            print(f"❌ Error handling message: {str(e)}")
            await self.send_error(str(e))
    
    async def handle_refresh_widget(self, data):
        """Refresh a specific widget"""
        widget_id = data.get('widget_id')
        if not widget_id:
            return
        
        print(f"🔄 Refreshing widget {widget_id}")
        
        # Get fresh data
        widget_data = await self.get_widget_data(widget_id)
        
        # Send update to all clients in this dashboard
        await self.channel_layer.group_send(
            self.dashboard_group,
            {
                'type': 'widget_update',
                'widget_id': widget_id,
                'data': widget_data
            }
        )
    
    async def handle_update_layout(self, data):
        """Update dashboard layout"""
        layout = data.get('layout', {})
        widgets = data.get('widgets', [])
        
        print(f"📐 Updating layout with {len(widgets)} widgets")
        
        # Save to database
        success = await self.save_layout(layout, widgets)
        
        if success:
            # Broadcast to all clients
            await self.channel_layer.group_send(
                self.dashboard_group,
                {
                    'type': 'layout_update',
                    'layout': layout,
                    'widgets': widgets,
                    'updated_by': self.user.email
                }
            )
    
    async def handle_add_widget(self, data):
        """Add a new widget"""
        widget_data = data.get('widget', {})
        
        print(f"➕ Adding new widget: {widget_data.get('title')}")
        
        # Create widget in database
        widget = await self.create_widget(widget_data)
        
        if widget:
            # Get initial data
            widget['widget_data'] = await self.get_widget_data(widget['id'])
            
            # Broadcast to all clients
            await self.channel_layer.group_send(
                self.dashboard_group,
                {
                    'type': 'widget_added',
                    'widget': widget,
                    'added_by': self.user.email
                }
            )
    
    async def handle_delete_widget(self, data):
        """Delete a widget"""
        widget_id = data.get('widget_id')
        
        if not widget_id:
            return
        
        print(f"🗑️ Deleting widget {widget_id}")
        
        # Delete from database
        success = await self.delete_widget(widget_id)
        
        if success:
            # Broadcast to all clients
            await self.channel_layer.group_send(
                self.dashboard_group,
                {
                    'type': 'widget_deleted',
                    'widget_id': widget_id,
                    'deleted_by': self.user.email
                }
            )
    
    async def handle_update_widget_data(self, data):
        """Update widget data (from background tasks)"""
        widget_id = data.get('widget_id')
        widget_data = data.get('data', {})
        
        # Broadcast to all clients
        await self.channel_layer.group_send(
            self.dashboard_group,
            {
                'type': 'widget_update',
                'widget_id': widget_id,
                'data': widget_data
            }
        )
    
    async def handle_request_insights(self, data):
        """Request AI insights generation"""
        print("🤖 Generating AI insights...")
        
        # Trigger background task
        from .tasks import analyze_workspace_tables
        task = analyze_workspace_tables.delay(self.workspace_id)
        
        # Acknowledge
        await self.send(text_data=json.dumps({
            'type': 'insights_generation_started',
            'task_id': task.id
        }))
    
    async def handle_ping(self, data):
        """Respond to ping to keep connection alive"""
        await self.send(text_data=json.dumps({
            'type': 'pong',
            'timestamp': data.get('timestamp')
        }))
    
    async def dashboard_update(self, event):
        """Handle dashboard update messages from channel layer"""
        try:
            # Send the dashboard update to the WebSocket client
            await self.send(text_data=json.dumps({
                'type': 'dashboard_update',
                'data': event['data']
            }))
            logger.info(f"Sent dashboard update to client for {self.dashboard_id}")
        except Exception as e:
            logger.error(f"Error sending dashboard update: {e}")

    # Message handlers for group broadcasts
    async def widget_update(self, event):
        """Handle widget update messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'widget_update',
                'widget_id': event['widget_id'],
                'data': event['data']
            }))
        except Exception as e:
            logger.error(f"Error sending widget update: {e}")
    
    async def layout_update(self, event):
        """Handle layout update messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'layout_update',
                'layout': event['layout'],
                'widgets': event.get('widgets', [])
            }))
        except Exception as e:
            logger.error(f"Error sending layout update: {e}")
    
    async def widget_added(self, event):
        """Handle widget added messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'widget_added',
                'widget': event['widget']
            }))
        except Exception as e:
            logger.error(f"Error sending widget added: {e}")
    
    async def widget_deleted(self, event):
        """Handle widget deleted messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'widget_deleted',
                'widget_id': event['widget_id']
            }))
        except Exception as e:
            logger.error(f"Error sending widget deleted: {e}")
    
    async def user_joined(self, event):
        """Handle user joined messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'user_joined',
                'user': event['user']
            }))
        except Exception as e:
            logger.error(f"Error sending user joined: {e}")
    
    async def user_left(self, event):
        """Handle user left messages from channel layer"""
        try:
            await self.send(text_data=json.dumps({
                'type': 'user_left',
                'user': event['user']
            }))
        except Exception as e:
            logger.error(f"Error sending user left: {e}")
    
    # Database helpers
    @database_sync_to_async
    def check_dashboard_access(self):
        """Check if user has access to this dashboard"""
        try:
            dashboard = Dashboard.objects.get(
                id=self.dashboard_id,
                is_active=True
            )
            return dashboard.workspace.members.filter(id=self.user.id).exists()
        except Dashboard.DoesNotExist:
            return False
    
    @database_sync_to_async
    def get_workspace_id(self):
        """Get workspace ID for this dashboard"""
        try:
            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            return str(dashboard.workspace.id)
        except:
            return None
    

    @database_sync_to_async
    def get_dashboard_data(self):
        """Get dashboard data synchronously (runs in thread pool)"""
        try:
            from .serializers import DashboardSerializer
        
            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            serializer = DashboardSerializer(dashboard, context={'request': self.scope})
        
            # Get the serialized data
            data = serializer.data
        
            # Test JSON serialization
            json.dumps(data, cls=InsightJSONEncoder)
        
            return data
        except Dashboard.DoesNotExist:
            logger.error(f"Dashboard {self.dashboard_id} not found")
            return None
        except Exception as e:
            logger.error(f"Error getting dashboard data: {e}")
            return None

    async def send_dashboard_state(self):
        """Send current dashboard state"""
        try:
            # Get data in thread pool
            data = await self.get_dashboard_data()
        
            if data:
                # Ensure data is JSON serializable
                try:
                    # Test serialization
                    json_str = json.dumps(data, cls=InsightJSONEncoder)
                
                    # Send via channel layer
                    await self.channel_layer.group_send(
                        self.dashboard_group,
                        {
                            'type': 'dashboard_update',
                            'data': json.loads(json_str)  # Parse back to dict for sending
                        }
                    )
                    logger.info(f"Dashboard state sent for {self.dashboard_id}")
                except Exception as e:
                    logger.error(f"JSON serialization error: {e}")
                    # Send error message to client
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': f'Serialization error: {str(e)}'
                    }))
            else:
                logger.warning(f"No data to send for dashboard {self.dashboard_id}")
            
        except Exception as e:
            logger.error(f"Error sending dashboard state: {str(e)}")

    @database_sync_to_async
    def get_widget_data(self, widget_id):
        """Get fresh data for a widget"""
        try:
            widget = Widget.objects.get(id=widget_id, dashboard_id=self.dashboard_id)
            
            # Clear cache
            from apps.dashboards.services import QueryEngine
            engine = QueryEngine()
            cache_key = engine._generate_cache_key(widget)
            cache.delete(cache_key)
            
            # Get fresh data
            return widget.get_data(limit=100)
        except Exception as e:
            logger.error(f"Error getting widget data: {str(e)}")
            return {'error': str(e)}
    
    @database_sync_to_async
    def save_layout(self, layout, widgets):
        """Save dashboard layout"""
        try:
            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            dashboard.layout_config = layout
            dashboard.save(update_fields=['layout_config'])
            
            # Update widget positions
            for widget_data in widgets:
                widget_id = widget_data.get('id')
                position = widget_data.get('position', {})
                
                if widget_id and position:
                    Widget.objects.filter(
                        id=widget_id,
                        dashboard=dashboard
                    ).update(position=position)
            
            return True
        except Exception as e:
            logger.error(f"Error saving layout: {str(e)}")
            return False
    
    @database_sync_to_async
    def create_widget(self, widget_data):
        """Create a new widget"""
        try:
            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            
            widget = Widget.objects.create(
                dashboard=dashboard,
                widget_type=widget_data.get('widget_type', 'metric'),
                title=widget_data.get('title', 'New Widget'),
                table_id=widget_data.get('table_id'),
                query_config=widget_data.get('query_config', {}),
                viz_config=widget_data.get('viz_config', {}),
                position=widget_data.get('position', {'x': 0, 'y': 0, 'w': 4, 'h': 4})
            )
            
            # Return serialized widget
            from .serializers import WidgetSerializer
            serializer = WidgetSerializer(widget, context={'request': self.scope})
            return serializer.data
        except Exception as e:
            logger.error(f"Error creating widget: {str(e)}")
            return None
    
    @database_sync_to_async
    def delete_widget(self, widget_id):
        """Delete a widget"""
        try:
            Widget.objects.filter(
                id=widget_id,
                dashboard_id=self.dashboard_id
            ).delete()
            return True
        except Exception as e:
            logger.error(f"Error deleting widget: {str(e)}")
            return False
    
    async def send_error(self, message):
        """Send error message to client"""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message
        }))

    


class WorkspaceConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for workspace-level updates
    Useful for notifications across multiple dashboards
    """
    
    async def connect(self):
        self.workspace_id = self.scope['url_route']['kwargs']['workspace_id']
        self.user = self.scope['user']
        
        if not self.user.is_authenticated:
            await self.close()
            return
        
        # Check workspace access
        has_access = await self.check_workspace_access()
        if not has_access:
            await self.close()
            return
        
        self.workspace_group = f'workspace_{self.workspace_id}'
        
        await self.channel_layer.group_add(
            self.workspace_group,
            self.channel_name
        )
        
        await self.accept()
    
    async def disconnect(self, close_code):
        if hasattr(self, 'workspace_group'):
            await self.channel_layer.group_discard(
                self.workspace_group,
                self.channel_name
            )
    
    async def receive(self, text_data):
        """Handle messages from client"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
        
            logger.info(f"📨 Received message: {message_type}")
        
            # Handle different message types
            if message_type == 'ping':
                await self.handle_ping(data)
            elif message_type == 'request_state':
                await self.send_dashboard_state()
            elif message_type == 'refresh_widget':
                await self.handle_refresh_widget(data)
            elif message_type == 'update_layout':
                await self.handle_update_layout(data)
            elif message_type == 'add_widget':
                await self.handle_add_widget(data)
            elif message_type == 'delete_widget':
                await self.handle_delete_widget(data)
            else:
                logger.warning(f"Unknown message type: {message_type}")
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Unknown message type: {message_type}'
                }))
            
        except json.JSONDecodeError:
            logger.error("Invalid JSON received")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': str(e)
            }))
            
    async def table_update(self, event):
        """Send table update notification"""
        await self.send(text_data=json.dumps({
            'type': 'table_update',
            'table_id': event['table_id'],
            'action': event['action']
        }))
    
    @database_sync_to_async
    def check_workspace_access(self):
        try:
            workspace = Workspace.objects.get(id=self.workspace_id)
            return workspace.members.filter(id=self.user.id).exists()
        except Workspace.DoesNotExist:
            return False


class TableConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for table-level updates
    Used for real-time data entry and updates
    """
    
    async def connect(self):
        self.table_id = self.scope['url_route']['kwargs']['table_id']
        self.user = self.scope['user']
        
        if not self.user.is_authenticated:
            await self.close()
            return
        
        # Check table access
        has_access = await self.check_table_access()
        if not has_access:
            await self.close()
            return
        
        self.table_group = f'table_{self.table_id}'
        
        await self.channel_layer.group_add(
            self.table_group,
            self.channel_name
        )
        
        await self.accept()
    
    async def disconnect(self, close_code):
        if hasattr(self, 'table_group'):
            await self.channel_layer.group_discard(
                self.table_group,
                self.channel_name
            )
    
    async def receive(self, text_data):
        data = json.loads(text_data)
        message_type = data.get('type')
        
        if message_type == 'record_created':
            # New record added
            await self.channel_layer.group_send(
                self.table_group,
                {
                    'type': 'record_update',
                    'record': data.get('record'),
                    'action': 'created'
                }
            )
        
        elif message_type == 'record_updated':
            # Record updated
            await self.channel_layer.group_send(
                self.table_group,
                {
                    'type': 'record_update',
                    'record': data.get('record'),
                    'action': 'updated'
                }
            )
    
    async def record_update(self, event):
        """Send record update to clients"""
        await self.send(text_data=json.dumps({
            'type': 'record_update',
            'record': event['record'],
            'action': event['action']
        }))
    
    @database_sync_to_async
    def check_table_access(self):
        try:
            table = DataTable.objects.get(id=self.table_id, is_active=True)
            return table.workspace.members.filter(id=self.user.id).exists()
        except DataTable.DoesNotExist:
            return False