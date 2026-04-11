"""
REST API v1 – complete spec implementation.

All endpoints require authentication. Workspace isolation is enforced
by filtering every queryset through the requesting user's current workspace.
"""
import io
import json
import logging

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
from .models import DataTable, Record, Dashboard, Widget, AuditLog, ImportJob
from .permissions import HasWorkspaceAccess, CanEditData, CanManageWorkspace
from .serializers import (
    WorkspaceSerializer,
    DataTableSerializer,
    RecordSerializer,
    DashboardSerializer,
    WidgetSerializer,
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

        # Create ImportJob
        job = ImportJob.objects.create(
            workspace=table.workspace,
            table=table,
            created_by=request.user,
            file_name=uploaded.name,
            file_path=import_id,  # We use import_id as a reference into cache
            status='pending',
            total_rows=len(df),
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
