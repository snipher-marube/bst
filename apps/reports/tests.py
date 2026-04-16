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


# ===========================================================================
# ReportSchedule — model + task tests
# ===========================================================================

import uuid as _uuid
from unittest.mock import patch, MagicMock
from django.utils import timezone

from apps.reports.models import ReportSchedule
from apps.reports.tasks import process_report_schedules, deliver_scheduled_report


class TestReportScheduleModel(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.dashboard_id = _uuid.uuid4()

    def _make_schedule(self, **kwargs):
        defaults = dict(
            workspace=self.workspace,
            dashboard_id=self.dashboard_id,
            name='Daily Sales',
            cron_expression='0 8 * * *',
            recipients=['alice@example.com'],
            is_active=True,
            created_by=self.user,
        )
        defaults.update(kwargs)
        return ReportSchedule.objects.create(**defaults)

    # ------------------------------------------------------------------
    # compute_next_send
    # ------------------------------------------------------------------

    def test_compute_next_send_returns_future_datetime(self):
        schedule = self._make_schedule()
        nxt = schedule.compute_next_send()
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, timezone.now())

    def test_compute_next_send_invalid_cron_returns_none(self):
        schedule = self._make_schedule(cron_expression='not a cron')
        self.assertIsNone(schedule.compute_next_send())

    # ------------------------------------------------------------------
    # process_report_schedules Beat task
    # ------------------------------------------------------------------

    def test_process_schedules_fires_due_schedules(self):
        past = timezone.now() - timezone.timedelta(minutes=1)
        schedule = self._make_schedule(next_send_at=past)

        with patch('apps.reports.tasks.generate_pdf_report') as _gen, \
             patch('apps.reports.tasks.deliver_scheduled_report') as mock_deliver:
            mock_deliver.delay = MagicMock()
            result = process_report_schedules()

        self.assertEqual(result['triggered'], 1)
        self.assertEqual(result['skipped'],   0)
        mock_deliver.delay.assert_called_once()

        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.last_sent_at)
        # next_send_at should have been advanced past the original value
        self.assertGreater(schedule.next_send_at, past)

    def test_process_schedules_skips_future_schedules(self):
        future = timezone.now() + timezone.timedelta(hours=1)
        self._make_schedule(next_send_at=future)

        with patch('apps.reports.tasks.deliver_scheduled_report') as mock_deliver:
            mock_deliver.delay = MagicMock()
            result = process_report_schedules()

        self.assertEqual(result['triggered'], 0)
        mock_deliver.delay.assert_not_called()

    def test_process_schedules_skips_inactive_schedules(self):
        past = timezone.now() - timezone.timedelta(minutes=1)
        self._make_schedule(next_send_at=past, is_active=False)

        with patch('apps.reports.tasks.deliver_scheduled_report') as mock_deliver:
            mock_deliver.delay = MagicMock()
            result = process_report_schedules()

        self.assertEqual(result['triggered'], 0)
        mock_deliver.delay.assert_not_called()

    # ------------------------------------------------------------------
    # deliver_scheduled_report task
    # ------------------------------------------------------------------

    def test_deliver_sends_email_when_job_done(self):
        job = ReportJob.objects.create(
            workspace=self.workspace,
            dashboard_id=self.dashboard_id,
            requested_by=self.user,
            status=ReportJob.STATUS_DONE,
            pdf_path='',   # no actual file — email sent without attachment
        )
        with patch('apps.reports.tasks.os.path.exists', return_value=False), \
             patch('apps.reports.tasks.EmailMessage') as MockEmail:
            mock_msg = MagicMock()
            MockEmail.return_value = mock_msg
            deliver_scheduled_report(
                str(job.id),
                str(_uuid.uuid4()),
                ['bob@example.com'],
                'pdf',
            )
        mock_msg.send.assert_called_once_with(fail_silently=False)

    def test_deliver_skips_when_no_recipients(self):
        job = ReportJob.objects.create(
            workspace=self.workspace,
            dashboard_id=self.dashboard_id,
            requested_by=self.user,
            status=ReportJob.STATUS_DONE,
        )
        with patch('apps.reports.tasks.EmailMessage') as MockEmail:
            deliver_scheduled_report(str(job.id), str(_uuid.uuid4()), [], 'pdf')
        MockEmail.assert_not_called()

    def test_deliver_skips_when_job_failed(self):
        job = ReportJob.objects.create(
            workspace=self.workspace,
            dashboard_id=self.dashboard_id,
            requested_by=self.user,
            status=ReportJob.STATUS_FAILED,
        )
        with patch('apps.reports.tasks.EmailMessage') as MockEmail:
            deliver_scheduled_report(str(job.id), str(_uuid.uuid4()), ['c@example.com'], 'pdf')
        MockEmail.assert_not_called()
