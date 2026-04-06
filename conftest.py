"""
Project-level pytest configuration.
Shared fixtures used across all test modules.
"""
import django
from django.conf import settings


def pytest_configure(config):
    """Ensure Django is configured before tests run."""
    pass  # DJANGO_SETTINGS_MODULE set in pytest.ini
