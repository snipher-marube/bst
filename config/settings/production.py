from .base import *
from decouple import config

DEBUG = False

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='analyticsmeta.com').split(',')

CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS]

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────
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
        "CONN_HEALTH_CHECKS": True,   # drop stale pooled connections automatically
        "OPTIONS": {
            "connect_timeout": 10,
            "sslmode": "require",     # enforce TLS to Render Postgres
        },
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Redis / Cache / Channel Layers — Upstash TLS-aware overrides
# ─────────────────────────────────────────────────────────────────────────────
_REDIS_URL = config('REDIS_URL')  # e.g. rediss://:password@host:6380

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': _REDIS_URL,
        'KEY_PREFIX': 'analyticsmeta',
        'TIMEOUT': 300,
    },
    'sessions': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': _REDIS_URL,
        'KEY_PREFIX': 'sessions',
        'TIMEOUT': SESSION_COOKIE_AGE,
    },
}

CELERY_BROKER_URL = _REDIS_URL
CELERY_RESULT_BACKEND = _REDIS_URL

# channels-redis 4.x accepts a URL string in hosts — required for rediss:// TLS
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [_REDIS_URL],
            "capacity": 1500,
            "expiry": 10,
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTPS / Security headers
# ─────────────────────────────────────────────────────────────────────────────
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# ─────────────────────────────────────────────────────────────────────────────
# Domain
# ─────────────────────────────────────────────────────────────────────────────
DOMAIN = config('SITE_URL', default='https://analyticsmeta.com')
SITE_URL = DOMAIN

# ─────────────────────────────────────────────────────────────────────────────
# Admin error notifications
# ─────────────────────────────────────────────────────────────────────────────
ADMINS = [
    (config('ADMIN_NAME', default='Admin'), config('ADMIN_EMAIL', default='')),
]
SERVER_EMAIL = config('EMAIL_HOST_USER', default='')

# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
DEFAULT_FROM_EMAIL = config('EMAIL_HOST_USER', default='noreply@analyticsmeta.com')

# ─────────────────────────────────────────────────────────────────────────────
# Static / media — Cloudinary CDN
# ─────────────────────────────────────────────────────────────────────────────
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': config('CLOUDINARY_CLOUD_NAME'),
    'API_KEY':    config('CLOUDINARY_API_KEY'),
    'API_SECRET': config('CLOUDINARY_API_SECRET'),
    'STATIC_TAG': 'analyticsmeta_static',
    'STATICFILES_MANIFEST_ROOT': BASE_DIR / '../staticfiles',
}

STATICFILES_STORAGE = 'cloudinary_storage.storage.StaticHashedCloudinaryStorage'
DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'

STATIC_URL  = f"https://res.cloudinary.com/{config('CLOUDINARY_CLOUD_NAME')}/raw/upload/analyticsmeta_static/"
MEDIA_URL   = f"https://res.cloudinary.com/{config('CLOUDINARY_CLOUD_NAME')}/raw/upload/"
STATIC_ROOT = BASE_DIR / '../staticfiles'
MEDIA_ROOT  = BASE_DIR / '../static/media'

# ─────────────────────────────────────────────────────────────────────────────
# M-Pesa Daraja — all required; no safe defaults in production
# ─────────────────────────────────────────────────────────────────────────────
MPESA_SANDBOX = False
MPESA_CONSUMER_KEY    = config('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = config('MPESA_CONSUMER_SECRET')
MPESA_SHORTCODE       = config('MPESA_SHORTCODE')
MPESA_PASSKEY         = config('MPESA_PASSKEY')
# Must be publicly reachable HTTPS:
# e.g. https://analyticsmeta.com/subscriptions/mpesa/callback/
MPESA_CALLBACK_URL = config('MPESA_CALLBACK_URL')

# ─────────────────────────────────────────────────────────────────────────────
# Logging — JSON output + email critical errors to ADMINS
# ─────────────────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler',
            'include_html': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'mail_admins'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
