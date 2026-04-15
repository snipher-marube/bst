from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'apps.core'

    def ready(self):
        # Register Celery → Prometheus signal handlers and custom metrics.
        # Import as a side-effect — no public API needed.
        from apps.core import metrics  # noqa: F401
