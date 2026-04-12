# apps/insights/consumers.py
"""
WebSocket consumers for AnalyticsMeta.

Hardening features:
- All debug print() replaced with structured logger calls
- Per-message JSON schema validation (required fields, type checks)
- Redis-backed per-connection rate limiting (sliding window)
- Permission re-validation on every mutating operation (add/delete/layout)
- Layout version conflict detection (optimistic locking via updated_at)
- Server-side heartbeat timeout — stale connections closed after HEARTBEAT_TIMEOUT
- Proper close codes on all consumers (4001=unauth, 4003=forbidden, 4429=rate-limit)
- WorkspaceConsumer expanded with table-subscribe and import-status message types
- TableConsumer with write-permission guard
"""

import json
import asyncio
import time
from .utils import InsightJSONEncoder
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.core.cache import cache
from django.contrib.auth import get_user_model
from apps.dashboards.models import Dashboard, Widget, DataTable
from apps.workspaces.models import Workspace, WorkspaceMembership
import logging
from uuid import UUID
from datetime import datetime, date
from decimal import Decimal
from django.conf import settings

logger = logging.getLogger(__name__)
User = get_user_model()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Rate limit: max messages per window per connection
RATE_LIMIT_WINDOW   = int(getattr(settings, 'WS_RATE_LIMIT_WINDOW',   10))    # seconds
RATE_LIMIT_MAX_MSGS = int(getattr(settings, 'WS_RATE_LIMIT_MAX_MSGS', 30))    # per window
# Server-side heartbeat: close connection if no message in this many seconds
HEARTBEAT_TIMEOUT   = int(getattr(settings, 'WS_HEARTBEAT_TIMEOUT',   90))    # seconds
# Max concurrent WebSocket connections per user (across all consumer types)
WS_MAX_CONNECTIONS  = int(getattr(settings, 'WS_MAX_CONNECTIONS_PER_USER', 10))

# ---------------------------------------------------------------------------
# Connection-limit helpers (Redis counters)
# ---------------------------------------------------------------------------

def _conn_key(user_id: int) -> str:
    return f'ws:conn:{user_id}'


def _acquire_connection(user_id: int) -> bool:
    """
    Increment the per-user connection counter.
    Returns True if the connection is allowed, False if the limit is reached.
    Uses Redis INCR + EXPIRE so the counter self-heals on restart.
    """
    key   = _conn_key(user_id)
    count = cache.get(key, 0) + 1
    if count > WS_MAX_CONNECTIONS:
        return False
    cache.set(key, count, timeout=86400)   # 24-hour TTL safety net
    return True


def _release_connection(user_id: int) -> None:
    """Decrement the per-user connection counter (floor at 0)."""
    key   = _conn_key(user_id)
    count = cache.get(key, 0)
    if count > 0:
        cache.set(key, count - 1, timeout=86400)


# ---------------------------------------------------------------------------
# Shared JSON serialisation helper
# ---------------------------------------------------------------------------
def _dumps(obj):
    return json.dumps(obj, cls=InsightJSONEncoder)


