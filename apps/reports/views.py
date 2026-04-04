"""
Report views:
  POST /reports/dashboard/<dashboard_id>/generate/  → kicks off async job, returns JSON
  GET  /reports/job/<job_id>/status/                → poll job status
  GET  /reports/job/<job_id>/download/              → download the PDF (auth required)
  GET  /reports/share/<share_token>/                → public share link (no auth needed)
"""
import logging
import mimetypes
import os

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import (
    FileResponse, Http404, HttpResponse, JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.dashboards.models import Dashboard
from apps.reports.models import ReportJob
from apps.reports.tasks import generate_pdf_report

logger = logging.getLogger(__name__)

MEDIA_ROOT = getattr(settings, 'MEDIA_ROOT', '/tmp')


def _get_workspace(request):
    """Return the current workspace from the request (set by CurrentWorkspaceMiddleware)."""
    return getattr(request.user, 'current_workspace', None)


class GenerateReportView(LoginRequiredMixin, View):
    """
    POST /reports/dashboard/<dashboard_id>/generate/
    Creates a ReportJob and enqueues the Celery task.
    Returns JSON: { job_id, status_url, share_token }
    """
    def post(self, request, dashboard_id):
        workspace = _get_workspace(request)
        if not workspace:
            return JsonResponse({'error': 'No active workspace.'}, status=400)

        dashboard = get_object_or_404(
            Dashboard, pk=dashboard_id, workspace=workspace, is_active=True
        )

        job = ReportJob.objects.create(
            workspace=workspace,
            dashboard_id=dashboard.pk,
            requested_by=request.user,
        )
        generate_pdf_report.delay(str(job.id))

        return JsonResponse({
            'job_id':     str(job.id),
            'status':     job.status,
            'status_url': f'/reports/job/{job.id}/status/',
        }, status=202)


class ReportJobStatusView(LoginRequiredMixin, View):
    """
    GET /reports/job/<job_id>/status/
    Returns JSON with current job status.
    """
    def get(self, request, job_id):
        workspace = _get_workspace(request)
        job = get_object_or_404(ReportJob, pk=job_id, workspace=workspace)
        payload = {
            'job_id':       str(job.id),
            'status':       job.status,
            'created_at':   job.created_at.isoformat(),
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
            'error':        job.error or None,
            'share_token':  str(job.share_token) if job.status == ReportJob.STATUS_DONE else None,
            'download_url': f'/reports/job/{job.id}/download/' if job.status == ReportJob.STATUS_DONE else None,
            'share_url':    f'/reports/share/{job.share_token}/' if job.status == ReportJob.STATUS_DONE else None,
        }
        return JsonResponse(payload)


class DownloadReportView(LoginRequiredMixin, View):
    """
    GET /reports/job/<job_id>/download/
    Streams the PDF to the authenticated user.
    """
    def get(self, request, job_id):
        workspace = _get_workspace(request)
        job = get_object_or_404(
            ReportJob, pk=job_id, workspace=workspace, status=ReportJob.STATUS_DONE
        )
        return _stream_pdf(job, download=True)


class ShareReportView(View):
    """
    GET /reports/share/<share_token>/
    Public endpoint — no login required.
    Streams the PDF inline (for preview in browser tab).
    """
    def get(self, request, share_token):
        job = get_object_or_404(ReportJob, share_token=share_token, status=ReportJob.STATUS_DONE)
        return _stream_pdf(job, download=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _stream_pdf(job: ReportJob, download: bool) -> HttpResponse:
    abs_path = os.path.join(MEDIA_ROOT, job.pdf_path)
    if not os.path.exists(abs_path):
        raise Http404('Report file not found.')

    filename = f'report-{job.dashboard_id}.pdf'
    disposition = 'attachment' if download else 'inline'
    response = FileResponse(
        open(abs_path, 'rb'),
        content_type='application/pdf',
    )
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return response
