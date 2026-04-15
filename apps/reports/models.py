import uuid
import logging
from django.db import models
from django.contrib.auth import get_user_model
from apps.workspaces.models import Workspace

User = get_user_model()
logger = logging.getLogger(__name__)


class ReportJob(models.Model):
    """
    Tracks async PDF report generation jobs.
    """
    STATUS_PENDING   = 'pending'
    STATUS_RUNNING   = 'running'
    STATUS_DONE      = 'done'
    STATUS_FAILED    = 'failed'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_DONE,    'Done'),
        (STATUS_FAILED,  'Failed'),
    ]

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace    = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='report_jobs')
    dashboard_id = models.UUIDField()              # FK-like, avoids circular import
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error        = models.TextField(blank=True)
    # Relative path inside MEDIA_ROOT, e.g. reports/<workspace>/<job>.pdf
    pdf_path     = models.CharField(max_length=500, blank=True)
    share_token  = models.UUIDField(default=uuid.uuid4, unique=True)

    created_at   = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['workspace', 'dashboard_id', 'status']),
            models.Index(fields=['share_token']),
        ]

    def __str__(self):
        return f"ReportJob {self.id} [{self.status}]"


class ReportSchedule(models.Model):
    """
    Defines a recurring report delivery schedule.

    A schedule binds a dashboard to a set of recipient email addresses
    and fires at a cron-style interval.  On each firing the system
    creates a ``ReportJob``, waits for it to complete, then emails the
    PDF to all recipients.

    cron_expression examples:
        '0 8 * * 1'    — every Monday at 08:00 UTC
        '0 6 * * *'    — every day at 06:00 UTC
        '0 9 1 * *'    — first day of every month at 09:00 UTC
    """
    FORMAT_PDF  = 'pdf'
    FORMAT_CSV  = 'csv'
    FORMAT_CHOICES = [
        (FORMAT_PDF, 'PDF'),
        (FORMAT_CSV, 'CSV'),
    ]

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace       = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='report_schedules')
    dashboard_id    = models.UUIDField()          # mirrors ReportJob pattern
    name            = models.CharField(max_length=200)
    cron_expression = models.CharField(max_length=100, default='0 8 * * 1',
                                       help_text='Cron expression (UTC). E.g. "0 8 * * 1" = Mon 08:00.')
    recipients      = models.JSONField(default=list,
                                       help_text='List of email addresses to deliver the report to.')
    report_format   = models.CharField(max_length=10, choices=FORMAT_CHOICES, default=FORMAT_PDF)
    is_active       = models.BooleanField(default=True)
    created_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_schedules')

    last_sent_at    = models.DateTimeField(null=True, blank=True)
    next_send_at    = models.DateTimeField(null=True, blank=True,
                                           help_text='Pre-computed next fire time (UTC). Updated after each send.')

    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['workspace', 'name']
        indexes  = [
            models.Index(fields=['workspace', 'is_active', 'next_send_at']),
        ]

    def __str__(self):
        return f"ReportSchedule '{self.name}' [{self.cron_expression}]"

    def compute_next_send(self, after=None):
        """Return the next UTC datetime this schedule should fire.

        Uses ``croniter`` if installed (listed in requirements).  Falls
        back to None (which disables the schedule) if the library is
        missing or the expression is malformed.
        """
        from django.utils import timezone as tz
        import datetime as _dt
        try:
            from croniter import croniter
        except ImportError:
            logger.warning('croniter not installed — ReportSchedule.compute_next_send unavailable')
            return None
        try:
            base = after or tz.now()
            # croniter works with naive datetimes; normalise to UTC-naive
            if base.tzinfo is not None:
                base_naive = base.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            else:
                base_naive = base
            cron  = croniter(self.cron_expression, base_naive)
            naive = cron.get_next(_dt.datetime)
            # Attach UTC tzinfo without conversion
            return naive.replace(tzinfo=_dt.timezone.utc)
        except Exception as exc:
            logger.warning('Invalid cron expression %r: %s', self.cron_expression, exc)
            return None