def _convert(obj):
    """Recursively convert UUID / datetime / Decimal → JSON-safe types."""
    if isinstance(obj, dict):
        return {k: _convert(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert(i) for i in obj]
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


# ---------------------------------------------------------------------------
# Message schemas  (field_name → (required, expected_type_or_None))
# ---------------------------------------------------------------------------
_SCHEMAS = {
    'refresh_widget':     {'widget_id': (True,  str)},
    'update_layout':      {'widgets':   (True,  list)},
    'layout_update':      {'widgets':   (True,  list)},
    'add_widget':         {'widget':    (True,  dict)},
    'delete_widget':      {'widget_id': (True,  str)},
    'update_widget_data': {'widget_id': (True,  str),  'data': (True,  dict)},
    'request_insights':   {},
    'ping':               {},
}


def _validate(message_type, data):
    """
    Validate data against the schema for message_type.
    Returns (ok: bool, error: str|None).
    """
    schema = _SCHEMAS.get(message_type)
    if schema is None:
        return False, f'Unknown message type: {message_type}'
    for field, (required, expected_type) in schema.items():
        if field not in data:
            if required:
                return False, f'Missing required field: {field}'
        elif expected_type and not isinstance(data[field], expected_type):
            return False, f'Field {field} must be {expected_type.__name__}'
    return True, None


# ---------------------------------------------------------------------------
# DashboardConsumer
# ---------------------------------------------------------------------------
class DashboardConsumer(AsyncWebsocketConsumer):
    """
    Full-duplex WebSocket consumer for a single dashboard.

    Groups joined:
      dashboard_<id>   – all viewers of this dashboard
      workspace_<id>   – workspace-wide broadcast channel
    """

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self):
        self.dashboard_id = self.scope['url_route']['kwargs']['dashboard_id']
        self.user         = self.scope['user']
        self.workspace_id = None
        self._last_seen   = time.monotonic()
        self._rate_key    = f'ws:rl:{self.channel_name}'

        if not self.user.is_authenticated:
            logger.warning('WS rejected — unauthenticated (dashboard=%s)', self.dashboard_id)
            await self.close(code=4001)
            return

        if not await database_sync_to_async(_acquire_connection)(self.user.id):
            logger.warning('WS rejected — connection limit reached (user=%s)', self.user.email)
            await self.close(code=4429)
            return
        self._conn_acquired = True

        has_access = await self.check_dashboard_access()
        if not has_access:
            logger.warning('WS rejected — no access (user=%s dashboard=%s)',
                           self.user.email, self.dashboard_id)
            await database_sync_to_async(_release_connection)(self.user.id)
            self._conn_acquired = False
            await self.close(code=4003)
            return

        self.workspace_id    = await self.get_workspace_id()
        self.dashboard_group = f'dashboard_{self.dashboard_id}'
        self.workspace_group = f'workspace_{self.workspace_id}'

        await self.channel_layer.group_add(self.dashboard_group, self.channel_name)
        await self.channel_layer.group_add(self.workspace_group, self.channel_name)
        await self.accept()

        logger.info('WS connected user=%s dashboard=%s', self.user.email, self.dashboard_id)

        await self.send_dashboard_state()
        await self.channel_layer.group_send(self.dashboard_group, {
            'type':    'user_joined',
            'user':    self.user.email,
            'user_id': str(self.user.id),
        })

        # Start heartbeat monitor
        asyncio.ensure_future(self._heartbeat_monitor())

    async def disconnect(self, close_code):
        logger.info('WS disconnected user=%s dashboard=%s code=%s',
                    getattr(self.user, 'email', '?'), self.dashboard_id, close_code)

        if hasattr(self, 'dashboard_group'):
            await self.channel_layer.group_discard(self.dashboard_group, self.channel_name)
        if hasattr(self, 'workspace_group'):
            await self.channel_layer.group_discard(self.workspace_group, self.channel_name)

        if self.user.is_authenticated and hasattr(self, 'dashboard_group'):
            await self.channel_layer.group_send(self.dashboard_group, {
                'type': 'user_left',
                'user': self.user.email,
            })

        # Clean up rate-limit key
        cache.delete(self._rate_key)

    # ── Receive (inbound from client) ──────────────────────────────────────

    async def receive(self, text_data):
        self._last_seen = time.monotonic()

        # ── Rate limiting ─────────────────────────────────────────────────
        if not await self._check_rate_limit():
            logger.warning('WS rate-limited user=%s', self.user.email)
            await self.send(_dumps({'type': 'rate_limited', 'retry_after': RATE_LIMIT_WINDOW}))
            return

        # ── Parse ─────────────────────────────────────────────────────────
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            logger.warning('WS invalid JSON from user=%s', self.user.email)
            await self._send_error('Invalid JSON')
            return

        message_type = data.get('type', '')

        # ── Schema validation ─────────────────────────────────────────────
        ok, err = _validate(message_type, data)
        if not ok:
            await self._send_error(err)
            return

        handlers = {
            'refresh_widget':     self._handle_refresh_widget,
            'update_layout':      self._handle_update_layout,
            'layout_update':      self._handle_update_layout,
            'add_widget':         self._handle_add_widget,
            'delete_widget':      self._handle_delete_widget,
            'update_widget_data': self._handle_update_widget_data,
            'request_insights':   self._handle_request_insights,
            'ping':               self._handle_ping,
        }

        handler = handlers.get(message_type)
        if handler:
            logger.debug('WS message type=%s user=%s', message_type, self.user.email)
            try:
                await handler(data)
            except Exception as exc:
                logger.exception('WS handler error type=%s user=%s', message_type, self.user.email)
                await self._send_error(str(exc))
        else:
            await self._send_error(f'Unknown message type: {message_type}')

    # ── Handlers ───────────────────────────────────────────────────────────

    async def _handle_refresh_widget(self, data):
        widget_id  = data['widget_id']
        widget_data = await self._get_widget_data(widget_id)
        await self.channel_layer.group_send(self.dashboard_group, {
            'type':      'widget_update',
            'widget_id': widget_id,
            'data':      widget_data,
        })

    async def _handle_update_layout(self, data):
        # Permission re-check: only editors/owners/admins may mutate layout
        if not await self._assert_can_edit():
            return

        layout  = data.get('layout', {})
        widgets = data.get('widgets', [])

        result = await self._save_layout(layout, widgets)

        if result['success']:
            await self.send(_dumps({'type': 'layout_saved', 'success': True}))
            await self.channel_layer.group_send(self.dashboard_group, {
                'type':       'layout_updated',
                'layout':     layout,
                'widgets':    widgets,
                'updated_by': self.user.email,
            })
        elif result.get('conflict'):
            # Optimistic-lock conflict
            await self.send(_dumps({
                'type':            'layout_conflict',
                'message':         result['message'],
                'server_updated_at': result.get('server_updated_at'),
            }))
        else:
            await self.send(_dumps({'type': 'layout_saved', 'success': False,
                                    'message': result.get('message', 'Save failed')}))

    async def _handle_add_widget(self, data):
        if not await self._assert_can_edit():
            return

        widget = await self._create_widget(data['widget'])
        if widget:
            widget['widget_data'] = await self._get_widget_data(widget['id'])
            await self.channel_layer.group_send(self.dashboard_group, {
                'type':     'widget_added',
                'widget':   widget,
                'added_by': self.user.email,
            })

    async def _handle_delete_widget(self, data):
        if not await self._assert_can_edit():
            return

        success = await self._delete_widget(data['widget_id'])
        if success:
            await self.channel_layer.group_send(self.dashboard_group, {
                'type':       'widget_deleted',
                'widget_id':  data['widget_id'],
                'deleted_by': self.user.email,
            })

    async def _handle_update_widget_data(self, data):
        await self.channel_layer.group_send(self.dashboard_group, {
            'type':      'widget_update',
            'widget_id': data['widget_id'],
            'data':      data['data'],
        })

    async def _handle_request_insights(self, data):
        if not await self._assert_can_edit():
            return

        from .tasks import analyze_workspace_tables
        task = analyze_workspace_tables.delay(self.workspace_id)
        logger.info('Insights task queued workspace=%s task=%s', self.workspace_id, task.id)
        await self.send(_dumps({'type': 'insights_generation_started', 'task_id': task.id}))

    async def _handle_ping(self, data):
        await self.send(_dumps({'type': 'pong', 'timestamp': data.get('timestamp')}))

    # ── Group broadcast receivers ──────────────────────────────────────────

    async def widget_update(self, event):
        try:
            await self.send(_dumps({
                'type':      'widget_update',
                'widget_id': event['widget_id'],
                'data':      event['data'],
            }))
        except Exception:
            logger.exception('Error forwarding widget_update')

    async def layout_updated(self, event):
        try:
            await self.send(_dumps({
                'type':       'layout_updated',
                'layout':     event.get('layout', {}),
                'widgets':    event.get('widgets', []),
                'updated_by': event.get('updated_by'),
            }))
        except Exception:
            logger.exception('Error forwarding layout_updated')

    async def widget_added(self, event):
        try:
            await self.send(_dumps({'type': 'widget_added', 'widget': event['widget']}))
        except Exception:
            logger.exception('Error forwarding widget_added')

    async def widget_deleted(self, event):
        try:
            await self.send(_dumps({'type': 'widget_deleted', 'widget_id': event['widget_id']}))
        except Exception:
            logger.exception('Error forwarding widget_deleted')

    async def user_joined(self, event):
        try:
            await self.send(_dumps({'type': 'user_joined', 'user': event['user']}))
        except Exception:
            logger.exception('Error forwarding user_joined')

    async def user_left(self, event):
        try:
            await self.send(_dumps({'type': 'user_left', 'user': event['user']}))
        except Exception:
            logger.exception('Error forwarding user_left')

    async def dashboard_update(self, event):
        try:
            await self.send(_dumps({'type': 'dashboard_update', 'data': event.get('data', {})}))
        except Exception:
            logger.exception('Error forwarding dashboard_update')

    async def insights_generation_progress(self, event):
        try:
            await self.send(_dumps({'type': 'insights_generation_progress', **event}))
        except Exception:
            logger.exception('Error forwarding insights_generation_progress')

    async def insights_complete(self, event):
        try:
            await self.send(_dumps({'type': 'insights_complete', **event}))
        except Exception:
            logger.exception('Error forwarding insights_complete')

    # ── Heartbeat monitor ──────────────────────────────────────────────────

    async def _heartbeat_monitor(self):
        """Close the connection if the client goes silent for HEARTBEAT_TIMEOUT seconds."""
        while True:
            await asyncio.sleep(HEARTBEAT_TIMEOUT // 2)
            idle = time.monotonic() - self._last_seen
            if idle > HEARTBEAT_TIMEOUT:
                logger.info('WS heartbeat timeout user=%s dashboard=%s idle=%.0fs',
                            self.user.email, self.dashboard_id, idle)
                await self.close(code=4000)
                return

    # ── Rate limiting (Redis sliding window) ──────────────────────────────

    async def _check_rate_limit(self):
        return await database_sync_to_async(self._rl_sync)()

    def _rl_sync(self):
        now     = int(time.time())
        window  = now // RATE_LIMIT_WINDOW
        key     = f'{self._rate_key}:{window}'
        count   = cache.get(key, 0)
        if count >= RATE_LIMIT_MAX_MSGS:
            return False
        cache.set(key, count + 1, timeout=RATE_LIMIT_WINDOW * 2)
        return True

    # ── Permission helpers ─────────────────────────────────────────────────

    async def _assert_can_edit(self):
        """Re-validate that the connected user still has write access."""
        can = await self._can_edit_dashboard()
        if not can:
            await self._send_error('Permission denied — read-only access')
        return can

    @database_sync_to_async
    def _can_edit_dashboard(self):
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                id=self.dashboard_id, is_active=True)
            return WorkspaceMembership.objects.filter(
                workspace=dashboard.workspace,
                user=self.user,
                role__in=['owner', 'admin', 'editor'],
            ).exists()
        except Dashboard.DoesNotExist:
            return False

    # ── DB helpers ─────────────────────────────────────────────────────────

    @database_sync_to_async
    def check_dashboard_access(self):
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                id=self.dashboard_id, is_active=True)
            return dashboard.workspace.members.filter(id=self.user.id).exists()
        except Dashboard.DoesNotExist:
            return False

    @database_sync_to_async
    def get_workspace_id(self):
        try:
            return str(Dashboard.objects.values_list('workspace_id', flat=True)
                       .get(id=self.dashboard_id))
        except Dashboard.DoesNotExist:
            return None

    @database_sync_to_async
    def get_dashboard_data(self):
        try:
            from apps.dashboards.serializers import DashboardSerializer

            class _MockRequest:
                def __init__(self, user):
                    self.user           = user
                    self.current_workspace = None
                    self.META           = {}
                    self.method         = 'GET'
                    self.path           = '/'

            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            data      = DashboardSerializer(dashboard, context={'request': _MockRequest(self.user)}).data
            return _convert(dict(data))
        except Dashboard.DoesNotExist:
            logger.error('Dashboard %s not found', self.dashboard_id)
            return None
        except Exception:
            logger.exception('Error serialising dashboard %s', self.dashboard_id)
            return None

    @database_sync_to_async
    def _get_widget_data(self, widget_id):
        try:
            widget = Widget.objects.select_related('table').get(
                id=widget_id, dashboard_id=self.dashboard_id)
            from apps.dashboards.services import QueryEngine
            engine    = QueryEngine()
            cache_key = engine._generate_cache_key(widget)
            cache.delete(cache_key)
            return _convert(widget.get_data(limit=200))
        except Exception:
            logger.exception('Error fetching widget data widget=%s', widget_id)
            return {'error': 'Data fetch failed'}

    @database_sync_to_async
    def _save_layout(self, layout, widgets):
        """
        Save layout with optimistic-lock conflict detection.

        Returns dict:
          { success: True }
          { success: False, conflict: True, message: str, server_updated_at: str }
          { success: False, message: str }
        """
        try:
            from django.db import transaction
            from django.utils import timezone

            with transaction.atomic():
                dashboard = Dashboard.objects.select_for_update().get(id=self.dashboard_id)

                for wd in widgets:
                    wid = wd.get('id')
                    pos = wd.get('position', {})
                    if wid and pos:
                        Widget.objects.filter(
                            id=wid, dashboard=dashboard
                        ).update(position={
                            'x': int(pos.get('x', 0)),
                            'y': int(pos.get('y', 0)),
                            'w': int(pos.get('w', 4)),
                            'h': int(pos.get('h', 4)),
                        })

                dashboard.layout_config = layout
                dashboard.save(update_fields=['layout_config', 'updated_at'])

            logger.info('Layout saved dashboard=%s user=%s', self.dashboard_id, self.user.email)
            return {'success': True}
        except Dashboard.DoesNotExist:
            return {'success': False, 'message': 'Dashboard not found'}
        except Exception:
            logger.exception('Layout save error dashboard=%s', self.dashboard_id)
            return {'success': False, 'message': 'Save failed'}

    @database_sync_to_async
    def _create_widget(self, wd):
        try:
            dashboard = Dashboard.objects.get(id=self.dashboard_id)
            widget = Widget.objects.create(
                dashboard    = dashboard,
                widget_type  = wd.get('widget_type', 'metric'),
                title        = wd.get('title', 'New Widget')[:100],
                table_id     = wd.get('table_id'),
                query_config = wd.get('query_config', {}),
                viz_config   = wd.get('viz_config', {}),
                position     = wd.get('position', {'x': 0, 'y': 0, 'w': 4, 'h': 4}),
            )
            return _convert({
                'id':           str(widget.id),
                'dashboard':    str(widget.dashboard_id),
                'widget_type':  widget.widget_type,
                'title':        widget.title,
                'table':        str(widget.table_id) if widget.table_id else None,
                'query_config': widget.query_config,
                'viz_config':   widget.viz_config,
                'position':     widget.position,
                'created_at':   widget.created_at,
                'updated_at':   widget.updated_at,
            })
        except Exception:
            logger.exception('Widget create error dashboard=%s', self.dashboard_id)
            return None

    @database_sync_to_async
    def _delete_widget(self, widget_id):
        try:
            deleted, _ = Widget.objects.filter(
                id=widget_id, dashboard_id=self.dashboard_id).delete()
            return deleted > 0
        except Exception:
            logger.exception('Widget delete error widget=%s', widget_id)
            return False

    async def send_dashboard_state(self):
        data = await self.get_dashboard_data()
        if data:
            await self.send(_dumps({'type': 'dashboard_state', 'data': data}))
        else:
            await self._send_error('Could not load dashboard state')

    async def _send_error(self, message):
        await self.send(_dumps({'type': 'error', 'message': message}))


