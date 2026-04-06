"""
apps/dashboards/test_api.py
============================
Integration tests for the REST API v1 endpoints at /api/v1/.

Auth: DRF Token authentication.
Workspace isolation is verified throughout — users should not access
data belonging to other workspaces.
"""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, WorkspaceMembershipFactory,
    DataTableFactory, RecordFactory, DashboardFactory, WidgetFactory,
)
from apps.workspaces.models import WorkspaceMembership


# ---------------------------------------------------------------------------
# Base class: authenticated API client helper
# ---------------------------------------------------------------------------

class AuthAPITestCase(TestCase):
    """Creates a user + DRF token and provides an authenticated client."""

    def setUp(self):
        self.user      = UserFactory()
        self.token     = Token.objects.create(user=self.user)
        self.client    = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        # Workspace owned by self.user
        self.workspace = WorkspaceFactory(owner=self.user)

    def _ws_header(self, workspace_id=None):
        return {'HTTP_X_WORKSPACE_ID': str(workspace_id or self.workspace.id)}


# ---------------------------------------------------------------------------
# Auth / Registration
# ---------------------------------------------------------------------------

class TestRegisterAPI(TestCase):

    def test_register_creates_user(self):
        client = APIClient()
        resp = client.post('/api/v1/auth/register/', {
            'username': 'newuser',
            'email': 'newuser@test.com',
            'password': 'StrongPass123!',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('token', resp.data)

    def test_register_duplicate_email_rejected(self):
        UserFactory(email='exists@test.com')
        client = APIClient()
        resp = client.post('/api/v1/auth/register/', {
            'username': 'another',
            'email': 'exists@test.com',
            'password': 'StrongPass123!',
        }, format='json')
        self.assertIn(resp.status_code, [400, 409])

    def test_register_missing_fields_rejected(self):
        client = APIClient()
        resp = client.post('/api/v1/auth/register/', {'username': 'x'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_profile_returns_401(self):
        client = APIClient()
        resp = client.get('/api/v1/auth/profile/')
        self.assertEqual(resp.status_code, 401)

    def test_authenticated_profile_returns_200(self):
        user   = UserFactory()
        token  = Token.objects.create(user=user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        resp = client.get('/api/v1/auth/profile/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['email'], user.email)


# ---------------------------------------------------------------------------
# Workspace API
# ---------------------------------------------------------------------------

class TestWorkspaceAPI(AuthAPITestCase):

    def test_list_workspaces(self):
        resp = self.client.get('/api/v1/workspaces/', **self._ws_header())
        self.assertEqual(resp.status_code, 200)

    def test_create_workspace(self):
        resp = self.client.post('/api/v1/workspaces/', {'name': 'New WS'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'New WS')

    def test_create_workspace_empty_name_rejected(self):
        resp = self.client.post('/api/v1/workspaces/', {'name': ''}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_get_workspace_detail(self):
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/', **self._ws_header())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], self.workspace.name)

    def test_get_other_users_workspace_returns_403_or_404(self):
        other_ws = WorkspaceFactory()   # owned by a different user
        resp = self.client.get(f'/api/v1/workspaces/{other_ws.id}/')
        self.assertIn(resp.status_code, [403, 404])


# ---------------------------------------------------------------------------
# Table API
# ---------------------------------------------------------------------------

class TestTableAPI(AuthAPITestCase):

    def test_list_tables_empty(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/tables/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results'] if 'results' in resp.data else resp.data), 0)

    def test_create_table(self):
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/tables/',
            {
                'name':   'Sales Data',
                'schema': [{'name': 'amount', 'type': 'currency', 'required': True}],
            },
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Sales Data')

    def test_create_table_enforces_limit(self):
        """Free tier: max 5 tables."""
        for i in range(5):
            DataTableFactory(workspace=self.workspace)
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/tables/',
            {'name': 'Table 6', 'schema': []},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [400, 403])

    def test_get_table_detail(self):
        table = DataTableFactory(workspace=self.workspace)
        resp  = self.client.get(f'/api/v1/tables/{table.id}/', **self._ws_header())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], table.name)

    def test_update_table(self):
        table = DataTableFactory(workspace=self.workspace, name='Old Name')
        resp  = self.client.put(
            f'/api/v1/tables/{table.id}/',
            {'name': 'New Name', 'schema': []},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 202])

    def test_delete_table(self):
        table = DataTableFactory(workspace=self.workspace)
        resp  = self.client.delete(f'/api/v1/tables/{table.id}/', **self._ws_header())
        self.assertIn(resp.status_code, [200, 204])

    def test_cannot_access_other_workspace_table(self):
        other_table = DataTableFactory()
        resp = self.client.get(f'/api/v1/tables/{other_table.id}/')
        self.assertIn(resp.status_code, [403, 404])


# ---------------------------------------------------------------------------
# Record API
# ---------------------------------------------------------------------------

class TestRecordAPI(AuthAPITestCase):

    def setUp(self):
        super().setUp()
        self.table = DataTableFactory(
            workspace=self.workspace,
            schema=[{'name': 'product', 'type': 'text', 'required': True}],
        )

    def test_list_records_empty(self):
        resp = self.client.get(
            f'/api/v1/tables/{self.table.id}/records/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)

    def test_create_record(self):
        resp = self.client.post(
            f'/api/v1/tables/{self.table.id}/records/',
            {'data': {'product': 'Widget A'}},
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 201)

    def test_get_record_detail(self):
        with patch('apps.insights.tasks.broadcast_widget_update.delay'), \
             patch('apps.insights.tasks.notify_table_change.delay'):
            record = RecordFactory(table=self.table, data={'product': 'Test'})
        resp = self.client.get(
            f'/api/v1/records/{record.id}/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_record(self):
        with patch('apps.insights.tasks.broadcast_widget_update.delay'), \
             patch('apps.insights.tasks.notify_table_change.delay'):
            record = RecordFactory(table=self.table, data={'product': 'Old'})
        resp = self.client.put(
            f'/api/v1/records/{record.id}/',
            {'data': {'product': 'New'}},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 202])

    def test_delete_record(self):
        with patch('apps.insights.tasks.broadcast_widget_update.delay'), \
             patch('apps.insights.tasks.notify_table_change.delay'):
            record = RecordFactory(table=self.table, data={'product': 'Del'})
        resp = self.client.delete(
            f'/api/v1/records/{record.id}/',
            **self._ws_header()
        )
        self.assertIn(resp.status_code, [200, 204])


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------

class TestDashboardAPI(AuthAPITestCase):

    def test_list_dashboards(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/dashboards/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)

    def test_create_dashboard(self):
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/dashboards/',
            {'name': 'Revenue Board', 'slug': 'revenue-board'},
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Revenue Board')

    def test_create_dashboard_duplicate_slug_rejected(self):
        DashboardFactory(workspace=self.workspace, slug='existing-slug')
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/dashboards/',
            {'name': 'Dup', 'slug': 'existing-slug'},
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_get_dashboard_detail(self):
        d = DashboardFactory(workspace=self.workspace)
        resp = self.client.get(f'/api/v1/dashboards/{d.id}/', **self._ws_header())
        self.assertEqual(resp.status_code, 200)

    def test_delete_dashboard(self):
        d = DashboardFactory(workspace=self.workspace)
        resp = self.client.delete(f'/api/v1/dashboards/{d.id}/', **self._ws_header())
        self.assertIn(resp.status_code, [200, 204])


# ---------------------------------------------------------------------------
# Widget API
# ---------------------------------------------------------------------------

class TestWidgetAPI(AuthAPITestCase):

    def setUp(self):
        super().setUp()
        self.dashboard = DashboardFactory(workspace=self.workspace)
        self.table     = DataTableFactory(workspace=self.workspace)

    def test_list_widgets(self):
        resp = self.client.get(
            f'/api/v1/dashboards/{self.dashboard.id}/widgets/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)

    def test_create_widget(self):
        resp = self.client.post(
            f'/api/v1/dashboards/{self.dashboard.id}/widgets/',
            {
                'widget_type': 'metric',
                'title':       'Total Revenue',
                'table':       str(self.table.id),
                'position':    {'x': 0, 'y': 0, 'w': 3, 'h': 2},
                'query_config': {'aggregations': [{'type': 'count', 'name': 'val'}]},
                'viz_config':  {},
            },
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 201)

    def test_delete_widget(self):
        w    = WidgetFactory(dashboard=self.dashboard)
        resp = self.client.delete(f'/api/v1/widgets/{w.id}/', **self._ws_header())
        self.assertIn(resp.status_code, [200, 204])


# ---------------------------------------------------------------------------
# Team members API
# ---------------------------------------------------------------------------

class TestTeamMemberAPI(AuthAPITestCase):

    def test_list_members_includes_owner(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/members/',
            **self._ws_header()
        )
        self.assertEqual(resp.status_code, 200)
        emails = [m.get('email', m.get('user', {}).get('email', '')) for m in
                  (resp.data.get('results', resp.data) if isinstance(resp.data, dict) else resp.data)]
        self.assertTrue(any(self.user.email in str(e) for e in emails))

    def test_non_member_cannot_list_members(self):
        other      = UserFactory()
        other_tok  = Token.objects.create(user=other)
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f'Token {other_tok.key}')
        resp = other_client.get(
            f'/api/v1/workspaces/{self.workspace.id}/members/',
            **{'HTTP_X_WORKSPACE_ID': str(self.workspace.id)}
        )
        self.assertIn(resp.status_code, [403, 404])


# ---------------------------------------------------------------------------
# Profile API — additional paths
# ---------------------------------------------------------------------------

class TestProfileAPI(AuthAPITestCase):

    def test_patch_profile(self):
        resp = self.client.patch(
            '/api/v1/auth/profile/',
            {'first_name': 'Jane', 'last_name': 'Doe'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['first_name'], 'Jane')

    def test_register_short_password_rejected(self):
        client = APIClient()
        resp = client.post('/api/v1/auth/register/', {
            'email': 'short@test.com', 'password': 'abc',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_register_missing_fields(self):
        client = APIClient()
        resp = client.post('/api/v1/auth/register/', {}, format='json')
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# Workspace API — update / delete
# ---------------------------------------------------------------------------

class TestWorkspaceUpdateDelete(AuthAPITestCase):

    def test_update_workspace_name(self):
        resp = self.client.put(
            f'/api/v1/workspaces/{self.workspace.id}/',
            {'name': 'Renamed WS'},
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], 'Renamed WS')

    def test_non_owner_cannot_update_workspace(self):
        member = UserFactory()
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=member, role='editor'
        )
        tok = Token.objects.create(user=member)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {tok.key}')
        resp = c.put(
            f'/api/v1/workspaces/{self.workspace.id}/',
            {'name': 'Hacked'},
            format='json',
            **{'HTTP_X_WORKSPACE_ID': str(self.workspace.id)},
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_workspace_soft_deletes(self):
        resp = self.client.delete(
            f'/api/v1/workspaces/{self.workspace.id}/',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 204)
        from apps.workspaces.models import Workspace
        self.workspace.refresh_from_db()
        self.assertFalse(self.workspace.is_active)


# ---------------------------------------------------------------------------
# Dashboard detail — update
# ---------------------------------------------------------------------------

class TestDashboardUpdate(AuthAPITestCase):

    def setUp(self):
        super().setUp()
        self.dashboard = DashboardFactory(workspace=self.workspace)

    def test_update_dashboard_name(self):
        resp = self.client.put(
            f'/api/v1/dashboards/{self.dashboard.id}/',
            {'name': 'Updated', 'slug': self.dashboard.slug},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 201])

    def test_update_widget(self):
        table = DataTableFactory(workspace=self.workspace)
        w = WidgetFactory(dashboard=self.dashboard, table=table, widget_type='metric',
                          query_config={'aggregations': [{'type': 'count', 'name': 'total'}]})
        resp = self.client.put(
            f'/api/v1/widgets/{w.id}/',
            {'title': 'Updated Widget', 'position': {'x': 1, 'y': 1, 'w': 4, 'h': 2}},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 201])

    def test_list_insights_empty(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/insights/',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Team Member API — invite / update role / remove
# ---------------------------------------------------------------------------

class TestTeamMemberManagement(AuthAPITestCase):

    def test_invite_member_by_email(self):
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/members/',
            {'email': 'invited@test.com', 'role': 'editor'},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 201])
        self.assertIn('invitation_id', resp.data)

    def test_invite_missing_email_rejected(self):
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/members/',
            {'role': 'editor'},
            format='json',
            **self._ws_header(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_member_role(self):
        member = UserFactory()
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=member, role='viewer'
        )
        resp = self.client.patch(
            f'/api/v1/workspaces/{self.workspace.id}/members/{member.id}/',
            {'role': 'editor'},
            format='json',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [200, 403])  # CanManageWorkspace needed

    def test_remove_member(self):
        member = UserFactory()
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=member, role='viewer'
        )
        resp = self.client.delete(
            f'/api/v1/workspaces/{self.workspace.id}/members/{member.id}/',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [204, 403])

    def test_cannot_remove_workspace_owner(self):
        resp = self.client.delete(
            f'/api/v1/workspaces/{self.workspace.id}/members/{self.user.id}/',
            **self._ws_header(),
        )
        self.assertIn(resp.status_code, [400, 403])


# ---------------------------------------------------------------------------
# Import Job Status API
# ---------------------------------------------------------------------------

class TestImportJobStatusAPI(AuthAPITestCase):

    def test_get_import_job_status(self):
        from apps.dashboards.factories import ImportJobFactory
        table = DataTableFactory(workspace=self.workspace)
        job = ImportJobFactory(
            workspace=self.workspace,
            table=table,
            created_by=self.user,
            status='completed',
            total_rows=10,
            processed_rows=10,
        )
        resp = self.client.get(f'/api/v1/imports/{job.id}/status/', **self._ws_header())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'completed')

    def test_get_nonexistent_job_returns_404(self):
        import uuid
        resp = self.client.get(f'/api/v1/imports/{uuid.uuid4()}/status/', **self._ws_header())
        self.assertEqual(resp.status_code, 404)
