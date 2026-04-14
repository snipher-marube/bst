"""
apps/subscriptions/emails.py
=============================
Dunning email helpers for the billing / grace-period flow.

These are direct transactional emails (not routed through the Notification
model) because billing emails are critical and must always be delivered,
regardless of a user's in-app notification preferences.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

SITE_URL    = getattr(settings, 'SITE_URL', 'http://localhost:8000')
FROM_EMAIL  = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@analyticsmeta.com')
BILLING_URL = '/subscriptions/billing/'

STAGE_LABELS = {
    0: 'Failed',
    1: 'Reminder',
    2: 'Final Warning',
}

STAGE_SUBJECTS = {
    0: 'Action required: your payment failed',
    1: 'Reminder: payment still outstanding for {workspace}',
    2: 'Final notice: your workspace will be downgraded tomorrow',
}


def send_dunning_email(subscription, stage: int) -> None:
    """
    Send a dunning email to the workspace owner for *subscription* at *stage*.

    Parameters
    ----------
    subscription : Subscription
        The past_due Subscription whose owner should receive the email.
    stage : int
        0 = day-0 initial failure notice
        1 = day-3 reminder
        2 = day-7 final warning
    """
    owner = subscription.workspace.owner
    if not owner or not owner.email:
        logger.warning(
            "Dunning stage %d skipped for subscription %s — no owner email",
            stage, subscription.id,
        )
        return

    plan_name      = subscription.plan.name if subscription.plan else 'Paid'
    workspace_name = subscription.workspace.name
    grace_ends     = subscription.grace_period_ends_at or timezone.now()
    days_remaining = max(0, (grace_ends - timezone.now()).days)

    context = {
        'user':              owner,
        'subscription':      subscription,
        'workspace_name':    workspace_name,
        'plan_name':         plan_name,
        'stage':             stage,
        'stage_label':       STAGE_LABELS.get(stage, 'Notice'),
        'grace_ends_display': grace_ends.strftime('%B %d, %Y'),
        'days_remaining':    days_remaining,
        'action_url':        f"{SITE_URL}{BILLING_URL}",
        'unsubscribe_url':   f"{SITE_URL}/dashboard/profile/",
        'site_url':          SITE_URL,
    }

    html_body  = render_to_string('notifications/email/billing_dunning.html', context)
    plain_body = strip_tags(html_body)
    subject    = STAGE_SUBJECTS.get(stage, 'Payment notice').format(workspace=workspace_name)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain_body,
        from_email=f"AnalyticsMeta Billing <{FROM_EMAIL}>",
        to=[owner.email],
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send(fail_silently=False)

    logger.info(
        "Dunning email sent: stage=%d subscription=%s to=%s",
        stage, subscription.id, owner.email,
    )