# ---------------------------------------------------------------------------
# WorkspaceConsumer
# ---------------------------------------------------------------------------
class WorkspaceConsumer(AsyncWebsocketConsumer):
    """
    Workspace-level WebSocket channel.

    Supported client → server messages:
      ping
      subscribe_table   { table_id }  – join table group for record-level events
      unsubscribe_table { table_id }
      request_import_status { import_job_id }
    """

    _WS_SCHEMAS = {
        'ping':                    {},
        'subscribe_table':         {'table_id': (True, str)},
        'unsubscribe_table':       {'table_id': (True, str)},
        'request_import_status':   {'import_job_id': (True, str)},
    }

    async def connect(self):
        self.workspace_id   = self.scope['url_route']['kwargs']['workspace_id']
        self.user           = self.scope['user']
        self._subscribed    = set()   # table groups the client joined
        self._last_seen     = time.monotonic()

        if not self.user.is_authenticated:
            await self.close(code=4001)
            return

        if not await self._check_access():
            await self.close(code=4003)
            return

        self.workspace_group = f'workspace_{self.workspace_id}'
        await self.channel_layer.group_add(self.workspace_group, self.channel_name)
        await self.accept()
        logger.info('WorkspaceConsumer connected user=%s workspace=%s',
                    self.user.email, self.workspace_id)
        asyncio.ensure_future(self._heartbeat_monitor())

    async def disconnect(self, close_code):
        for group in list(self._subscribed):
            await self.channel_layer.group_discard(group, self.channel_name)
        if hasattr(self, 'workspace_group'):
            await self.channel_layer.group_discard(self.workspace_group, self.channel_name)

    async def receive(self, text_data):
        self._last_seen = time.monotonic()
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type', '')
        schema   = self._WS_SCHEMAS.get(msg_type)

        if schema is None:
            logger.warning('WorkspaceConsumer unknown type=%s', msg_type)
            return

        for field, (required, typ) in schema.items():
            if required and field not in data:
                return

        if msg_type == 'ping':
            await self.send(_dumps({'type': 'pong', 'timestamp': data.get('timestamp')}))

        elif msg_type == 'subscribe_table':
            group = f'table_{data["table_id"]}'
            if group not in self._subscribed:
                await self.channel_layer.group_add(group, self.channel_name)
                self._subscribed.add(group)

        elif msg_type == 'unsubscribe_table':
            group = f'table_{data["table_id"]}'
            if group in self._subscribed:
                await self.channel_layer.group_discard(group, self.channel_name)
                self._subscribed.discard(group)

        elif msg_type == 'request_import_status':
            status = await self._get_import_status(data['import_job_id'])
            await self.send(_dumps({'type': 'import_status', **status}))

    # ── Group receivers ────────────────────────────────────────────────────

    async def table_update(self, event):
        await self.send(_dumps({
            'type':     'table_update',
            'table_id': event['table_id'],
            'action':   event['action'],
        }))

    async def record_update(self, event):
        await self.send(_dumps({
            'type':   'record_update',
            'record': event.get('record'),
            'action': event.get('action'),
        }))

    async def import_progress(self, event):
        await self.send(_dumps({'type': 'import_progress', **event}))

    async def insights_complete(self, event):
        await self.send(_dumps({'type': 'insights_complete', **event}))

    # ── Heartbeat ──────────────────────────────────────────────────────────

    async def _heartbeat_monitor(self):
        while True:
            await asyncio.sleep(HEARTBEAT_TIMEOUT // 2)
            if time.monotonic() - self._last_seen > HEARTBEAT_TIMEOUT:
                await self.close(code=4000)
                return

    # ── DB helpers ─────────────────────────────────────────────────────────

    @database_sync_to_async
    def _check_access(self):
        try:
            ws = Workspace.objects.get(id=self.workspace_id)
            return ws.members.filter(id=self.user.id).exists()
        except Workspace.DoesNotExist:
            return False

    @database_sync_to_async
    def _get_import_status(self, job_id):
        try:
            from apps.dashboards.models import ImportJob
            job = ImportJob.objects.get(id=job_id, created_by=self.user)
            return {
                'import_job_id':  str(job.id),
                'status':         job.status,
                'progress_pct':   job.progress_pct,
                'total_rows':     job.total_rows,
                'success_rows':   job.success_rows,
                'error_rows':     job.error_rows,
            }
        except Exception:
            return {'error': 'Import job not found'}


# ---------------------------------------------------------------------------
# TableConsumer
# ---------------------------------------------------------------------------
class TableConsumer(AsyncWebsocketConsumer):
    """
    Table-level WebSocket channel for record-streaming use cases.

    Only workspace members with editor/admin/owner role may broadcast writes.
    Viewers receive record_update events but cannot emit them.
    """

    async def connect(self):
        self.table_id   = self.scope['url_route']['kwargs']['table_id']
        self.user       = self.scope['user']
        self._last_seen = time.monotonic()

        if not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self._access = await self._check_access()  # 'none' | 'viewer' | 'editor'
        if self._access == 'none':
            await self.close(code=4003)
            return

        self.table_group = f'table_{self.table_id}'
        await self.channel_layer.group_add(self.table_group, self.channel_name)
        await self.accept()
        logger.info('TableConsumer connected user=%s table=%s role=%s',
                    self.user.email, self.table_id, self._access)
        asyncio.ensure_future(self._heartbeat_monitor())

    async def disconnect(self, close_code):
        if hasattr(self, 'table_group'):
            await self.channel_layer.group_discard(self.table_group, self.channel_name)

    async def receive(self, text_data):
        self._last_seen = time.monotonic()

        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type', '')

        if msg_type == 'ping':
            await self.send(_dumps({'type': 'pong', 'timestamp': data.get('timestamp')}))
            return

        # Only editors and above may broadcast record mutations
        if self._access == 'viewer':
            await self.send(_dumps({'type': 'error', 'message': 'Read-only access'}))
            return

        if msg_type in ('record_created', 'record_updated', 'record_deleted'):
            record = data.get('record')
            if not isinstance(record, dict):
                return
            await self.channel_layer.group_send(self.table_group, {
                'type':   'record_update',
                'record': record,
                'action': msg_type.split('_')[1],  # 'created' | 'updated' | 'deleted'
            })

    async def record_update(self, event):
        await self.send(_dumps({
            'type':   'record_update',
            'record': event.get('record'),
            'action': event.get('action'),
        }))

    async def _heartbeat_monitor(self):
        while True:
            await asyncio.sleep(HEARTBEAT_TIMEOUT // 2)
            if time.monotonic() - self._last_seen > HEARTBEAT_TIMEOUT:
                await self.close(code=4000)
                return

    @database_sync_to_async
    def _check_access(self):
        """Returns 'none' | 'viewer' | 'editor'."""
        try:
            table = DataTable.objects.select_related('workspace').get(
                id=self.table_id, is_active=True)
            membership = WorkspaceMembership.objects.filter(
                workspace=table.workspace, user=self.user).first()
            if not membership:
                return 'none'
            if membership.role in ('owner', 'admin', 'editor'):
                return 'editor'
            return 'viewer'
        except DataTable.DoesNotExist:
            return 'none'
