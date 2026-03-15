from django.test import TestCase
from django.contrib.auth import get_user_model
from .models import Workspace, DataTable, Record, Dashboard, Widget
import json

User = get_user_model()

class WorkspaceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.workspace = Workspace.objects.create(
            name='Test Workspace',
            owner=self.user
        )
    
    def test_workspace_creation(self):
        self.assertEqual(self.workspace.name, 'Test Workspace')
        self.assertEqual(self.workspace.owner, self.user)
        self.assertEqual(self.workspace.tier, 'free')
    
    def test_workspace_membership(self):
        self.workspace.members.add(self.user, through_defaults={'role': 'owner'})
        self.assertTrue(self.workspace.members.filter(id=self.user.id).exists())
    
    def test_table_limits(self):
        self.assertTrue(self.workspace.can_add_table())
        # Add 5 tables (max for free tier)
        for i in range(5):
            DataTable.objects.create(
                workspace=self.workspace,
                name=f'Table {i}',
                created_by=self.user
            )
        self.assertFalse(self.workspace.can_add_table())

class DataTableTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.workspace = Workspace.objects.create(
            name='Test Workspace',
            owner=self.user
        )
        self.table = DataTable.objects.create(
            workspace=self.workspace,
            name='Test Table',
            schema=[
                {'name': 'name', 'type': 'text', 'required': True},
                {'name': 'age', 'type': 'number', 'required': False}
            ],
            created_by=self.user
        )
    
    def test_record_validation(self):
        # Valid record
        is_valid, errors = self.table.validate_record({
            'name': 'John Doe',
            'age': 30
        })
        self.assertTrue(is_valid)
        
        # Missing required field
        is_valid, errors = self.table.validate_record({
            'age': 30
        })
        self.assertFalse(is_valid)
        self.assertIn('name', errors[0])
        
        # Wrong type
        is_valid, errors = self.table.validate_record({
            'name': 'John Doe',
            'age': 'thirty'
        })
        self.assertFalse(is_valid)
    
    def test_record_creation(self):
        record = Record.objects.create(
            table=self.table,
            data={'name': 'Jane Doe', 'age': 25},
            created_by=self.user
        )
        self.assertEqual(record.data['name'], 'Jane Doe')
        self.assertEqual(self.table.record_count, 1)

