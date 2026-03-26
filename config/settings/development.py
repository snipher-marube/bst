from .base import *
from decouple import config
import os

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = ['http://localhost:8000', 'http://127.0.0.1:8000']
CORS_ALLOWED_ORIGINS = ['http://localhost:8000', 'http://127.0.0.1:8000']

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("PG_DATABASE_NAME_DEV"),
        "USER": config("PG_DATABASE_USER_DEV"),
        "PASSWORD": config("PG_DATABASE_PASSWORD_DEV"),
        "HOST": config("PG_DATABASE_HOST_DEV"),
        "PORT": config("PG_DATABASE_PORT_DEV"),
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": 600,
    }
}

# Override Redis settings with environment variables (for Docker compatibility)
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

# Update cache settings
CACHES['default']['LOCATION'] = REDIS_URL
CACHES['sessions']['LOCATION'] = f'{REDIS_URL}/1'

# Update Celery settings
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# Update Channel Layers
CHANNEL_LAYERS['default']['CONFIG']['hosts'] = [(REDIS_HOST, REDIS_PORT)]

# Media and static files
MEDIA_URL = '/media/'
STATICFILES_DIRS = [BASE_DIR / '../static']

MEDIA_ROOT = BASE_DIR / '../static/media'
STATIC_ROOT = BASE_DIR / '../staticfiles'

# Email backend for development - use console to avoid sending real emails
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Disable rate limiting in development for easier testing
ACCOUNT_RATE_LIMITS = {
    'login_failed': None,
    'signup': None,
}
