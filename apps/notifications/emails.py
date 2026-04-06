"""
apps/notifications/emails.py
=============================
Handles rendering and sending HTML notification emails.

Called by Notification.notify() — failures are caught and logged so they
never break the in-app notification path.
"""

import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

SITE_URL = getattr(settings, 'SITE_URL', 'http://localhost:8000')
FROM_EMAIL = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@analyticsmeta.com')

# Map notif_type → template name
TYPE_TEMPLATES = {
    'invite':  'notifications/email/invite.html',
    'import':  'notifications/email/import.html',
    'insight': 'notifications/email/insight.html',
}
GENERIC_TEMPLATE = 'notifications/email/generic.html'

SYSTEM_ICONS = {
    'success': ('#10b981', '✅'),
    'warning': ('#f59e0b', '⚠️'),
    'error':   ('#ef4444', '❌'),
    'info':    ('#03466e', 'ℹ️'),
}


def send_notification_email(notification):
    """
    Send an HTML email for *notification* if the user has opted in.

    Checks NotificationPreference — skips silently if the user has turned
    off emails for this notification type.
    """
    from apps.notifications.models import NotificationPreference

    prefs, _ = NotificationPreference.objects.get_or_create(user=notification.user)

    if not prefs.allows_email(notification.notif_type):
        logger.debug(
            "Email suppressed for %s (type=%s, user opted out)",
            notification.id, notification.notif_type,
        )
        return

    template = TYPE_TEMPLATES.get(notification.notif_type, GENERIC_TEMPLATE)

    icon_bg, icon_emoji = SYSTEM_ICONS.get(notification.notif_type, ('#03466e', 'ℹ️'))

    context = {
        'notification': notification,
        'user': notification.user,
        'site_url': SITE_URL,
        'action_url': (
            f"{SITE_URL}{notification.action_url}"
            if notification.action_url and not notification.action_url.startswith('http')
            else notification.action_url
        ),
        'unsubscribe_url': f"{SITE_URL}/dashboard/profile/",
        'icon_bg': icon_bg,
        'icon_emoji': icon_emoji,
    }

    html_body = render_to_string(template, context)
    plain_body = strip_tags(html_body)

    subject = notification.title

    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain_body,
        from_email=f"AnalyticsMeta <{FROM_EMAIL}>",
        to=[notification.user.email],
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send(fail_silently=False)  # caller wraps in try/except

    logger.info(
        "Notification email sent: type=%s to=%s subject=%r",
        notification.notif_type, notification.user.email, subject,
    )
