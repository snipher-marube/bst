from .base import *
from decouple import config
# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []

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

MEDIA_URL = '/media/'
STATICFILES_DIRS = [BASE_DIR / '../static']

MEDIA_ROOT = BASE_DIR / '../static/media'
STATIC_ROOT = BASE_DIR / '../staticfiles'