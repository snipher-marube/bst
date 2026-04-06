"""
config/settings/test.py
========================
Test-only settings that override development to use SQLite (no CREATEDB needed)
and in-memory caches/email so the test suite runs anywhere.

Usage:
    python manage.py test --settings=config.settings.test
    OR set in pytest.ini: DJANGO_SETTINGS_MODULE = config.settings.test
"""
from .development import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Database — SQLite for tests (no CREATEDB privilege required)
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   BASE_DIR / 'test_db.sqlite3',  # noqa: F405
    }
}

# ---------------------------------------------------------------------------
# Cache — in-memory so tests are isolated and fast
# ---------------------------------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    },
    'sessions': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'test-sessions',
    },
}

# ---------------------------------------------------------------------------
# Email — capture in memory; inspect via django.core.mail.outbox
# ---------------------------------------------------------------------------
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# ---------------------------------------------------------------------------
# Celery — run tasks synchronously in the same process (no broker needed)
# ---------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER  = True
CELERY_TASK_EAGER_PROPAGATES = False   # don't let task errors crash tests

# ---------------------------------------------------------------------------
# Channels — use in-memory layer so WebSocket tests don't need Redis
# ---------------------------------------------------------------------------
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

# ---------------------------------------------------------------------------
# Passwords — use fast hasher so user creation is instant in tests
# ---------------------------------------------------------------------------
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# ---------------------------------------------------------------------------
# Allauth — don't require email confirmation in tests
# ---------------------------------------------------------------------------
ACCOUNT_EMAIL_VERIFICATION = 'none'

# ---------------------------------------------------------------------------
# Silence migration output
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'handlers': {'null': {'class': 'logging.NullHandler'}},
    'root': {'handlers': ['null']},
}

# ---------------------------------------------------------------------------
# Debug toolbar — must be disabled during tests
# ---------------------------------------------------------------------------
DEBUG_TOOLBAR_CONFIG = {
    'IS_RUNNING_TESTS': False,
    'SHOW_TOOLBAR_CALLBACK': lambda _request: False,
}
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'debug_toolbar']  # noqa: F405
MIDDLEWARE = [m for m in MIDDLEWARE if 'debug_toolbar' not in m]  # noqa: F405
