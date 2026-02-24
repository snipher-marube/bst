from django.test import TestCase
from django.contrib.auth import get_user_model
from .models import Workspace, DataTable, Record
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