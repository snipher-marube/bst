from .base import *
from decouple import config

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='analyticsmeta.com').split(',')

CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS]


# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("PG_DATABASE_NAME"),
        "USER": config("PG_DATABASE_USER"),
        "PASSWORD": config("PG_DATABASE_PASSWORD"),
        "HOST": config("PG_DATABASE_HOST"),
        "PORT": config("PG_DATABASE_PORT"),
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": 600,
    }
}

# Security settings
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Domain settings
DOMAIN = config('SITE_URL', default='https://analyticsmeta.com')
SITE_URL = DOMAIN

# Stripe
STRIPE_PUBLISHABLE_KEY = config('STRIPE_PUBLISHABLE_KEY', default='')
STRIPE_SECRET_KEY = config('STRIPE_SECRET_KEY', default='')
STRIPE_WEBHOOK_SECRET = config('STRIPE_WEBHOOK_SECRET', default='')

# Static / media
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
MEDIA_URL = '/media/'
STATIC_ROOT = BASE_DIR / '../staticfiles'
MEDIA_ROOT = BASE_DIR / '../static/media'

# Email use real SMTP in production (override from base.py which uses console in dev)
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

