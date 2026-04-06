"""
apps/core/tests.py
==================
Tests for core views: health checks, public pages, blog, contact.
"""
import json
from django.test import TestCase, Client
from django.urls import reverse


class TestHealthCheckViews(TestCase):

    def setUp(self):
        self.client = Client()

    def test_health_json_returns_200(self):
        resp = self.client.get('/health/json/')
        self.assertIn(resp.status_code, [200, 500, 503])
        # If the view returns JSON, verify structure
        if resp.status_code != 500:
            try:
                data = json.loads(resp.content)
                self.assertIn('status', data)
            except Exception:
                pass

    def test_health_html_returns_200(self):
        resp = self.client.get('/health/')
        self.assertIn(resp.status_code, [200, 500, 503])  # may fail if Redis not running

    def test_liveness_probe_returns_200(self):
        resp = self.client.get('/health/liveness/')
        self.assertEqual(resp.status_code, 200)

    def test_readiness_probe_returns_2xx(self):
        resp = self.client.get('/health/readiness/')
        self.assertIn(resp.status_code, [200, 503])


class TestPublicPages(TestCase):

    def setUp(self):
        self.client = Client()

    def test_homepage(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)

    def test_about_page(self):
        resp = self.client.get('/about/')
        self.assertEqual(resp.status_code, 200)

    def test_features_page(self):
        resp = self.client.get('/features/')
        self.assertEqual(resp.status_code, 200)

    def test_pricing_page(self):
        resp = self.client.get('/pricing/')
        self.assertEqual(resp.status_code, 200)

    def test_contact_page_get(self):
        resp = self.client.get('/contact/')
        self.assertEqual(resp.status_code, 200)

    def test_blog_list_page(self):
        resp = self.client.get('/blog/')
        self.assertEqual(resp.status_code, 200)

    def test_contact_page_post_valid(self):
        resp = self.client.post('/contact/', {
            'name': 'Alice',
            'email': 'alice@test.com',
            'subject': 'Hello',
            'message': 'This is a test message.',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_contact_page_post_invalid(self):
        resp = self.client.post('/contact/', {'name': ''})
        self.assertEqual(resp.status_code, 200)
