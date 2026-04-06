"""
apps/reports/tests.py
=====================
Tests for report generation views.
"""
from unittest.mock import patch

from django.test import TestCase, Client

from apps.dashboards.factories import UserFactory, WorkspaceFactory, DashboardFactory
from apps.reports.models import ReportJob


class TestGenerateReportView(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        # Attach workspace to user's session via middleware convention
        self.client.session['current_workspace_id'] = str(self.workspace.id)
        session = self.client.session
        session.save()
        self.user.current_workspace = self.workspace
        self.dashboard = DashboardFactory(workspace=self.workspace)

    def test_generate_report_no_workspace_returns_400(self):
        self.user.current_workspace = None
        with patch('apps.reports.views._get_workspace', return_value=None):
            resp = self.client.post(f'/reports/dashboard/{self.dashboard.id}/generate/')
        self.assertEqual(resp.status_code, 400)

    def test_generate_report_valid_returns_202(self):
        with patch('apps.reports.views._get_workspace', return_value=self.workspace), \
             patch('apps.reports.tasks.generate_pdf_report.delay'):
            resp = self.client.post(f'/reports/dashboard/{self.dashboard.id}/generate/')
        self.assertEqual(resp.status_code, 202)
        import json
        data = json.loads(resp.content)
        self.assertIn('job_id', data)

    def test_generate_report_unauthenticated_redirects(self):
        c = Client()
        resp = c.post(f'/reports/dashboard/{self.dashboard.id}/generate/')
        self.assertEqual(resp.status_code, 302)

    def test_generate_report_wrong_dashboard_404(self):
        import uuid
        with patch('apps.reports.views._get_workspace', return_value=self.workspace):
            resp = self.client.post(f'/reports/dashboard/{uuid.uuid4()}/generate/')
        self.assertEqual(resp.status_code, 404)


class TestReportJobStatusView(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        self.dashboard = DashboardFactory(workspace=self.workspace)
        self.job = ReportJob.objects.create(
            workspace=self.workspace,
            dashboard_id=self.dashboard.pk,
            requested_by=self.user,
        )

    def test_status_view_returns_json(self):
        with patch('apps.reports.views._get_workspace', return_value=self.workspace):
            resp = self.client.get(f'/reports/job/{self.job.id}/status/')
        self.assertEqual(resp.status_code, 200)
        import json
        data = json.loads(resp.content)
        self.assertEqual(str(data['job_id']), str(self.job.id))

    def test_status_view_unauthenticated_redirects(self):
        c = Client()
        resp = c.get(f'/reports/job/{self.job.id}/status/')
        self.assertEqual(resp.status_code, 302)

    def test_status_404_wrong_workspace(self):
        with patch('apps.reports.views._get_workspace', return_value=None):
            resp = self.client.get(f'/reports/job/{self.job.id}/status/')
        self.assertEqual(resp.status_code, 404)
