"""
Celery tasks for async PDF report generation and scheduled delivery.
"""
import logging
import os
import time

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
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
                if widget.table:
                    engine = QueryEngine(widget.table)
                    data = engine.execute_widget_query(widget)
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


@shared_task(name='apps.reports.tasks.process_report_schedules')
def process_report_schedules() -> dict:
    """
    Evaluate all active ``ReportSchedule`` records and fire delivery for
    any whose ``next_send_at`` has elapsed.

    This task is run by Celery Beat on a frequent cadence (every 15 min)
    so the maximum schedule latency is 15 minutes.

    For each due schedule the task:
      1. Creates a ``ReportJob``
      2. Calls ``deliver_scheduled_report.delay()`` asynchronously
      3. Advances ``next_send_at`` and sets ``last_sent_at``
    """
    from apps.reports.models import ReportJob, ReportSchedule

    now       = timezone.now()
    schedules = ReportSchedule.objects.filter(is_active=True, next_send_at__lte=now)
    triggered = 0
    skipped   = 0

    for schedule in schedules:
        try:
            job = ReportJob.objects.create(
                workspace=schedule.workspace,
                dashboard_id=schedule.dashboard_id,
                requested_by=schedule.created_by,
            )
            deliver_scheduled_report.delay(
                str(job.id),
                str(schedule.id),
                schedule.recipients,
                schedule.report_format,
            )
            # Advance schedule
            schedule.last_sent_at = now
            schedule.next_send_at = schedule.compute_next_send(after=now)
            schedule.save(update_fields=['last_sent_at', 'next_send_at'])
            triggered += 1
            logger.info('Schedule %s triggered job %s', schedule.id, job.id)
        except Exception as exc:
            logger.exception('Failed to trigger schedule %s: %s', schedule.id, exc)
            skipped += 1

    return {'triggered': triggered, 'skipped': skipped}


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name='apps.reports.tasks.deliver_scheduled_report',
)
def deliver_scheduled_report(self, job_id: str, schedule_id: str, recipients: list, report_format: str):
    """
    Wait for a ``ReportJob`` to complete, then email the result to recipients.

    Retries up to 3 times with 60 s delay if the job is still running.
    """
    from apps.reports.models import ReportJob

    try:
        job = ReportJob.objects.get(pk=job_id)
    except ReportJob.DoesNotExist:
        logger.error('deliver_scheduled_report: job %s not found', job_id)
        return

    # If the PDF job has not been triggered yet, trigger it now
    if job.status == ReportJob.STATUS_PENDING:
        generate_pdf_report.delay(job_id)

    # Poll until done or failed (max ~5 minutes via retries)
    job.refresh_from_db()
    if job.status in (ReportJob.STATUS_PENDING, ReportJob.STATUS_RUNNING):
        raise self.retry(countdown=60)

    if job.status == ReportJob.STATUS_FAILED:
        logger.error('Scheduled report job %s failed; skipping delivery', job_id)
        return

    if not recipients:
        logger.warning('Schedule %s has no recipients; skipping delivery', schedule_id)
        return

    # Attach and send
    subject   = f'Scheduled Report — {timezone.now().strftime("%Y-%m-%d")}'
    body      = (
        'Your scheduled dashboard report is attached.\n\n'
        'This report was generated automatically by AnalyticsMeta.'
    )
    pdf_abs   = os.path.join(getattr(settings, 'MEDIA_ROOT', '/tmp'), job.pdf_path) if job.pdf_path else None

    msg = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com'),
        to=recipients,
    )

    if pdf_abs and os.path.exists(pdf_abs):
        with open(pdf_abs, 'rb') as fh:
            filename = f'report_{timezone.now().strftime("%Y%m%d")}.pdf'
            msg.attach(filename, fh.read(), 'application/pdf')
    else:
        logger.warning('PDF file not found for job %s — sending notification only', job_id)

    msg.send(fail_silently=False)
    logger.info('Scheduled report for schedule %s delivered to %d recipients', schedule_id, len(recipients))


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
