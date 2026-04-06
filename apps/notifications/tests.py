"""
apps/notifications/tests.py
============================
Tests for Notification, NotificationPreference, and the email delivery layer.
"""
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, NotificationFactory, NotificationPreferenceFactory,
)
from apps.notifications.models import Notification, NotificationPreference


# ---------------------------------------------------------------------------
# Notification model
# ---------------------------------------------------------------------------

class TestNotificationModel(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory()

    def test_notify_creates_notification(self):
        n = Notification.notify(
            user=self.user,
            title='Test',
            message='Hello',
            notif_type='info',
            send_email=False,
        )
        self.assertIsNotNone(n.pk)
        self.assertEqual(n.title, 'Test')
        self.assertEqual(n.user, self.user)
        self.assertEqual(n.notif_type, 'info')
        self.assertFalse(n.is_read)

    def test_notify_with_workspace(self):
        n = Notification.notify(
            user=self.user, title='Hi', message='Msg',
            workspace=self.workspace, send_email=False,
        )
        self.assertEqual(n.workspace, self.workspace)

    def test_notify_with_action_url_and_metadata(self):
        n = Notification.notify(
            user=self.user, title='T', message='M',
            action_url='/dashboard/', metadata={'key': 'value'},
            send_email=False,
        )
        self.assertEqual(n.action_url, '/dashboard/')
        self.assertEqual(n.metadata['key'], 'value')

    def test_notify_calls_send_email_when_flag_true(self):
        with patch('apps.notifications.emails.send_notification_email') as mock_send:
            Notification.notify(
                user=self.user, title='T', message='M', send_email=True
            )
        mock_send.assert_called_once()

    def test_notify_skips_email_when_flag_false(self):
        with patch('apps.notifications.emails.send_notification_email') as mock_send:
            Notification.notify(
                user=self.user, title='T', message='M', send_email=False
            )
        mock_send.assert_not_called()

    def test_notify_email_failure_does_not_raise(self):
        """A broken email backend must never crash the notify() call."""
        with patch('apps.notifications.emails.send_notification_email',
                   side_effect=Exception('SMTP error')):
            # Should not raise
            n = Notification.notify(user=self.user, title='T', message='M', send_email=True)
        self.assertIsNotNone(n.pk)

    def test_str_representation(self):
        n = NotificationFactory(user=self.user, notif_type='success')
        s = str(n)
        self.assertIn('success', s)
        self.assertIn(self.user.email, s)

    def test_all_notif_types_valid(self):
        valid = [c[0] for c in Notification.NOTIF_TYPES]
        for t in ['info', 'success', 'warning', 'error', 'invite', 'import', 'insight']:
            self.assertIn(t, valid)

    def test_ordering_newest_first(self):
        n1 = Notification.notify(user=self.user, title='First',  message='', send_email=False)
        n2 = Notification.notify(user=self.user, title='Second', message='', send_email=False)
        qs = Notification.objects.filter(user=self.user)
        self.assertEqual(qs.first().title, 'Second')


# ---------------------------------------------------------------------------
# NotificationPreference model
# ---------------------------------------------------------------------------

class TestNotificationPreference(TestCase):

    def setUp(self):
        self.user = UserFactory()

    def test_get_or_create_uses_defaults(self):
        prefs, created = NotificationPreference.objects.get_or_create(user=self.user)
        self.assertTrue(created)
        self.assertTrue(prefs.email_invites)
        self.assertTrue(prefs.email_imports)
        self.assertFalse(prefs.email_insights)   # off by default (noisy)
        self.assertTrue(prefs.email_system)

    def test_allows_email_invite_type(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_invites=True)
        self.assertTrue(prefs.allows_email('invite'))

    def test_blocks_email_when_opt_out(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_invites=False)
        self.assertFalse(prefs.allows_email('invite'))

    def test_allows_email_import_type(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_imports=True)
        self.assertTrue(prefs.allows_email('import'))

    def test_blocks_email_import_when_opted_out(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_imports=False)
        self.assertFalse(prefs.allows_email('import'))

    def test_allows_email_insight_when_enabled(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_insights=True)
        self.assertTrue(prefs.allows_email('insight'))

    def test_blocks_email_insight_by_default(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_insights=False)
        self.assertFalse(prefs.allows_email('insight'))

    def test_system_type_covers_info_success_warning_error(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_system=True)
        for t in ['info', 'success', 'warning', 'error']:
            self.assertTrue(prefs.allows_email(t), f"Expected email allowed for type={t}")

    def test_system_opt_out_blocks_all_system_types(self):
        prefs = NotificationPreferenceFactory(user=self.user, email_system=False)
        for t in ['info', 'success', 'warning', 'error']:
            self.assertFalse(prefs.allows_email(t))

    def test_unknown_type_defaults_to_allow(self):
        prefs = NotificationPreferenceFactory(user=self.user)
        self.assertTrue(prefs.allows_email('unknown_type'))


# ---------------------------------------------------------------------------
# Email sending layer
# ---------------------------------------------------------------------------

class TestSendNotificationEmail(TestCase):

    def setUp(self):
        self.user = UserFactory()
        NotificationPreference.objects.create(
            user=self.user,
            email_invites=True,
            email_imports=True,
            email_insights=False,
            email_system=True,
        )

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_email_sent_for_opted_in_type(self):
        from django.core import mail
        from apps.notifications.emails import send_notification_email

        n = Notification.objects.create(
            user=self.user, title='Import Done', message='Your file is ready.',
            notif_type='import', action_url='/dashboard/tables/'
        )
        send_notification_email(n)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)
        self.assertEqual(mail.outbox[0].subject, 'Import Done')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_email_not_sent_for_opted_out_type(self):
        from django.core import mail
        from apps.notifications.emails import send_notification_email

        n = Notification.objects.create(
            user=self.user, title='Insight', message='New insight available.',
            notif_type='insight'
        )
        send_notification_email(n)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_generic_email_sent_for_info_type(self):
        from django.core import mail
        from apps.notifications.emails import send_notification_email

        n = Notification.objects.create(
            user=self.user, title='System Notice', message='All good.',
            notif_type='info'
        )
        send_notification_email(n)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_invite_email_includes_workspace_name(self):
        from django.core import mail
        from apps.notifications.emails import send_notification_email
        from apps.workspaces.models import Workspace
        from apps.dashboards.factories import WorkspaceFactory

        ws = WorkspaceFactory()
        n = Notification.objects.create(
            user=self.user, title='You are invited', message='Join us!',
            notif_type='invite', workspace=ws
        )
        send_notification_email(n)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(ws.name, mail.outbox[0].alternatives[0][0])
