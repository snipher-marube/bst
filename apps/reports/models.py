import uuid
from django.db import models
from django.contrib.auth import get_user_model
from apps.workspaces.models import Workspace

User = get_user_model()


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
