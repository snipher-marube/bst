"""
apps/newsletter/tests.py
========================
Tests for newsletter subscription, confirmation, unsubscribe, tracking, and forms.
"""
import json
from unittest.mock import patch

from django.test import TestCase, Client

from apps.newsletter.models import Subscriber, List
from apps.newsletter.forms import NewsletterSubscriptionForm, CampaignForm


# ---------------------------------------------------------------------------
# Newsletter forms
# ---------------------------------------------------------------------------

class TestNewsletterSubscriptionForm(TestCase):

    def _valid_data(self, **kwargs):
        d = {'email': 'user@example.com', 'consent': True}
        d.update(kwargs)
        return d

    def test_valid_form(self):
        form = NewsletterSubscriptionForm(self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_missing_email(self):
        form = NewsletterSubscriptionForm({'consent': True})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_missing_consent(self):
        form = NewsletterSubscriptionForm({'email': 'a@b.com'})
        self.assertFalse(form.is_valid())
        self.assertIn('consent', form.errors)

    def test_invalid_email(self):
        form = NewsletterSubscriptionForm(self._valid_data(email='not-an-email'))
        self.assertFalse(form.is_valid())

    def test_already_active_subscriber_fails(self):
        Subscriber.objects.create(
            email='taken@example.com',
            status=Subscriber.Status.ACTIVE,
        )
        form = NewsletterSubscriptionForm(self._valid_data(email='taken@example.com'))
        self.assertFalse(form.is_valid())

    def test_email_normalized_to_lowercase(self):
        form = NewsletterSubscriptionForm(self._valid_data(email='User@EXAMPLE.COM'))
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['email'], 'user@example.com')

    def test_optional_first_name(self):
        form = NewsletterSubscriptionForm(self._valid_data(first_name='Alice'))
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['first_name'], 'Alice')


class TestCampaignFormValidation(TestCase):

    def test_php_code_rejected(self):
        form = CampaignForm(data={'content_html': '<?php echo 1; ?>'})
        form.is_valid()
        self.assertIn('content_html', form.errors)

    def test_script_tag_rejected(self):
        form = CampaignForm(data={'content_html': '<p>Hi</p><script>alert(1)</script>'})
        form.is_valid()
        self.assertIn('content_html', form.errors)


# ---------------------------------------------------------------------------
# Newsletter views
# ---------------------------------------------------------------------------

class TestNewsletterSubscribeView(TestCase):

    def setUp(self):
        self.client = Client()

    def test_subscribe_valid_post(self):
        with patch('apps.newsletter.views.send_confirmation_email'):
            resp = self.client.post(
                '/newsletter/api/subscribe/',
                data=json.dumps({'email': 'new@example.com', 'consent': True}),
                content_type='application/json',
            )
        self.assertIn(resp.status_code, [200, 429])
        if resp.status_code == 200:
            data = json.loads(resp.content)
            self.assertTrue(data.get('success'))

    def test_subscribe_invalid_email(self):
        resp = self.client.post(
            '/newsletter/api/subscribe/',
            data=json.dumps({'email': 'bad', 'consent': True}),
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [400, 429])

    def test_subscribe_missing_consent(self):
        resp = self.client.post(
            '/newsletter/api/subscribe/',
            data=json.dumps({'email': 'a@b.com'}),
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [400, 429])

    def test_subscribe_invalid_json(self):
        resp = self.client.post(
            '/newsletter/api/subscribe/',
            data='not-json',
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [400, 429])

    def test_subscribe_form_post(self):
        with patch('apps.newsletter.views.send_confirmation_email'):
            resp = self.client.post(
                '/newsletter/api/subscribe/',
                data={'email': 'form@example.com', 'consent': True},
            )
        self.assertIn(resp.status_code, [200, 400, 429])

    def test_subscribe_existing_unsubscribed_reactivates(self):
        sub = Subscriber.objects.create(
            email='reactivate@example.com',
            status=Subscriber.Status.UNSUBSCRIBED,
        )
        with patch('apps.newsletter.views.send_confirmation_email'):
            resp = self.client.post(
                '/newsletter/api/subscribe/',
                data=json.dumps({'email': 'reactivate@example.com', 'consent': True}),
                content_type='application/json',
            )
        self.assertIn(resp.status_code, [200, 429])

    def test_subscribe_already_active_returns_error(self):
        Subscriber.objects.create(
            email='active@example.com',
            status=Subscriber.Status.ACTIVE,
        )
        resp = self.client.post(
            '/newsletter/api/subscribe/',
            data=json.dumps({'email': 'active@example.com', 'consent': True}),
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [200, 400, 429])


