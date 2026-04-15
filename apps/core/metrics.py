"""
apps/core/metrics.py
====================
Custom Prometheus metrics for AnalyticsMeta.

Exposes Celery task counters that complement the built-in django-prometheus
HTTP, DB, and cache metrics.

Usage
-----
Import this module once at app startup (AppConfig.ready):

    from apps.core import metrics  # noqa: F401 — side-effect import

The Celery signal handlers register automatically on import.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy metric initialisation — prometheus_client may not be installed in
# test environments that mock everything.  We guard so tests still pass.
# ---------------------------------------------------------------------------
try:
    from prometheus_client import Counter, Histogram, Gauge

    # ── HTTP request metrics (used by PrometheusMiddleware) ───────────────
    HTTP_REQUESTS_TOTAL = Counter(
        'http_requests_total',
        'Total HTTP requests by method, path template, and status code.',
        ['method', 'path_template', 'status'],
    )

    HTTP_REQUEST_DURATION_SECONDS = Histogram(
        'http_request_duration_seconds',
        'HTTP request latency in seconds.',
        ['method', 'path_template'],
        buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, float('inf')],
    )

    # ── Celery task metrics ────────────────────────────────────────────────
    CELERY_TASKS_TOTAL = Counter(
        'celery_tasks_total',
        'Total number of Celery tasks dispatched, by name and state.',
        ['task_name', 'state'],
    )

    CELERY_TASK_DURATION_SECONDS = Histogram(
        'celery_task_duration_seconds',
        'Celery task execution duration in seconds.',
        ['task_name'],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float('inf')],
    )

    # ── Business metrics ───────────────────────────────────────────────────
    IMPORT_JOBS_TOTAL = Counter(
        'analyticsmeta_import_jobs_total',
        'Total CSV/webhook import jobs, by status.',
        ['status'],
    )

    INSIGHTS_GENERATED_TOTAL = Counter(
        'analyticsmeta_insights_generated_total',
        'Insight records created by type.',
        ['insight_type'],
    )

    ANOMALIES_DETECTED_TOTAL = Counter(
        'analyticsmeta_anomalies_detected_total',
        'Anomaly insights found during daily analysis.',
    )

    _METRICS_AVAILABLE = True

except ImportError:
    _METRICS_AVAILABLE = False
    logger.warning('prometheus_client not available — metrics disabled')


# ---------------------------------------------------------------------------
# Celery signal wiring
# ---------------------------------------------------------------------------

def _connect_celery_signals():
    """Connect Celery task lifecycle signals to Prometheus counters."""
    if not _METRICS_AVAILABLE:
        return

    import time

    try:
        from celery.signals import (
            task_prerun, task_success, task_failure, task_retry,
        )

        _task_start_times: dict = {}

        @task_prerun.connect
        def on_task_prerun(task_id, task, **kwargs):
            _task_start_times[task_id] = time.monotonic()
            CELERY_TASKS_TOTAL.labels(task_name=task.name, state='started').inc()

        @task_success.connect
        def on_task_success(sender, **kwargs):
            task_id = kwargs.get('task_id') or getattr(sender.request, 'id', None)
            start   = _task_start_times.pop(task_id, None)
            if start is not None:
                CELERY_TASK_DURATION_SECONDS.labels(task_name=sender.name).observe(
                    time.monotonic() - start
                )
            CELERY_TASKS_TOTAL.labels(task_name=sender.name, state='success').inc()

        @task_failure.connect
        def on_task_failure(sender, task_id, **kwargs):
            _task_start_times.pop(task_id, None)
            CELERY_TASKS_TOTAL.labels(task_name=sender.name, state='failure').inc()

        @task_retry.connect
        def on_task_retry(sender, **kwargs):
            CELERY_TASKS_TOTAL.labels(task_name=sender.name, state='retry').inc()

        logger.debug('Celery → Prometheus signal handlers connected.')

    except Exception:
        logger.exception('Failed to connect Celery Prometheus signals.')


_connect_celery_signals()
