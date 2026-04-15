"""
apps/core/middleware.py
=======================
Lightweight Prometheus HTTP instrumentation middleware for Django 6.

Records:
  http_requests_total{method, path_template, status}
  http_request_duration_seconds{method, path_template}

Uses URL resolver to convert concrete paths (/api/v1/workspaces/abc-123/) to
templates (/api/v1/workspaces/<uuid:workspace_id>/) so high-cardinality UUIDs
do not explode the metric cardinality.
"""

import time
import logging

logger = logging.getLogger(__name__)


def _path_template(request) -> str:
    """
    Return the matched URL pattern string, falling back to the raw path.
    High-cardinality path parameters (UUIDs, IDs) are replaced by their
    converter type so e.g. /api/v1/workspaces/abc-123/ becomes
    /api/v1/workspaces/<uuid:workspace_id>/.
    """
    try:
        from django.urls import resolve, Resolver404
        match = resolve(request.path_info)
        # The route attribute on the match is the pattern string
        route = getattr(match, 'route', None)
        if route:
            return f'/{route}'
    except Exception:
        pass
    return request.path_info


class PrometheusMiddleware:
    """
    WSGI/ASGI middleware that records HTTP request counts and latency.

    Safely no-ops when prometheus_client is not installed so tests are unaffected.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._enabled = False
        try:
            from apps.core.metrics import (
                HTTP_REQUESTS_TOTAL,
                HTTP_REQUEST_DURATION_SECONDS,
            )
            self._requests_total   = HTTP_REQUESTS_TOTAL
            self._request_duration = HTTP_REQUEST_DURATION_SECONDS
            self._enabled = True
        except Exception:
            logger.debug('PrometheusMiddleware: metrics unavailable, disabled.')

    def __call__(self, request):
        if not self._enabled:
            return self.get_response(request)

        start    = time.monotonic()
        response = self.get_response(request)
        elapsed  = time.monotonic() - start
        method   = request.method
        path     = _path_template(request)
        status   = str(response.status_code)

        self._requests_total.labels(
            method=method, path_template=path, status=status
        ).inc()
        self._request_duration.labels(
            method=method, path_template=path
        ).observe(elapsed)

        return response
