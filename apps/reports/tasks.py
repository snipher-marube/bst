"""
Celery tasks for async PDF report generation.
"""
import logging
import os

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(
    getattr(settings, 'MEDIA_ROOT', '/tmp'),
    'reports',
)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    name='apps.reports.tasks.generate_pdf_report',
)
def generate_pdf_report(self, report_job_id: str) -> str:
    """
    Generate a PDF for a ReportJob and persist the file.
    Returns the path to the generated PDF.
    """
    from apps.reports.models import ReportJob
    from apps.dashboards.models import Dashboard, Widget
    from apps.insights.models import Insight
    from apps.dashboards.services import QueryEngine
    from apps.reports.generators import build_dashboard_pdf

    job = ReportJob.objects.select_related('workspace', 'requested_by').get(pk=report_job_id)
    job.status = ReportJob.STATUS_RUNNING
    job.save(update_fields=['status'])

    try:
        dashboard = Dashboard.objects.get(pk=job.dashboard_id, workspace=job.workspace)
        widgets   = list(
            Widget.objects.filter(dashboard=dashboard).select_related('table').order_by('id')
        )
        insights  = list(
            Insight.objects.filter(workspace=job.workspace).order_by('-created_at')[:30]
        )

        # Fetch live query results for every widget
        widgets_with_data = []
        for widget in widgets:
            try:
                engine = QueryEngine(widget.table) if widget.table else None
                if engine:
                    data = engine.execute_widget_query(widget.query_config or {})
                else:
                    data = {}
            except Exception as exc:
                logger.warning('Widget %s query failed: %s', widget.id, exc)
                data = {}
            widgets_with_data.append((widget, data))

        pdf_bytes = build_dashboard_pdf(
            dashboard=dashboard,
            workspace=job.workspace,
            insights=insights,
            widgets_with_data=widgets_with_data,
            requested_by=job.requested_by,
        )

        # Persist to disk
        os.makedirs(REPORT_DIR, exist_ok=True)
        ws_dir = os.path.join(REPORT_DIR, str(job.workspace_id))
        os.makedirs(ws_dir, exist_ok=True)
        filename = f'{job.id}.pdf'
        abs_path = os.path.join(ws_dir, filename)

        with open(abs_path, 'wb') as fh:
            fh.write(pdf_bytes)

        rel_path = os.path.join('reports', str(job.workspace_id), filename)
        job.status       = ReportJob.STATUS_DONE
        job.pdf_path     = rel_path
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'pdf_path', 'completed_at'])
        logger.info('Report %s generated: %s', job.id, rel_path)
        return rel_path

    except Exception as exc:
        logger.exception('Report %s failed: %s', report_job_id, exc)
        job.status = ReportJob.STATUS_FAILED
        job.error  = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error', 'completed_at'])
        raise


@shared_task(name='apps.reports.tasks.cleanup_old_reports')
def cleanup_old_reports(days: int = 30) -> int:
    """Delete report files and job records older than `days` days."""
    from apps.reports.models import ReportJob
    cutoff = timezone.now() - timezone.timedelta(days=days)
    old_jobs = ReportJob.objects.filter(created_at__lt=cutoff, status=ReportJob.STATUS_DONE)
    deleted = 0
    for job in old_jobs:
        if job.pdf_path:
            abs_path = os.path.join(
                getattr(settings, 'MEDIA_ROOT', '/tmp'), job.pdf_path
            )
            try:
                os.remove(abs_path)
            except FileNotFoundError:
                pass
        job.delete()
        deleted += 1
    logger.info('Cleaned up %d old report jobs.', deleted)
    return deleted
