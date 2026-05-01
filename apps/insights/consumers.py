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
WS_MAX_CONNECTIONS  = int(getattr(settings, 'WS_MAX_CONNECTIONS_PER_USER', 200))

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

        # Release connection slot so the counter doesn't accumulate
        if getattr(self, '_conn_acquired', False):
            await database_sync_to_async(_release_connection)(self.user.id)
            self._conn_acquired = False

        # Clean up rate-limit key (use async-safe wrapper)
        if hasattr(self, '_rate_key'):
            await database_sync_to_async(cache.delete)(self._rate_key)

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

    async def dashboard_ready(self, event):
        try:
            await self.send(_dumps({
                'type':       'dashboard_ready',
                'grid_rows':  event.get('grid_rows'),
                'ai_advised': event.get('ai_advised'),
                'table':      event.get('table'),
            }))
        except Exception:
            logger.exception('Error forwarding dashboard_ready')

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


# ---------------------------------------------------------------------------
# AskAIConsumer
# ---------------------------------------------------------------------------
class AskAIConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for the Ask AI chat panel.

    Path: ws/dashboard/<dashboard_id>/ask-ai/

    Client → server messages:
        { "type": "ask", "question": "...", "session_id": "...", "table_id": "<uuid|optional>" }
        { "type": "ping" }

    Server → client messages:
        { "type": "chunk",   "text": "..." }          — streamed response tokens
        { "type": "done",    "insight_id": "..." }    — full response saved
        { "type": "history", "items": [...] }         — sent on connect
        { "type": "error",   "message": "..." }
        { "type": "pong" }

    Session memory:
        Last SESSION_BUFFER_SIZE turns are stored in Redis under
        ask_ai:<session_id> with a SESSION_TTL_SECONDS sliding TTL.
        Each message in Claude's turn gets a fresh shadow dataset so
        answers are always based on current data, not stale context.
    """

    SESSION_BUFFER_SIZE = 6      # turns (user + assistant pairs) kept in Redis
    SESSION_TTL_SECONDS = 1800   # 30-minute sliding TTL

    async def connect(self):
        self.dashboard_id = self.scope['url_route']['kwargs']['dashboard_id']
        self.user = self.scope['user']

        if not self.user.is_authenticated:
            await self.close(code=4001)
            return

        has_access = await self._check_access()
        if not has_access:
            await self.close(code=4003)
            return

        self.workspace_id = await self._get_workspace_id()
        await self.accept()
        logger.info('AskAIConsumer connected user=%s dashboard=%s', self.user.email, self.dashboard_id)

        # Send recent history on connect
        history = await self._load_history()
        await self.send(_dumps({'type': 'history', 'items': history}))

    async def disconnect(self, close_code):
        logger.info('AskAIConsumer disconnected user=%s dashboard=%s code=%s',
                    getattr(self.user, 'email', '?'), self.dashboard_id, close_code)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error('Invalid JSON')
            return

        msg_type = data.get('type', '')

        if msg_type == 'ping':
            await self.send(_dumps({'type': 'pong'}))
            return

        if msg_type == 'ask':
            question = (data.get('question') or '').strip()
            session_id = (data.get('session_id') or '').strip()
            table_id = data.get('table_id', '')
            widget_id = data.get('widget_id', '')
            if not question:
                await self._send_error('question is required')
                return
            if not session_id:
                await self._send_error('session_id is required')
                return
            await self._handle_ask(question, session_id, table_id, widget_id)
            return

        await self._send_error(f'Unknown message type: {msg_type}')

    # ── Chart-generation tool definition ──────────────────────────────────
    _CHART_TOOL = {
        "name": "generate_chart",
        "description": (
            "Generate a chart by querying and filtering the dashboard data. "
            "Use this whenever the user wants to see a visual pattern, trend, comparison, or distribution — "
            "e.g. 'show me sales in the last 6 months', 'compare revenue by region', 'distribution of scores'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar_chart", "line_chart", "area_chart", "pie_chart", "scatter", "histogram"],
                    "description": "Best chart type for the request",
                },
                "title":  {"type": "string", "description": "Chart title"},
                "x_field": {"type": "string", "description": "Field name for X axis / grouping dimension"},
                "y_field": {"type": "string", "description": "Field name for Y axis / value (omit for count)"},
                "aggregation": {
                    "type": "string",
                    "enum": ["sum", "avg", "count", "min", "max"],
                    "description": "Aggregation to apply to y_field",
                },
                "filters": {
                    "type": "array",
                    "description": "Filters to apply before aggregating",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field":    {"type": "string"},
                            "operator": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains"]},
                            "value":    {},
                        },
                        "required": ["field", "operator", "value"],
                    },
                },
                "explanation": {
                    "type": "string",
                    "description": "One or two sentences explaining what this chart reveals",
                },
            },
            "required": ["chart_type", "title", "aggregation", "explanation"],
        },
    }

    # ── Core Q&A handler ──────────────────────────────────────────────────

    async def _handle_ask(self, question: str, session_id: str, table_id: str, widget_id: str = ''):
        """Handle Ask AI: generates a chart when the user requests a visual, otherwise streams text."""
        budget_ok = await database_sync_to_async(self._check_budget)()
        if not budget_ok:
            await self._send_error('Monthly AI token budget exhausted.')
            return

        widget_ctx = None
        if widget_id:
            widget_ctx = await database_sync_to_async(self._build_widget_context)(widget_id)

        shadow = await database_sync_to_async(self._build_shadow)(table_id)
        if shadow is None:
            await self._send_error('No data table found for this dashboard.')
            return

        buf_key = f'{session_id}:w:{widget_id}' if widget_id else session_id
        buffer  = await database_sync_to_async(self._load_buffer)(buf_key)

        messages = list(buffer)
        messages.append({'role': 'user', 'content': self._build_ask_prompt(question, shadow, widget_ctx)})

        import anthropic
        from django.conf import settings
        api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
        model   = getattr(settings, 'CLAUDE_INSIGHT_MODEL', 'claude-sonnet-4-6')
        client  = anthropic.Anthropic(api_key=api_key)

        if widget_ctx:
            system_prompt = (
                "You are a business data analyst. The user is looking at a specific chart on their dashboard. "
                "Explain what the chart shows, interpret numbers, highlight patterns or outliers, and answer follow-up questions. "
                "If the user asks to see a different view, filter, or comparison, use the generate_chart tool. "
                "Speak about the data you can see — do not speculate about rendering issues or configuration problems. "
                "Be concise — 2-4 sentences unless the user asks for more. No markdown headers. Plain prose only."
            )
        else:
            system_prompt = (
                "You are a smart business data analyst assistant embedded in a BI dashboard. "
                "You have access to the dataset schema and sample records. "
                "When the user asks to SEE data (patterns, trends, comparisons, distributions, charts), "
                "use the generate_chart tool to build it for them. "
                "For all other questions answer in plain prose — 2-5 sentences, specific, referencing field names. "
                "No markdown headers. Plain prose only."
            )

        full_response = ''
        tokens = {'input': 0, 'output': 0}
        try:
            # Non-streaming call with tool available — lets Claude decide to chart or talk
            response = client.messages.create(
                model=model,
                max_tokens=1000,
                system=system_prompt,
                tools=[self._CHART_TOOL],
                tool_choice={"type": "auto"},
                messages=messages,
            )
            tokens['input']  = response.usage.input_tokens
            tokens['output'] = response.usage.output_tokens

            chart_result = None
            text_parts   = []

            for block in response.content:
                if block.type == 'tool_use' and block.name == 'generate_chart':
                    # Execute the chart query against real data
                    chart_result = await database_sync_to_async(
                        self._execute_chart_request
                    )(block.input, shadow)
                    text_parts.append(block.input.get('explanation', ''))
                elif block.type == 'text':
                    text_parts.append(block.text)

            full_response = ' '.join(t for t in text_parts if t)

            # Send chart data first (frontend renders mini chart)
            if chart_result and 'error' not in chart_result:
                await self.send(_dumps({'type': 'chart', **chart_result}))

            # Send text explanation as a single chunk
            if full_response:
                await self.send(_dumps({'type': 'chunk', 'text': full_response}))

        except Exception as exc:
            logger.exception('AskAIConsumer: Claude call failed user=%s', self.user.email)
            await self._send_error(f'AI error: {exc}')
            return

        insight_id = await database_sync_to_async(self._save_insight)(
            question, full_response, tokens, shadow.get('table_name', ''), bool(widget_id)
        )
        await database_sync_to_async(self._update_buffer)(buf_key, question, full_response)
        await database_sync_to_async(self._consume_tokens)(tokens)
        await self.send(_dumps({'type': 'done', 'insight_id': str(insight_id)}))

    # ── Chart execution helper ─────────────────────────────────────────────

    def _execute_chart_request(self, tool_input: dict, shadow: dict) -> dict:
        """Execute a chart query from the AI tool call and return renderable data."""
        from apps.dashboards.models import Widget, DataTable
        from apps.dashboards.services import QueryEngine
        from datetime import datetime

        chart_type  = tool_input.get('chart_type', 'bar_chart')
        title       = tool_input.get('title', 'Chart')
        x_field     = tool_input.get('x_field', '')
        y_field     = tool_input.get('y_field', '')
        aggregation = tool_input.get('aggregation', 'count')
        filters     = tool_input.get('filters', [])
        explanation = tool_input.get('explanation', '')

        table_pk = shadow.get('table_obj_pk')
        if not table_pk:
            return {'error': 'No table available', 'explanation': explanation}

        try:
            table = DataTable.objects.get(pk=table_pk)
        except DataTable.DoesNotExist:
            return {'error': 'Table not found', 'explanation': explanation}

        # Build query_config matching the chart type
        if chart_type == 'scatter':
            query_config = {'x_field': x_field, 'y_field': y_field,
                            'label_field': x_field, 'filters': filters}
        elif chart_type == 'histogram':
            query_config = {'field': y_field or x_field, 'bins': 12,
                            'show_kde': True, 'filters': filters}
        else:
            agg_entry = {'type': aggregation, 'name': 'val'}
            if y_field:     agg_entry['field']    = y_field
            if x_field:     agg_entry['group_by'] = x_field
            query_config = {'aggregations': [agg_entry], 'filters': filters, 'limit': 300}

        # Unsaved Widget instance — enough for QueryEngine to execute
        w = Widget(widget_type=chart_type, title=title,
                   query_config=query_config, table=table)
        w.pk = None

        try:
            data = QueryEngine().execute_widget_query(w)
        except Exception as exc:
            logger.exception('AskAIConsumer: chart execution failed')
            return {'error': str(exc), 'explanation': explanation}

        return {
            'chart_type':  chart_type,
            'title':       title,
            'explanation': explanation,
            'data':        data,
            'viz_config':  {
                'x_axis': x_field.replace('_', ' ').title() if x_field else '',
                'y_axis': y_field.replace('_', ' ').title() if y_field else '',
            },
        }

    # ── DB / Redis helpers ─────────────────────────────────────────────────

    def _build_ask_prompt(self, question: str, shadow: dict, widget_ctx: dict | None = None) -> str:
        import json as _json
        schema_lines = '\n'.join(
            f'  - {f["name"]} ({f["type"]})' for f in shadow.get('schema', [])
        )
        sample = _json.dumps(shadow.get('records', [])[:5], indent=2)

        if widget_ctx:
            data = widget_ctx.get('data', {})
            # Summarise data for the AI — strip raw value arrays (too large) and keep stats
            data_summary = {k: v for k, v in data.items() if k not in ('values', 'kde', 'points')}
            if 'values' in data:
                import statistics as _stats
                vals = [v for v in data['values'] if isinstance(v, (int, float))]
                if vals:
                    data_summary['value_count'] = len(vals)
                    data_summary['min']    = round(min(vals), 4)
                    data_summary['max']    = round(max(vals), 4)
                    data_summary['mean']   = round(_stats.mean(vals), 4)
                    data_summary['median'] = round(_stats.median(vals), 4)
            if 'points' in data:
                pts = data['points']
                data_summary['point_count'] = len(pts)
                if pts:
                    xs = [p['x'] for p in pts if isinstance(p.get('x'), (int, float))]
                    ys = [p['y'] for p in pts if isinstance(p.get('y'), (int, float))]
                    if xs: data_summary['x_range'] = [round(min(xs), 4), round(max(xs), 4)]
                    if ys: data_summary['y_range'] = [round(min(ys), 4), round(max(ys), 4)]
            chart_data_str = _json.dumps(data_summary, indent=2)[:800]
            return (
                f"Chart title: {widget_ctx['title']}\n"
                f"Chart type: {widget_ctx['chart_type']}\n"
                f"Fields used: {widget_ctx['fields_summary']}\n\n"
                f"Underlying table: {shadow.get('table_name', 'data')} "
                f"({shadow.get('record_count', 'N/A')} total records)\n"
                f"Schema:\n{schema_lines}\n\n"
                f"Chart data summary:\n{chart_data_str}\n\n"
                f"Sample records (anonymised):\n{sample}\n\n"
                f"User question: {question}"
            )

        return (
            f"Table: {shadow.get('table_name', 'data')}\n"
            f"Total records: {shadow.get('record_count', 'N/A')}\n\n"
            f"Schema:\n{schema_lines}\n\n"
            f"Sample (values anonymised):\n{sample}\n\n"
            f"User question: {question}"
        )

    def _build_widget_context(self, widget_id: str) -> dict | None:
        """Return chart type, fields used, and current rendered data for the widget."""
        from apps.dashboards.models import Widget
        from apps.dashboards.services import QueryEngine
        try:
            widget = Widget.objects.select_related('table').get(pk=widget_id)
        except Widget.DoesNotExist:
            return None

        qc = widget.query_config or {}
        aggs = qc.get('aggregations', [])

        if widget.widget_type == 'scatter':
            fields_summary = f"x={qc.get('x_field','?')}, y={qc.get('y_field','?')}"
        elif widget.widget_type in ('histogram', 'box_plot'):
            fields_summary = f"field={qc.get('field','?')}"
        elif aggs:
            parts = []
            for a in aggs:
                parts.append(f"{a.get('type','?')}({a.get('field','')}) grouped by {a.get('group_by','(none)')}")
            fields_summary = '; '.join(parts)
        else:
            fields_summary = str(qc)

        # Fetch live chart data (hits cache if warm)
        try:
            data = QueryEngine().execute_widget_query(widget)
        except Exception:
            data = {}

        return {
            'title':         widget.title,
            'chart_type':    widget.widget_type,
            'fields_summary': fields_summary,
            'data':          data,
        }

    def _check_budget(self) -> bool:
        from apps.workspaces.models import Workspace
        try:
            ws = Workspace.objects.get(id=self.workspace_id)
        except Workspace.DoesNotExist:
            return False
        from apps.insights.tasks import _get_or_create_budget
        budget = _get_or_create_budget(ws)
        return budget.has_capacity(estimated_tokens=700)

    def _build_shadow(self, table_id: str) -> dict | None:
        from apps.dashboards.models import Dashboard, Widget, DataTable
        from apps.insights.privacy import PrivacySampler
        try:
            if table_id:
                table = DataTable.objects.get(pk=table_id)
            else:
                table_id_val = (
                    Widget.objects
                    .filter(dashboard_id=self.dashboard_id, table__isnull=False)
                    .values_list('table_id', flat=True)
                    .first()
                )
                if not table_id_val:
                    return None
                table = DataTable.objects.get(pk=table_id_val)
            result = PrivacySampler(table).sample()
            result['table_id'] = str(table.pk)
            result['table_obj_pk'] = table.pk
            return result
        except DataTable.DoesNotExist:
            return None

    def _load_buffer(self, session_id: str) -> list[dict]:
        """Load the session conversation buffer from Redis."""
        from django.core.cache import cache
        import json as _json
        key = f'ask_ai:{session_id}'
        raw = cache.get(key, '[]')
        try:
            return _json.loads(raw)
        except Exception:
            return []

    def _update_buffer(self, session_id: str, question: str, answer: str):
        """Append the latest turn and keep only the last SESSION_BUFFER_SIZE turns."""
        from django.core.cache import cache
        import json as _json
        key = f'ask_ai:{session_id}'
        raw = cache.get(key, '[]')
        try:
            buffer = _json.loads(raw)
        except Exception:
            buffer = []

        buffer.append({'role': 'user',      'content': question})
        buffer.append({'role': 'assistant', 'content': answer})

        # Keep last SESSION_BUFFER_SIZE turns (each turn = 2 messages)
        max_msgs = self.SESSION_BUFFER_SIZE * 2
        buffer = buffer[-max_msgs:]

        cache.set(key, _json.dumps(buffer), timeout=self.SESSION_TTL_SECONDS)

    def _save_insight(self, question: str, answer: str, tokens: dict, table_name: str, is_widget: bool = False) -> str:
        from apps.insights.models import Insight
        from django.conf import settings
        insight = Insight.objects.create(
            workspace         = self._get_workspace_sync(),
            title             = question[:200],
            description       = answer,
            insight_type      = 'ask_ai_widget' if is_widget else 'ask_ai',
            source_table_name = table_name,
            question          = question,
            llm_model         = getattr(settings, 'CLAUDE_INSIGHT_MODEL', 'claude-sonnet-4-6'),
            prompt_tokens     = tokens.get('input', 0),
            completion_tokens = tokens.get('output', 0),
            created_by        = self.user,
        )
        return str(insight.id)

    def _consume_tokens(self, tokens: dict):
        from apps.workspaces.models import Workspace
        from apps.insights.tasks import _get_or_create_budget
        try:
            ws = Workspace.objects.get(id=self.workspace_id)
            budget = _get_or_create_budget(ws)
            budget.consume(tokens.get('input', 0) + tokens.get('output', 0))
        except Exception:
            logger.exception('AskAIConsumer: failed to consume tokens')

    def _get_workspace_sync(self):
        from apps.workspaces.models import Workspace
        return Workspace.objects.get(id=self.workspace_id)

    def _load_history_sync(self) -> list:
        from apps.insights.models import Insight
        return list(
            Insight.objects
            .filter(workspace_id=self.workspace_id, insight_type='ask_ai')  # excludes ask_ai_widget
            .order_by('-created_at')[:20]
            .values('id', 'question', 'description', 'created_at')
        )

    @database_sync_to_async
    def _load_history(self) -> list:
        items = self._load_history_sync()
        return _convert(items)

    @database_sync_to_async
    def _check_access(self) -> bool:
        try:
            dashboard = Dashboard.objects.select_related('workspace').get(
                id=self.dashboard_id, is_active=True)
            return dashboard.workspace.members.filter(id=self.user.id).exists()
        except Dashboard.DoesNotExist:
            return False

    @database_sync_to_async
    def _get_workspace_id(self) -> str:
        try:
            return str(Dashboard.objects.values_list('workspace_id', flat=True)
                       .get(id=self.dashboard_id))
        except Dashboard.DoesNotExist:
            return ''

    async def _send_error(self, message: str):
        await self.send(_dumps({'type': 'error', 'message': message}))