class APITestCase(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)
    
    def test_create_workspace(self):
        response = self.client.post('/api/workspaces/', {
            'name': 'API Test Workspace'
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['name'], 'API Test Workspace')
    
    def test_create_table(self):
        # First create workspace
        workspace_response = self.client.post('/api/workspaces/', {
            'name': 'Test Workspace'
        })
        workspace_id = workspace_response.json()['id']
        
        # Create table
        response = self.client.post('/api/tables/', {
            'workspace': workspace_id,
            'name': 'Test Table',
            'schema': [
                {'name': 'name', 'type': 'text', 'required': True}
            ]
        }, headers={'X-Workspace-ID': workspace_id})
        
        self.assertEqual(response.status_code, 201)

class QueryEngineFilterTestCase(TestCase):
    """Tests for QueryEngine._apply_filters() and _execute_chart_query() fixes."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='qe_user',
            email='qe@example.com',
            password='testpass123'
        )
        self.workspace = Workspace.objects.create(
            name='QE Workspace',
            owner=self.user
        )
        self.table = DataTable.objects.create(
            workspace=self.workspace,
            name='QE Table',
            schema=[
                {'name': 'value', 'type': 'number', 'required': False},
                {'name': 'category', 'type': 'text', 'required': False},
            ],
            created_by=self.user
        )
        # Create some records
        for i in range(5):
            Record.objects.create(
                table=self.table,
                data={'value': i * 10, 'category': 'a' if i % 2 == 0 else 'b'},
                created_by=self.user
            )

    def test_apply_filters_eq(self):
        from .services import QueryEngine
        from .models import Record
        engine = QueryEngine()
        qs = Record.objects.filter(table=self.table, is_active=True)
        filtered = engine._apply_filters(qs, [{'field': 'category', 'operator': 'eq', 'value': 'a'}])
        self.assertEqual(filtered.count(), 3)

    def test_apply_filters_ne(self):
        from .services import QueryEngine
        from .models import Record
        engine = QueryEngine()
        qs = Record.objects.filter(table=self.table, is_active=True)
        filtered = engine._apply_filters(qs, [{'field': 'category', 'operator': 'ne', 'value': 'a'}])
        self.assertEqual(filtered.count(), 2)

    def test_apply_filters_empty(self):
        from .services import QueryEngine
        from .models import Record
        engine = QueryEngine()
        qs = Record.objects.filter(table=self.table, is_active=True)
        filtered = engine._apply_filters(qs, [])
        self.assertEqual(filtered.count(), 5)

    def test_execute_chart_query_min_max(self):
        """Test that min/max aggregations don't use locals() for variable checks."""
        from .services import QueryEngine
        engine = QueryEngine()

        self.table.workspace.members.add(self.user, through_defaults={'role': 'member'})
        dashboard = Dashboard.objects.create(
            workspace=self.workspace,
            name='Test Dash',
            created_by=self.user
        )
        widget = Widget.objects.create(
            dashboard=dashboard,
            widget_type='metric',
            title='Min Widget',
            table=self.table,
            query_config={
                'aggregations': [
                    {'type': 'min', 'field': 'value', 'name': 'min_val'},
                    {'type': 'max', 'field': 'value', 'name': 'max_val'},
                ]
            },
            viz_config={},
            position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )
        result = engine._execute_chart_query(widget, limit=100)
        self.assertEqual(result.get('min_val'), 0.0)
        self.assertEqual(result.get('max_val'), 40.0)


class DashboardSerializerTestCase(TestCase):
    """Tests for DashboardSerializer widget serialization."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='ds_user',
            email='ds@example.com',
            password='testpass123'
        )
        self.workspace = Workspace.objects.create(
            name='DS Workspace',
            owner=self.user
        )
        self.workspace.members.add(self.user, through_defaults={'role': 'owner'})
        self.table = DataTable.objects.create(
            workspace=self.workspace,
            name='DS Table',
            schema=[{'name': 'val', 'type': 'number', 'required': False}],
            created_by=self.user
        )
        self.dashboard = Dashboard.objects.create(
            workspace=self.workspace,
            name='DS Dashboard',
            created_by=self.user
        )

    def test_get_widgets_returns_list(self):
        """get_widgets() should return a list even when there are no widgets."""
        from .serializers import DashboardSerializer
        from django.test import RequestFactory
        factory = RequestFactory()
        request = factory.get('/')
        request.user = self.user
        request.user.current_workspace = self.workspace
        serializer = DashboardSerializer(self.dashboard, context={'request': request})
        widgets = serializer.data['widgets']
        self.assertIsInstance(widgets, list)
        self.assertEqual(len(widgets), 0)

    def test_get_widgets_includes_created_widget(self):
        """Widgets created in DB should appear in serialized data."""
        Widget.objects.create(
            dashboard=self.dashboard,
            widget_type='metric',
            title='Test Widget',
            table=self.table,
            query_config={'aggregations': [{'type': 'count', 'name': 'val'}]},
            viz_config={},
            position={'x': 0, 'y': 0, 'w': 4, 'h': 4},
        )
        from .serializers import DashboardSerializer
        from django.test import RequestFactory
        factory = RequestFactory()
        request = factory.get('/')
        request.user = self.user
        request.user.current_workspace = self.workspace
        serializer = DashboardSerializer(self.dashboard, context={'request': request})
        widgets = serializer.data['widgets']
        self.assertEqual(len(widgets), 1)
        self.assertEqual(widgets[0]['title'], 'Test Widget')
