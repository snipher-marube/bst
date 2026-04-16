"""
REST API v1 – complete spec implementation.

All endpoints require authentication. Workspace isolation is enforced
by filtering every queryset through the requesting user's current workspace.
"""
import io
import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import permissions, serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.workspaces.models import Workspace, WorkspaceMembership
from .models import DataTable, Record, Dashboard, Widget, AuditLog, ImportJob, DataAlert, WebhookEndpoint, DataSource, CalculatedField
from .permissions import HasWorkspaceAccess, CanEditData, CanManageWorkspace
from .webhook_crypto import encrypt_secret, decrypt_secret
from .serializers import (
    WorkspaceSerializer,
    DataTableSerializer,
    RecordSerializer,
    DashboardSerializer,
    WidgetSerializer,
    DataAlertSerializer,
    WebhookEndpointSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------------------
# Custom throttle scopes
# ---------------------------------------------------------------------------

class RegistrationThrottle(AnonRateThrottle):
    """Strict rate limit for account registration — prevents mass account creation."""
    scope = 'registration'


class FileUploadThrottle(UserRateThrottle):
    """Per-user limit on file upload / import operations."""
    scope = 'file_upload'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace(request):
    """Resolve the active workspace for this request.

    Resolution order:
    1. current_workspace set by CurrentWorkspaceMiddleware (session/HTML flow)
    2. X-Workspace-ID header (API/token-auth flow where middleware runs before auth)
    """
    ws = getattr(request.user, 'current_workspace', None)
    if ws:
        return ws
    workspace_id = request.headers.get('X-Workspace-ID')
    if workspace_id and request.user.is_authenticated:
        try:
            return Workspace.objects.get(id=workspace_id, members=request.user)
        except Workspace.DoesNotExist:
            return None
    return None


def _require_workspace(request):
    ws = _workspace(request)
    if not ws:
        raise serializers.ValidationError('No active workspace. Set X-Workspace-ID header.')
    return ws


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

class RegisterAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [RegistrationThrottle]

    def post(self, request):
        email = request.data.get('email', '').strip()
        password = request.data.get('password', '')
        if not email or not password:
            return Response({'error': 'email and password are required'}, status=400)
        if len(password) < 8:
            return Response({'error': 'Password must be at least 8 characters'}, status=400)
        if User.objects.filter(email=email).exists():
            return Response({'error': 'Email already registered'}, status=400)
        user = User.objects.create_user(username=email, email=email, password=password)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({'id': user.id, 'email': user.email, 'token': token.key}, status=201)


class ProfileAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        return Response({
            'id': u.id,
            'email': u.email,
            'first_name': u.first_name,
            'last_name': u.last_name,
            'date_joined': u.date_joined,
        })

    def patch(self, request):
        u = request.user
        u.first_name = request.data.get('first_name', u.first_name)
        u.last_name = request.data.get('last_name', u.last_name)
        u.save(update_fields=['first_name', 'last_name'])
        return Response({'id': u.id, 'email': u.email,
                         'first_name': u.first_name, 'last_name': u.last_name})


# ---------------------------------------------------------------------------
# WORKSPACES
# ---------------------------------------------------------------------------

class WorkspaceListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        workspaces = Workspace.objects.filter(members=request.user, is_active=True)
        return Response(WorkspaceSerializer(workspaces, many=True, context={'request': request}).data)

    def post(self, request):
        serializer = WorkspaceSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            workspace = serializer.save(owner=request.user)
            WorkspaceMembership.objects.create(workspace=workspace, user=request.user, role='owner')
        request.session['current_workspace_id'] = str(workspace.id)
        return Response(serializer.data, status=201)


class WorkspaceDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def _get_ws(self, pk, user):
        return get_object_or_404(Workspace, pk=pk, members=user, is_active=True)

    def get(self, request, pk):
        ws = self._get_ws(pk, request.user)
        return Response(WorkspaceSerializer(ws, context={'request': request}).data)

    def put(self, request, pk):
        ws = self._get_ws(pk, request.user)
        if ws.owner != request.user:
            return Response({'error': 'Only the owner can update the workspace'}, status=403)
        serializer = WorkspaceSerializer(ws, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        ws = self._get_ws(pk, request.user)
        if ws.owner != request.user:
            return Response({'error': 'Only the owner can delete the workspace'}, status=403)
        ws.is_active = False
        ws.save(update_fields=['is_active'])
        return Response(status=204)


# ---------------------------------------------------------------------------
# TABLES
# ---------------------------------------------------------------------------

class TableListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        page = max(int(request.GET.get('page', 1)), 1)
        limit = min(int(request.GET.get('limit', 50)), 200)
        offset = (page - 1) * limit
        qs = DataTable.objects.filter(workspace=ws, is_active=True).order_by('-updated_at')
        return Response({
            'count': qs.count(),
            'page': page,
            'limit': limit,
            'results': DataTableSerializer(qs[offset:offset + limit], many=True, context={'request': request}).data,
        })

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        if not ws.can_add_table():
            return Response({'error': 'Table limit reached for this workspace'}, status=400)
        serializer = DataTableSerializer(data=request.data, context={'request': request, 'workspace': ws})
        serializer.is_valid(raise_exception=True)
        # Table creation is atomic on its own; dashboard generation is async so a
        # dashboard failure never rolls back the committed table row.
        with transaction.atomic():
            table = serializer.save(workspace=ws, created_by=request.user)
        AuditLog.objects.create(
            workspace=ws, user=request.user, action='create',
            content_type='DataTable', object_id=table.id, object_repr=str(table),
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        from apps.insights.tasks import generate_default_dashboard as _gen_dashboard
        task = _gen_dashboard.delay(str(table.id))
        return Response({
            **DataTableSerializer(table, context={'request': request}).data,
            'auto_dashboard_task_id': task.id,
        }, status=201)


class TableDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def _get_table(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(DataTable, pk=pk, workspace=ws, is_active=True)

    def get(self, request, pk):
        table = self._get_table(pk, request)
        return Response(DataTableSerializer(table, context={'request': request}).data)

    def put(self, request, pk):
        table = self._get_table(pk, request)
        serializer = DataTableSerializer(table, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AuditLog.objects.create(
            workspace=table.workspace, user=request.user, action='update',
            content_type='DataTable', object_id=table.id, object_repr=str(table),
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        return Response(serializer.data)

    def delete(self, request, pk):
        table = self._get_table(pk, request)
        table.is_active = False
        table.deleted_at = timezone.now()
        table.save(update_fields=['is_active', 'deleted_at'])
        AuditLog.objects.create(
            workspace=table.workspace, user=request.user, action='delete',
            content_type='DataTable', object_id=table.id, object_repr=str(table),
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        return Response(status=204)


# ---------------------------------------------------------------------------
# RECORDS
# ---------------------------------------------------------------------------

class RecordListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, table_id):
        ws = _require_workspace(request)
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        page = max(int(request.GET.get('page', 1)), 1)
        limit = min(int(request.GET.get('limit', 50)), 500)
        offset = (page - 1) * limit

        qs = Record.objects.filter(table=table, is_active=True).order_by('-created_at')
        total = qs.count()
        records = qs[offset: offset + limit]

        return Response({
            'count': total,
            'page': page,
            'limit': limit,
            'results': RecordSerializer(records, many=True, context={'request': request}).data,
        })

    def post(self, request, table_id):
        ws = _require_workspace(request)
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        serializer = RecordSerializer(data={**request.data, 'table': table.id}, context={'request': request})
        serializer.is_valid(raise_exception=True)
        record = serializer.save(table=table, created_by=request.user)
        return Response(RecordSerializer(record, context={'request': request}).data, status=201)


class RecordDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]

    def _get_record(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(Record, pk=pk, table__workspace=ws, is_active=True)

    # Keep legacy signature so existing callers still work without breaking

    def get(self, request, pk):
        return Response(RecordSerializer(self._get_record(pk, request), context={'request': request}).data)

    def put(self, request, pk):
        record = self._get_record(pk, request)
        # Optimistic locking
        client_version = request.data.get('version')
        if client_version and int(client_version) != record.version:
            return Response({'error': 'Record was modified by another user. Reload and retry.'}, status=409)
        serializer = RecordSerializer(record, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, pk):
        record = self._get_record(pk, request)
        record.is_active = False
        record.deleted_at = timezone.now()
        record.save()
        return Response(status=204)


# ---------------------------------------------------------------------------
# BATCH RECORD OPERATIONS
# ---------------------------------------------------------------------------

class RecordBatchAPIView(APIView):
    """
    POST /api/v1/tables/<table_id>/records/batch/

    Accepts a JSON body with an array of operation objects.  Each object must
    include an ``op`` field:

    - ``create``: insert a new record.  Requires ``data`` dict.
    - ``update``: update an existing record by ``id``.  Requires ``id`` + ``data``.
    - ``upsert``: insert-or-update keyed on ``primary_key`` + ``data``.
    - ``delete``: soft-delete a record by ``id``.  Requires ``id``.

    Maximum 500 operations per request.

    Response::
        {
          "created":  <int>,
          "updated":  <int>,
          "deleted":  <int>,
          "errors":   [{"index": <int>, "op": "<op>", "error": "<msg>"}, ...]
        }
    """
    VALID_OPS  = {'create', 'update', 'upsert', 'delete'}
    MAX_OPS    = 500
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]

    def post(self, request, table_id):
        ws    = _require_workspace(request)
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)

        ops = request.data
        if not isinstance(ops, list):
            return Response({'error': 'Request body must be a JSON array of operations'}, status=400)
        if len(ops) > self.MAX_OPS:
            return Response(
                {'error': f'Maximum {self.MAX_OPS} operations per request (received {len(ops)})'},
                status=400,
            )

        created = updated = deleted = 0
        errors = []

        with transaction.atomic():
            for idx, op_obj in enumerate(ops):
                if not isinstance(op_obj, dict):
                    errors.append({'index': idx, 'op': None, 'error': 'Each operation must be a JSON object'})
                    continue

                op = op_obj.get('op')
                if op not in self.VALID_OPS:
                    errors.append({'index': idx, 'op': op, 'error': f'op must be one of: {", ".join(sorted(self.VALID_OPS))}'})
                    continue

                data       = op_obj.get('data', {})
                record_id  = op_obj.get('id')
                primary_key = op_obj.get('primary_key', '')

                try:
                    if op == 'create':
                        if not isinstance(data, dict):
                            raise ValueError('data must be a dict')
                        Record.objects.create(table=table, data=data, created_by=request.user)
                        created += 1

                    elif op == 'update':
                        if not record_id:
                            raise ValueError('id is required for update')
                        record = get_object_or_404(Record, pk=record_id, table=table, is_active=True)
                        record.data = {**record.data, **data} if isinstance(data, dict) else data
                        record.save(update_fields=['data'])
                        updated += 1

                    elif op == 'upsert':
                        if not primary_key:
                            raise ValueError('primary_key is required for upsert')
                        if not isinstance(data, dict):
                            raise ValueError('data must be a dict')
                        pk_val = data.get(primary_key)
                        if pk_val is None:
                            raise ValueError(f'primary_key field "{primary_key}" missing from data')
                        existing = Record.objects.filter(
                            table=table, is_active=True, **{f'data__{primary_key}': pk_val}
                        ).first()
                        if existing:
                            existing.data = data
                            existing.save(update_fields=['data'])
                            updated += 1
                        else:
                            Record.objects.create(table=table, data=data, created_by=request.user)
                            created += 1

                    elif op == 'delete':
                        if not record_id:
                            raise ValueError('id is required for delete')
                        record = get_object_or_404(Record, pk=record_id, table=table, is_active=True)
                        record.is_active  = False
                        record.deleted_at = timezone.now()
                        record.save(update_fields=['is_active', 'deleted_at'])
                        deleted += 1

                except Exception as exc:
                    errors.append({'index': idx, 'op': op, 'error': str(exc)})

        return Response({
            'created': created,
            'updated': updated,
            'deleted': deleted,
            'errors':  errors[:50],  # cap at 50
        }, status=200 if not errors or (created + updated + deleted) else 207)


# ---------------------------------------------------------------------------
# DASHBOARDS
# ---------------------------------------------------------------------------

class DashboardListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        page = max(int(request.GET.get('page', 1)), 1)
        limit = min(int(request.GET.get('limit', 50)), 200)
        offset = (page - 1) * limit
        qs = Dashboard.objects.filter(workspace=ws, is_active=True).order_by('-updated_at')
        return Response({
            'count': qs.count(),
            'page': page,
            'limit': limit,
            'results': DashboardSerializer(qs[offset:offset + limit], many=True, context={'request': request}).data,
        })

    def post(self, request, workspace_id):
        from django.db import IntegrityError
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        serializer = DashboardSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        try:
            dashboard = serializer.save(workspace=ws, created_by=request.user)
        except IntegrityError:
            return Response({'error': 'A dashboard with this slug already exists in this workspace.'}, status=400)
        return Response(DashboardSerializer(dashboard, context={'request': request}).data, status=201)


class DashboardDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def _get_dashboard(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(Dashboard, pk=pk, workspace=ws, is_active=True)

    def get(self, request, pk):
        return Response(DashboardSerializer(self._get_dashboard(pk, request), context={'request': request}).data)

    def put(self, request, pk):
        dashboard = self._get_dashboard(pk, request)
        serializer = DashboardSerializer(dashboard, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        dashboard = self._get_dashboard(pk, request)
        dashboard.is_active = False
        dashboard.deleted_at = timezone.now()
        dashboard.save(update_fields=['is_active', 'deleted_at'])
        return Response(status=204)


# ---------------------------------------------------------------------------
# WIDGETS
# ---------------------------------------------------------------------------

class WidgetListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]

    def get(self, request, dashboard_id):
        ws = _require_workspace(request)
        dashboard = get_object_or_404(Dashboard, pk=dashboard_id, workspace=ws, is_active=True)
        widgets = dashboard.widgets.select_related('table')
        return Response(WidgetSerializer(widgets, many=True, context={'request': request}).data)

    def post(self, request, dashboard_id):
        ws = _require_workspace(request)
        dashboard = get_object_or_404(Dashboard, pk=dashboard_id, workspace=ws, is_active=True)
        data = {**request.data, 'dashboard': dashboard.id}
        serializer = WidgetSerializer(data=data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        widget = serializer.save(dashboard=dashboard)
        return Response(WidgetSerializer(widget, context={'request': request}).data, status=201)


class WidgetDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]

    def _get_widget(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(Widget, pk=pk, dashboard__workspace=ws)

    def put(self, request, pk):
        widget = self._get_widget(pk, request)
        serializer = WidgetSerializer(widget, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # QueryEngine cache key includes widget.updated_at, so saving the widget
        # automatically invalidates the old key — no explicit deletion needed.
        return Response(serializer.data)

    def delete(self, request, pk):
        self._get_widget(pk, request).delete()
        return Response(status=204)


# ---------------------------------------------------------------------------
# IMPORT / EXPORT
# ---------------------------------------------------------------------------

class TableImportAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanEditData]
    throttle_classes = [FileUploadThrottle]

    def post(self, request, table_id):
        """Kick off an async import. Returns the ImportJob ID to poll."""
        from apps.insights.tasks import run_async_import
        import uuid as uuid_mod
        import pandas as pd
        import numpy as np
        import os

        ws = _require_workspace(request)
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'error': 'No file uploaded'}, status=400)

        # ── File size guard (reject before touching pandas) ────────────────
        MAX_UPLOAD_BYTES = getattr(settings, 'MAX_IMPORT_FILE_BYTES', 50 * 1024 * 1024)  # 50 MB
        if uploaded.size > MAX_UPLOAD_BYTES:
            limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            return Response(
                {'error': f'File too large. Maximum allowed size is {limit_mb} MB '
                          f'(uploaded {uploaded.size / 1024 / 1024:.1f} MB).'},
                status=413,
            )

        # ── Strip path traversal from filename ────────────────────────────
        uploaded.name = os.path.basename(uploaded.name)

        from apps.dashboards.services import DataImportService
        service = DataImportService()

        try:
            df = service.parse_file(uploaded)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

        # Persist df to cache so the Celery task can read it without re-uploading
        import_id = str(uuid_mod.uuid4())
        cache.set(f'import_df_{import_id}', df.to_json(orient='split'), 3600)

        # Detect/update schema if table has none
        if not table.schema:
            table.schema = service._detect_schema_from_df(df)
            table.save(update_fields=['schema'])

        # ── Import mode params ────────────────────────────────────────────
        import_mode = request.data.get('import_mode', 'append')
        if import_mode not in {'replace', 'append', 'upsert'}:
            return Response(
                {'error': 'import_mode must be one of: append, replace, upsert'},
                status=400,
            )
        primary_key_field = request.data.get('primary_key', '')
        if import_mode == 'upsert' and not primary_key_field:
            return Response({'error': 'primary_key is required for upsert mode'}, status=400)

        # Create ImportJob
        job = ImportJob.objects.create(
            workspace=table.workspace,
            table=table,
            created_by=request.user,
            file_name=uploaded.name,
            file_path=import_id,  # We use import_id as a reference into cache
            status='pending',
            total_rows=len(df),
            import_mode=import_mode,
            primary_key_field=primary_key_field,
        )

        # Fire Celery task
        task = run_async_import.delay(str(job.id))
        job.celery_task_id = task.id
        job.save(update_fields=['celery_task_id'])

        return Response({
            'import_job_id': str(job.id),
            'status': job.status,
            'total_rows': job.total_rows,
        }, status=202)


class ImportJobStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, job_id):
        job = get_object_or_404(ImportJob, pk=job_id, created_by=request.user)
        return Response({
            'id': str(job.id),
            'status': job.status,
            'total_rows': job.total_rows,
            'processed_rows': job.processed_rows,
            'success_rows': job.success_rows,
            'error_rows': job.error_rows,
            'progress_pct': job.progress_pct,
            'error_log': job.error_log[:5],  # first 5 errors
            'created_at': job.created_at,
            'completed_at': job.completed_at,
        })


class TableExportAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, table_id):
        from apps.exports.views import export_table as _export
        ws = _require_workspace(request)
        # Verify ownership before delegating to the exports view
        get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        return _export(request, table_id)


# ---------------------------------------------------------------------------
# INSIGHTS
# ---------------------------------------------------------------------------

class InsightsGenerateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanManageWorkspace]

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        from apps.insights.tasks import analyze_workspace_tables
        task = analyze_workspace_tables.delay(str(ws.id))
        return Response({'task_id': task.id, 'status': 'started'}, status=202)


class InsightsListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        try:
            from apps.insights.models import Insight
            page = max(int(request.GET.get('page', 1)), 1)
            limit = min(int(request.GET.get('limit', 50)), 200)
            offset = (page - 1) * limit
            qs = Insight.objects.filter(workspace=ws).order_by('-created_at')
            data = [
                {
                    'id': str(i.id),
                    'title': i.title,
                    'description': i.description,
                    'insight_type': i.insight_type,
                    'chart_data': i.chart_data,
                    'source_table_name': i.source_table_name,
                    'created_at': i.created_at,
                }
                for i in qs[offset:offset + limit]
            ]
            return Response({'count': qs.count(), 'page': page, 'limit': limit, 'results': data})
        except Exception as e:
            logger.error(f"Error fetching insights: {e}")
            return Response({'error': str(e)}, status=500)


# ---------------------------------------------------------------------------
# TEAM MANAGEMENT
# ---------------------------------------------------------------------------

class TeamMemberListAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        memberships = WorkspaceMembership.objects.filter(workspace=ws).select_related('user')
        data = [
            {
                'user_id': m.user.id,
                'email': m.user.email,
                'first_name': m.user.first_name,
                'last_name': m.user.last_name,
                'role': m.role,
                'joined_at': m.joined_at,
            }
            for m in memberships
        ]
        return Response(data)

    def post(self, request, workspace_id):
        """Invite a user by email."""
        from apps.workspaces.views import invite_member
        # Re-use the view but return JSON
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        from apps.workspaces.models import WorkspaceInvitation
        from django.core.mail import send_mail

        email = request.data.get('email', '').strip().lower()
        role = request.data.get('role', 'viewer')
        if not email:
            return Response({'error': 'email is required'}, status=400)

        invitation, _ = WorkspaceInvitation.objects.update_or_create(
            workspace=ws, email=email,
            defaults={
                'role': role,
                'invited_by': request.user,
                'is_revoked': False,
                'is_accepted': False,
                'expires_at': timezone.now() + timezone.timedelta(days=7),
            }
        )
        return Response({'message': f'Invitation sent to {email}', 'invitation_id': str(invitation.id)}, status=201)


class TeamMemberDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanManageWorkspace]

    def patch(self, request, workspace_id, user_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        membership = get_object_or_404(WorkspaceMembership, workspace=ws, user_id=user_id)
        new_role = request.data.get('role')
        if new_role:
            membership.role = new_role
            membership.save(update_fields=['role'])
        return Response({'user_id': user_id, 'role': membership.role})

    def delete(self, request, workspace_id, user_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        membership = get_object_or_404(WorkspaceMembership, workspace=ws, user_id=user_id)
        if membership.role == 'owner':
            return Response({'error': 'Cannot remove the workspace owner'}, status=400)
        membership.delete()
        return Response(status=204)


# ---------------------------------------------------------------------------
# DATA ALERTS
# ---------------------------------------------------------------------------

class DataAlertListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        qs = DataAlert.objects.filter(workspace=ws).order_by('-created_at')
        return Response(DataAlertSerializer(qs, many=True, context={'request': request}).data)

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        # Verify the referenced table belongs to this workspace
        table_id = request.data.get('table')
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        serializer = DataAlertSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        alert = serializer.save(workspace=ws, table=table, created_by=request.user)
        return Response(DataAlertSerializer(alert, context={'request': request}).data, status=201)


class DataAlertDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def _get_alert(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(DataAlert, pk=pk, workspace=ws)

    def get(self, request, pk):
        return Response(DataAlertSerializer(self._get_alert(pk, request), context={'request': request}).data)

    def patch(self, request, pk):
        alert = self._get_alert(pk, request)
        serializer = DataAlertSerializer(alert, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        self._get_alert(pk, request).delete()
        return Response(status=204)


# ---------------------------------------------------------------------------
# WEBHOOK ENDPOINTS
# ---------------------------------------------------------------------------

class WebhookEndpointListCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        qs = WebhookEndpoint.objects.filter(workspace=ws).order_by('-created_at')
        return Response(WebhookEndpointSerializer(qs, many=True, context={'request': request}).data)

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        table_id = request.data.get('table')
        table = get_object_or_404(DataTable, pk=table_id, workspace=ws, is_active=True)
        serializer = WebhookEndpointSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        plaintext_secret = WebhookEndpoint.generate_secret()
        endpoint = serializer.save(
            workspace=ws,
            table=table,
            created_by=request.user,
            token=WebhookEndpoint.generate_token(),
            secret=encrypt_secret(plaintext_secret),
        )
        # Return plaintext secret exactly once — it is not recoverable from
        # the API after this response.
        data = WebhookEndpointSerializer(endpoint, context={'request': request}).data
        data['secret'] = plaintext_secret
        return Response(data, status=201)


class WebhookEndpointDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def _get_endpoint(self, pk, request):
        ws = _require_workspace(request)
        return get_object_or_404(WebhookEndpoint, pk=pk, workspace=ws)

    def get(self, request, pk):
        return Response(WebhookEndpointSerializer(self._get_endpoint(pk, request), context={'request': request}).data)

    def patch(self, request, pk):
        endpoint = self._get_endpoint(pk, request)
        serializer = WebhookEndpointSerializer(endpoint, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        self._get_endpoint(pk, request).delete()
        return Response(status=204)


class WebhookEndpointRegenerateSecretAPIView(APIView):
    """Rotate the signing secret for a webhook endpoint."""
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanManageWorkspace]

    def post(self, request, pk):
        ws = _require_workspace(request)
        endpoint = get_object_or_404(WebhookEndpoint, pk=pk, workspace=ws)
        plaintext_secret = WebhookEndpoint.generate_secret()
        endpoint.secret = encrypt_secret(plaintext_secret)
        endpoint.save(update_fields=['secret', 'updated_at'])
        # Return plaintext exactly once — store it immediately, it won't be shown again.
        return Response({'secret': plaintext_secret})


# ---------------------------------------------------------------------------
# WEBHOOK INGEST  (public — no authentication required)
# ---------------------------------------------------------------------------

class WebhookIngestView(APIView):
    """
    Public endpoint: ``POST /webhook/ingest/<token>/``

    Validates the ``X-Hub-Signature-256`` HMAC header, then writes Record
    rows to the target DataTable from the JSON payload.

    Payload formats accepted:
    - JSON object  → one record
    - JSON array   → one record per element

    Query parameters:
    - ``import_mode``: ``replace`` (default) | ``append`` | ``upsert``
        - ``replace``: mark all existing records inactive, then insert fresh rows
        - ``append``:  insert new rows without touching existing data
        - ``upsert``:  insert or update rows keyed on ``primary_key`` field
    - ``primary_key``: field name used as the unique key for ``upsert`` mode
    """
    VALID_MODES = {'replace', 'append', 'upsert'}

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, token):
        import hashlib
        import hmac as _hmac_mod

        endpoint = WebhookEndpoint.objects.filter(token=token, is_active=True).first()
        if not endpoint:
            return Response(status=404)

        # ── HMAC verification ─────────────────────────────────────────────
        sig_header = request.META.get('HTTP_X_HUB_SIGNATURE_256', '')
        raw_body = request.body
        plaintext_key = endpoint.get_plaintext_secret().encode('utf-8')
        expected = 'sha256=' + _hmac_mod.new(
            plaintext_key,
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        if not _hmac_mod.compare_digest(sig_header, expected):
            logger.warning('Webhook HMAC mismatch endpoint=%s', endpoint.id)
            return Response({'error': 'Invalid signature'}, status=401)

        # ── Parse payload ─────────────────────────────────────────────────
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return Response({'error': 'Invalid JSON payload'}, status=400)

        if isinstance(payload, dict):
            rows = [payload]
        elif isinstance(payload, list):
            rows = payload
        else:
            return Response({'error': 'Payload must be a JSON object or array'}, status=400)

        if not rows:
            return Response({'created': 0, 'updated': 0})

        # ── Import mode ───────────────────────────────────────────────────
        import_mode = request.query_params.get('import_mode', 'replace')
        if import_mode not in self.VALID_MODES:
            return Response(
                {'error': f'import_mode must be one of: {", ".join(sorted(self.VALID_MODES))}'},
                status=400,
            )
        primary_key = request.query_params.get('primary_key', '')
        if import_mode == 'upsert' and not primary_key:
            return Response({'error': 'primary_key is required for upsert mode'}, status=400)

        # ── Write records ─────────────────────────────────────────────────
        table = endpoint.table
        created = updated = 0
        errors = []

        with transaction.atomic():
            if import_mode == 'replace':
                # Soft-delete all existing active records for this table
                Record.objects.filter(table=table, is_active=True).update(is_active=False)

            for i, row in enumerate(rows[:1000]):  # hard cap: 1000 records per request
                if not isinstance(row, dict):
                    errors.append({'index': i, 'error': 'Each element must be a JSON object'})
                    continue
                try:
                    if import_mode == 'upsert' and primary_key and primary_key in row:
                        pk_val = row[primary_key]
                        existing = Record.objects.filter(
                            table=table, is_active=True, **{f'data__{primary_key}': pk_val}
                        ).first()
                        if existing:
                            existing.data = row
                            existing.save(update_fields=['data'])
                            updated += 1
                        else:
                            Record.objects.create(table=table, data=row)
                            created += 1
                    else:
                        Record.objects.create(table=table, data=row)
                        created += 1
                except Exception as exc:
                    errors.append({'index': i, 'error': str(exc)})

        # ── Update telemetry ──────────────────────────────────────────────
        from django.db.models import F as _F
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            total_requests=_F('total_requests') + 1,
            last_request_at=timezone.now(),
        )

        response_data = {'created': created, 'updated': updated, 'mode': import_mode}
        if errors:
            response_data['errors'] = errors[:10]  # first 10 only
        return Response(response_data, status=200 if (created or updated) else 400)


# ---------------------------------------------------------------------------
# AUDIT LOG
# ---------------------------------------------------------------------------

class AuditLogListAPIView(APIView):
    """
    Read-only list of AuditLog entries for a workspace.
    Only admins and owners may view the full audit log.
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess, CanManageWorkspace]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        page  = max(int(request.GET.get('page', 1)), 1)
        limit = min(int(request.GET.get('limit', 50)), 200)
        offset = (page - 1) * limit

        qs = AuditLog.objects.filter(workspace=ws).order_by('-timestamp')

        # Optional filters
        action = request.GET.get('action')
        if action:
            qs = qs.filter(action=action)
        content_type = request.GET.get('content_type')
        if content_type:
            qs = qs.filter(content_type=content_type)

        data = [
            {
                'id':           str(entry.id) if hasattr(entry, 'id') else None,
                'user_id':      entry.user_id,
                'user_email':   entry.user.email if entry.user else None,
                'action':       entry.action,
                'content_type': entry.content_type,
                'object_id':    str(entry.object_id),
                'object_repr':  entry.object_repr,
                'changes':      entry.changes,
                'ip_address':   entry.ip_address,
                'timestamp':    entry.timestamp,
            }
            for entry in qs.select_related('user')[offset: offset + limit]
        ]
        return Response({'count': qs.count(), 'page': page, 'limit': limit, 'results': data})


# ---------------------------------------------------------------------------
# WORKSPACE USAGE ANALYTICS
# ---------------------------------------------------------------------------

class WorkspaceUsageAPIView(APIView):
    """
    Aggregate usage statistics for a workspace.
    Returns table count, total records, estimated storage, import history, etc.
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def get(self, request, workspace_id):
        from django.db.models import Sum, Count as DbCount, Max
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)

        tables_qs = DataTable.objects.filter(workspace=ws, is_active=True)
        agg = tables_qs.aggregate(
            total_tables=DbCount('id'),
            total_records=Sum('record_count'),
        )

        recent_import = (
            ImportJob.objects
            .filter(workspace=ws, status='completed')
            .order_by('-completed_at')
            .values('id', 'file_name', 'success_rows', 'completed_at')
            .first()
        )

        member_count = WorkspaceMembership.objects.filter(workspace=ws).count()

        total_records = agg['total_records'] or 0
        estimated_storage_mb = round(total_records * 1024 / (1024 ** 2), 2)  # ~1 KB per record

        from apps.insights.models import Insight
        insight_count = Insight.objects.filter(workspace=ws).count()

        return Response({
            'workspace_id':        str(ws.id),
            'workspace_name':      ws.name,
            'total_tables':        agg['total_tables'] or 0,
            'total_records':       total_records,
            'estimated_storage_mb': estimated_storage_mb,
            'member_count':        member_count,
            'insight_count':       insight_count,
            'recent_import':       recent_import,
        })


# =============================================================================
# DataSource connector API views (Gap 7 — PostgreSQL direct connector)
# =============================================================================

class DataSourceSerializer(serializers.ModelSerializer):
    """
    Serializer for DataSource.

    DB connectors (postgresql / mysql):
      • password (write-only) — plaintext password, encrypted at rest.
      • has_password (read-only) — True when a password has been stored.

    Google Sheets connector:
      • google_credentials_json (write-only) — service-account JSON, encrypted at rest.
      • has_google_credentials (read-only)   — True when credentials have been stored.
      • google_spreadsheet_id                — the sheet ID from the URL.
    """
    # ── DB connector fields ──────────────────────────────────────────────
    password     = serializers.CharField(write_only=True, required=False,
                                         allow_blank=True, default='')
    has_password = serializers.SerializerMethodField(read_only=True)

    # ── Google Sheets fields ─────────────────────────────────────────────
    google_credentials_json = serializers.CharField(
        write_only=True, required=False, allow_blank=True, default='',
        help_text='Full service-account JSON string (write-only, stored encrypted).',
    )
    has_google_credentials = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model  = DataSource
        fields = [
            'id', 'name', 'connector_type',
            # DB connector
            'host', 'port', 'database', 'username',
            'password', 'has_password', 'ssl_mode', 'extra_options',
            # Google Sheets
            'google_credentials_json', 'has_google_credentials',
            'google_spreadsheet_id',
            # shared status
            'is_active', 'last_tested_at', 'last_test_ok', 'last_test_error',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'has_password', 'has_google_credentials',
            'last_tested_at', 'last_test_ok', 'last_test_error',
            'created_at', 'updated_at',
        ]

    def get_has_password(self, obj):
        return bool(obj._password)

    def get_has_google_credentials(self, obj):
        return bool(obj.google_credentials_enc)

    def validate(self, data):
        connector = data.get('connector_type', getattr(self.instance, 'connector_type', 'postgresql'))
        if connector == 'google_sheets':
            # On create, credentials and spreadsheet ID are required.
            if not self.instance:
                if not data.get('google_credentials_json'):
                    raise serializers.ValidationError(
                        {'google_credentials_json': 'Required for Google Sheets connector.'}
                    )
                if not data.get('google_spreadsheet_id'):
                    raise serializers.ValidationError(
                        {'google_spreadsheet_id': 'Required for Google Sheets connector.'}
                    )
        else:
            # DB connectors require host, database, username, password on create.
            if not self.instance:
                for field in ('host', 'database', 'username'):
                    if not data.get(field):
                        raise serializers.ValidationError({field: 'Required for database connectors.'})
                if not data.get('password'):
                    raise serializers.ValidationError({'password': 'Required for database connectors.'})
        return data

    def create(self, validated_data):
        password     = validated_data.pop('password', '')
        creds_json   = validated_data.pop('google_credentials_json', '')
        ds = DataSource(**validated_data)
        if password:
            ds.set_password(password)
        if creds_json:
            ds.set_google_credentials(creds_json)
        ds.save()
        return ds

    def update(self, instance, validated_data):
        password   = validated_data.pop('password', None)
        creds_json = validated_data.pop('google_credentials_json', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        if creds_json:
            instance.set_google_credentials(creds_json)
        instance.save()
        return instance


class DataSourceListCreateAPIView(APIView):
    """
    GET  /api/v1/workspaces/<workspace_id>/data-sources/
    POST /api/v1/workspaces/<workspace_id>/data-sources/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        qs = DataSource.objects.filter(workspace=ws, is_active=True).order_by('name')
        return Response(DataSourceSerializer(qs, many=True).data)

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, pk=workspace_id, members=request.user)
        # Only owner/admin may add connectors
        try:
            membership = WorkspaceMembership.objects.get(workspace=ws, user=request.user)
        except WorkspaceMembership.DoesNotExist:
            return Response({'error': 'Not a member.'}, status=403)
        if membership.role not in ('owner', 'admin'):
            return Response({'error': 'Admin or owner required.'}, status=403)
        ser = DataSourceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        ds = ser.save(workspace=ws, created_by=request.user)
        return Response(DataSourceSerializer(ds).data, status=201)


class DataSourceDetailAPIView(APIView):
    """
    GET    /api/v1/data-sources/<pk>/
    PATCH  /api/v1/data-sources/<pk>/
    DELETE /api/v1/data-sources/<pk>/
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_ds(self, request, pk):
        ds = get_object_or_404(DataSource, pk=pk)
        # Enforce workspace membership
        if not ds.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()
        return ds

    def _require_manage(self, request, workspace):
        try:
            m = WorkspaceMembership.objects.get(workspace=workspace, user=request.user)
        except WorkspaceMembership.DoesNotExist:
            return False
        return m.role in ('owner', 'admin')

    def get(self, request, pk):
        return Response(DataSourceSerializer(self._get_ds(request, pk)).data)

    def patch(self, request, pk):
        ds = self._get_ds(request, pk)
        if not self._require_manage(request, ds.workspace):
            return Response({'error': 'Admin or owner required.'}, status=403)
        ser = DataSourceSerializer(ds, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        return Response(DataSourceSerializer(ser.save()).data)

    def delete(self, request, pk):
        ds = self._get_ds(request, pk)
        if not self._require_manage(request, ds.workspace):
            return Response({'error': 'Admin or owner required.'}, status=403)
        ds.is_active = False
        ds.save(update_fields=['is_active'])
        return Response(status=204)


class DataSourceTestAPIView(APIView):
    """
    POST /api/v1/data-sources/<pk>/test/

    DB connectors: runs ``SELECT 1`` over a real connection.
    Google Sheets: opens the spreadsheet and lists worksheets.
    Updates last_tested_at / last_test_ok / last_test_error.
    Returns 200 on success, 400 on connection error.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from .services import DataSourceQueryEngine, GoogleSheetsQueryEngine

        ds = get_object_or_404(DataSource, pk=pk)
        if not ds.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()

        ok    = False
        error = ''
        message = ''
        try:
            if ds.connector_type == 'google_sheets':
                engine = GoogleSheetsQueryEngine(ds)
                ok, message = engine.test_connection()
                if not ok:
                    error = message
            else:
                engine = DataSourceQueryEngine(ds)
                with engine._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute('SELECT 1')
                ok      = True
                message = 'Connection successful.'
        except Exception as exc:
            error = str(exc)

        ds.last_tested_at  = timezone.now()
        ds.last_test_ok    = ok
        ds.last_test_error = error
        ds.save(update_fields=['last_tested_at', 'last_test_ok', 'last_test_error'])

        if ok:
            return Response({'ok': True, 'message': message, 'error': None})
        return Response({'ok': False, 'error': error}, status=400)


class DataSourceSchemaAPIView(APIView):
    """
    GET /api/v1/data-sources/<pk>/schema/

    DB connectors: returns tables/views with column metadata.
    Google Sheets: returns worksheet names with inferred column types.
    Response shape: [{"name": str, "type": str, "columns": [...]}]
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from .services import DataSourceQueryEngine, GoogleSheetsQueryEngine

        ds = get_object_or_404(DataSource, pk=pk)
        if not ds.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()

        engine = GoogleSheetsQueryEngine(ds) if ds.connector_type == 'google_sheets' \
            else DataSourceQueryEngine(ds)
        try:
            tables = engine.list_tables()
            for tbl in tables:
                try:
                    tbl['columns'] = engine.list_columns(tbl['name'])
                except Exception:
                    tbl['columns'] = []
            return Response(tables)
        except Exception as exc:
            return Response({'error': str(exc)}, status=400)


# ---------------------------------------------------------------------------
# Gap 14 — Calculated Fields
# ---------------------------------------------------------------------------

class CalculatedFieldSerializer(serializers.ModelSerializer):
    table_id = serializers.UUIDField(source='table.id', read_only=True)

    class Meta:
        model = CalculatedField
        fields = [
            'id', 'table_id', 'name', 'display_name',
            'expression', 'format_type', 'description',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'table_id', 'created_at', 'updated_at']

    def validate_expression(self, value):
        from .services import CalculatedFieldEvaluator
        valid, error = CalculatedFieldEvaluator.validate_expression(value)
        if not valid:
            raise serializers.ValidationError(f'Invalid expression: {error}')
        return value


class CalculatedFieldListCreateAPIView(APIView):
    """
    GET  /api/v1/tables/<table_id>/calculated-fields/  — list all for this table
    POST /api/v1/tables/<table_id>/calculated-fields/  — create a new one
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_table(self, request, table_id):
        table = get_object_or_404(DataTable, pk=table_id)
        if not table.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()
        return table

    def get(self, request, table_id):
        table = self._get_table(request, table_id)
        fields = CalculatedField.objects.filter(table=table)
        return Response(CalculatedFieldSerializer(fields, many=True).data)

    def post(self, request, table_id):
        table = self._get_table(request, table_id)
        ser = CalculatedFieldSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cf = ser.save(table=table)
        return Response(CalculatedFieldSerializer(cf).data, status=status.HTTP_201_CREATED)


class CalculatedFieldDetailAPIView(APIView):
    """
    GET    /api/v1/calculated-fields/<pk>/  — retrieve
    PATCH  /api/v1/calculated-fields/<pk>/  — partial update
    DELETE /api/v1/calculated-fields/<pk>/  — delete
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_cf(self, request, pk):
        cf = get_object_or_404(CalculatedField, pk=pk)
        if not cf.table.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()
        return cf

    def get(self, request, pk):
        cf = self._get_cf(request, pk)
        return Response(CalculatedFieldSerializer(cf).data)

    def patch(self, request, pk):
        cf = self._get_cf(request, pk)
        ser = CalculatedFieldSerializer(cf, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(CalculatedFieldSerializer(cf).data)

    def delete(self, request, pk):
        cf = self._get_cf(request, pk)
        cf.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CalculatedFieldPreviewAPIView(APIView):
    """
    POST /api/v1/calculated-fields/<pk>/preview/

    Evaluate the calculated field against the first 5 000 records of its
    table and return the computed result.  Useful for validating an
    expression before saving.

    Also accepts an ad-hoc ``expression`` body param for live preview
    before a field is saved.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from .models import Record
        from .services import CalculatedFieldEvaluator

        cf = get_object_or_404(CalculatedField, pk=pk)
        if not cf.table.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()

        expression = request.data.get('expression', cf.expression)

        records = list(
            Record.objects.filter(table=cf.table, is_active=True)
            .values_list('data', flat=True)[:5_000]
        )

        try:
            value = CalculatedFieldEvaluator.evaluate_against_records(expression, records)
            return Response({
                'expression': expression,
                'result': value,
                'record_count': len(records),
                'format_type': cf.format_type,
            })
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)


class CalculatedFieldValidateAPIView(APIView):
    """
    POST /api/v1/tables/<table_id>/calculated-fields/validate/

    Validate an expression without saving it.
    Body: {"expression": "sum(revenue) / count(user_id)"}
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, table_id):
        from .services import CalculatedFieldEvaluator

        table = get_object_or_404(DataTable, pk=table_id)
        if not table.workspace.members.filter(id=request.user.id).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied()

        expression = request.data.get('expression', '')
        if not expression:
            return Response({'valid': False, 'error': 'expression is required.'}, status=400)

        valid, error = CalculatedFieldEvaluator.validate_expression(expression)
        return Response({'valid': valid, 'error': error or None})


# ---------------------------------------------------------------------------
# Gap 15 — Cohort & Funnel Analysis
# ---------------------------------------------------------------------------

class CohortAnalysisAPIView(APIView):
    """
    POST /api/v1/tables/<table_id>/cohort-analysis/

    Run an ad-hoc cohort retention analysis on a DataTable without creating a
    widget first.  The request body mirrors the cohort widget query_config.

    Request body
    ------------
    {
      "user_field":       "user_id",    # required
      "event_date_field": "created_at", # optional
      "period":           "week",       # "day" | "week" | "month"
      "periods":          8,            # number of periods (max 52)
      "filters":          []            # optional pre-filters
    }
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def post(self, request, table_id):
        from .services import CohortAnalysisEngine

        workspace = _require_workspace(request)
        table = get_object_or_404(DataTable, id=table_id, workspace=workspace, is_active=True)

        # Build a transient widget-like object so the engine can be reused
        class _FakeWidget:
            pass

        widget = _FakeWidget()
        widget.table = table
        widget.query_config = request.data if isinstance(request.data, dict) else {}

        if not widget.query_config.get('user_field'):
            return Response({'error': 'user_field is required'}, status=status.HTTP_400_BAD_REQUEST)

        result = CohortAnalysisEngine().execute(widget)
        if 'error' in result:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class FunnelAnalysisAPIView(APIView):
    """
    POST /api/v1/tables/<table_id>/funnel-analysis/

    Run an ad-hoc funnel drop-off analysis on a DataTable without creating a
    widget first.  The request body mirrors the funnel widget query_config.

    Request body
    ------------
    {
      "user_field": "user_id",    # optional
      "ordered":    true,         # enforce step ordering (default: true)
      "steps": [
        {"name": "Step 1", "filters": [{"field": "event", "operator": "eq", "value": "signup"}]},
        {"name": "Step 2", "filters": [{"field": "event", "operator": "eq", "value": "purchase"}]}
      ]
    }
    """
    permission_classes = [permissions.IsAuthenticated, HasWorkspaceAccess]

    def post(self, request, table_id):
        from .services import FunnelAnalysisEngine

        workspace = _require_workspace(request)
        table = get_object_or_404(DataTable, id=table_id, workspace=workspace, is_active=True)

        body = request.data if isinstance(request.data, dict) else {}
        steps = body.get('steps', [])
        if not steps or not isinstance(steps, list):
            return Response(
                {'error': 'steps must be a non-empty list'}, status=status.HTTP_400_BAD_REQUEST
            )

        class _FakeWidget:
            pass

        widget = _FakeWidget()
        widget.table = table
        widget.query_config = body

        result = FunnelAnalysisEngine().execute(widget)
        if 'error' in result:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
