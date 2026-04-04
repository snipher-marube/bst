from .base import *
from decouple import config
import os

DEBUG = True

ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = ['http://localhost:8000', 'http://127.0.0.1:8000']

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("PG_DATABASE_NAME_DEV"),
        "USER": config("PG_DATABASE_USER_DEV"),
        "PASSWORD": config("PG_DATABASE_PASSWORD_DEV"),
        "HOST": config("PG_DATABASE_HOST_DEV"),
        "PORT": config("PG_DATABASE_PORT_DEV"),
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": 0,   # no pooling in dev — fresh connection per request
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Redis — read from env so Docker container names work
# ─────────────────────────────────────────────────────────────────────────────
REDIS_URL  = os.environ.get('REDIS_URL',  'redis://localhost:6379/0')
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

CACHES['default']['LOCATION']  = REDIS_URL
CACHES['sessions']['LOCATION'] = REDIS_URL   # same DB, isolated by KEY_PREFIX

CELERY_BROKER_URL    = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

CHANNEL_LAYERS['default']['CONFIG']['hosts'] = [(REDIS_HOST, REDIS_PORT)]

# ─────────────────────────────────────────────────────────────────────────────
# Debug Toolbar
# ─────────────────────────────────────────────────────────────────────────────
INSTALLED_APPS += ['debug_toolbar']
MIDDLEWARE.insert(1, 'debug_toolbar.middleware.DebugToolbarMiddleware')

INTERNAL_IPS = [
    '127.0.0.1',
    '::1',
]

# Docker host — lets debug toolbar show when using docker-compose
import socket
try:
    hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
    INTERNAL_IPS += [ip[: ip.rfind('.')] + '.1' for ip in ips]
except OSError:
    pass

DEBUG_TOOLBAR_CONFIG = {
    'SHOW_COLLAPSED': True,
    'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG,
}

# ─────────────────────────────────────────────────────────────────────────────
# Static / media
# ─────────────────────────────────────────────────────────────────────────────
MEDIA_URL = '/media/'
STATICFILES_DIRS = [BASE_DIR / '../static']
MEDIA_ROOT = BASE_DIR / '../static/media'
STATIC_ROOT = BASE_DIR / '../staticfiles'

# ─────────────────────────────────────────────────────────────────────────────
# Email — use console in dev unless overridden
# ─────────────────────────────────────────────────────────────────────────────
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend',
)

# ─────────────────────────────────────────────────────────────────────────────
# Auth — relax rate limits in development for easier testing
# ─────────────────────────────────────────────────────────────────────────────
ACCOUNT_RATE_LIMITS = {
    'login_failed': None,
    'signup': None,
}
