import os

settings_module = os.getenv("DJANGO_SETTINGS_MODULE")

if settings_module == "config.settings.production":
    from .production import *
elif settings_module == "config.settings.testing":
    from .testing import *
else:
    from .development import *