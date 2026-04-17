import logging
import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.db.models import JSONField
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)
User = get_user_model()


class Notification(models.Model):
    """
    In-app notification for a user, scoped to a workspace.
    """
    NOTIF_TYPES = [
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('invite', 'Team Invite'),
        ('import', 'Import Complete'),
        ('insight', 'New Insight'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, null=True, blank=True)

    notif_type = models.CharField(max_length=20, choices=NOTIF_TYPES, default='info')
    title = models.CharField(max_length=200)
    message = models.TextField()
    action_url = models.CharField(max_length=500, blank=True)

    # Extra data (e.g. import_job_id, insight_id)
    metadata = JSONField(default=dict, blank=True)

    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read']),
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f"[{self.notif_type}] {self.title} → {self.user.email}"

    @classmethod
    def notify(cls, user, title, message, notif_type='info', workspace=None,
               action_url='', metadata=None, send_email=True):
        """
        Create an in-app notification and optionally send an email.

        Parameters
        ----------
        send_email : bool
            When True (default), also sends an email if the user has opted in
            for this notification type via their NotificationPreference.
        """
        notification = cls.objects.create(
            user=user,
            workspace=workspace,
            notif_type=notif_type,
            title=title,
            message=message,
            action_url=action_url,
            metadata=metadata or {},
        )

        if send_email:
            try:
                from apps.notifications.emails import send_notification_email
                send_notification_email(notification)
            except Exception as exc:
                # Email failure must never break the in-app notification path.
                logger.warning("Email send failed for notification %s: %s", notification.id, exc)

        return notification


class NotificationPreference(models.Model):
    """
    Per-user notification opt-in preferences for email and WhatsApp channels.

    Created lazily via get_or_create — all fields default to sensible values
    so existing users without a row still behave correctly.
    """
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='notification_prefs'
    )

    # Email opt-ins
    email_invites  = models.BooleanField(default=True,  help_text="Team invitation emails")
    email_imports  = models.BooleanField(default=True,  help_text="Import job completion emails")
    email_insights = models.BooleanField(default=False, help_text="New AI insight emails (can be frequent)")
    email_system   = models.BooleanField(default=True,  help_text="Important system / account emails")

    # WhatsApp channel
    whatsapp_number  = models.CharField(
        max_length=20, blank=True,
        help_text="E.164 format, e.g. +254712345678. Leave blank to disable WhatsApp alerts.",
    )
    whatsapp_alerts  = models.BooleanField(default=False, help_text="Send data-alert notifications via WhatsApp")

    def __str__(self):
        return f"NotifPrefs({self.user.email})"

    def allows_email(self, notif_type: str) -> bool:
        """Return True if the user wants email for the given notification type."""
        mapping = {
            'invite':  self.email_invites,
            'import':  self.email_imports,
            'insight': self.email_insights,
            'info':    self.email_system,
            'success': self.email_system,
            'warning': self.email_system,
            'error':   self.email_system,
        }
        return mapping.get(notif_type, True)

    def wants_whatsapp_alerts(self) -> bool:
        return bool(self.whatsapp_alerts and self.whatsapp_number)