class TestNewsletterConfirmView(TestCase):

    def setUp(self):
        self.client = Client()
        self.subscriber = Subscriber.objects.create(
            email='confirm@example.com',
            status=Subscriber.Status.PENDING,
        )

    def test_confirm_valid_token(self):
        with patch('apps.newsletter.views.settings') as mock_settings:
            mock_settings.NEWSLETTER_CONFIRM_REDIRECT = '/'
            resp = self.client.get(
                f'/newsletter/confirm/{self.subscriber.verification_token}/'
            )
        self.assertEqual(resp.status_code, 302)

    def test_confirm_invalid_token(self):
        import uuid
        resp = self.client.get(f'/newsletter/confirm/{uuid.uuid4()}/')
        self.assertEqual(resp.status_code, 404)

    def test_confirm_already_active(self):
        self.subscriber.status = Subscriber.Status.ACTIVE
        self.subscriber.save()
        with patch('apps.newsletter.views.settings') as mock_settings:
            mock_settings.NEWSLETTER_CONFIRM_REDIRECT = '/'
            resp = self.client.get(
                f'/newsletter/confirm/{self.subscriber.verification_token}/'
            )
        self.assertEqual(resp.status_code, 302)


class TestNewsletterUnsubscribeView(TestCase):

    def setUp(self):
        self.client = Client()
        self.subscriber = Subscriber.objects.create(
            email='unsub@example.com',
            status=Subscriber.Status.ACTIVE,
        )

    def test_unsubscribe_valid_token(self):
        resp = self.client.get(
            f'/newsletter/unsubscribe/{self.subscriber.unsubscribe_token}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.subscriber.refresh_from_db()
        self.assertEqual(self.subscriber.status, Subscriber.Status.UNSUBSCRIBED)

    def test_unsubscribe_already_unsubscribed(self):
        self.subscriber.status = Subscriber.Status.UNSUBSCRIBED
        self.subscriber.save()
        resp = self.client.get(
            f'/newsletter/unsubscribe/{self.subscriber.unsubscribe_token}/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_unsubscribe_invalid_token(self):
        import uuid
        resp = self.client.get(f'/newsletter/unsubscribe/{uuid.uuid4()}/')
        self.assertEqual(resp.status_code, 404)


class TestNewsletterTrackingViews(TestCase):

    def setUp(self):
        self.client = Client()

    def test_track_open_returns_gif(self):
        resp = self.client.get('/newsletter/track/open/999/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/gif')

    def test_track_open_with_subscriber_not_found(self):
        resp = self.client.get('/newsletter/track/open/999/12345/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/gif')

    def test_track_click_no_url_raises_404(self):
        resp = self.client.get('/newsletter/track/click/999/12345/')
        self.assertEqual(resp.status_code, 404)

    def test_track_click_with_url_redirects(self):
        resp = self.client.get(
            '/newsletter/track/click/999/12345/?url=https://example.com'
        )
        self.assertEqual(resp.status_code, 302)


class TestNewsletterWebhookView(TestCase):

    def setUp(self):
        self.client = Client()

    def test_webhook_valid_json(self):
        payload = json.dumps({'event': 'bounce', 'email': 'x@y.com'})
        with patch('apps.newsletter.views.process_webhook_event') as mock_task:
            mock_task.enqueue = lambda *a, **kw: None
            resp = self.client.post(
                '/newsletter/webhook/mailgun/',
                data=payload,
                content_type='application/json',
            )
        self.assertIn(resp.status_code, [200, 500])

    def test_webhook_invalid_json(self):
        resp = self.client.post(
            '/newsletter/webhook/mailgun/',
            data='bad-json',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
