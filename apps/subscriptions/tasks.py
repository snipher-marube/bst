"""
apps/subscriptions/tasks.py
============================
Celery tasks for the billing / subscription system.

Tasks
-----
send_dunning_email_task
    One-shot task that sends a single dunning email at the given stage.
    Fired immediately (via .delay()) from the Stripe webhook handler when a
    payment first fails, so the user gets the day-0 notice within seconds.

process_grace_periods
    Periodic task (runs daily at 08:00 UTC via Celery Beat).  Advances the
    dunning sequence for every ``past_due`` subscription:

    * Stage 0 → 1  when ≥ 3 days have elapsed since grace started (day-3 email)
    * Stage 1 → 2  when ≤ 1 day remains in the grace window (day-7 final warning)
    * Stage 2 → 3  when grace_period_ends_at has passed → downgrade to free plan
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

# Module-level import so tests can patch apps.subscriptions.tasks.send_dunning_email
from apps.subscriptions.emails import send_dunning_email  # noqa: E402

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300, name='subscriptions.send_dunning_email')
def send_dunning_email_task(self, subscription_id: str, stage: int) -> None:
    """
    Send a single dunning email for *subscription_id* at *stage*.

    Retried up to 3 times (5-minute intervals) on transient failures such as
    SMTP timeouts so that day-0 notices are reliably delivered even if the
    mail server is briefly unavailable.
    """
    from apps.subscriptions.models import Subscription
    from apps.subscriptions.emails import send_dunning_email

    try:
        sub = Subscription.objects.select_related('workspace', 'workspace__owner', 'plan').get(
            id=subscription_id
        )
    except Subscription.DoesNotExist:
        logger.warning("send_dunning_email_task: subscription %s not found", subscription_id)
        return

    try:
        send_dunning_email(sub, stage)
    except Exception as exc:
        logger.exception(
            "Dunning email failed: subscription=%s stage=%d — retrying",
            subscription_id, stage,
        )
        raise self.retry(exc=exc)


@shared_task(bind=True, name='subscriptions.process_grace_periods')
def process_grace_periods(self) -> dict:
    """
    Daily task: advance the dunning sequence and downgrade expired workspaces.

    Returns a summary dict for logging / monitoring.
    """
    from apps.subscriptions.models import Subscription, Plan, PLAN_LIMITS
    from apps.subscriptions.emails import send_dunning_email

    now = timezone.now()

    from apps.subscriptions.models import Subscription

    past_due_qs = Subscription.objects.filter(
        status='past_due',
        grace_period_ends_at__isnull=False,
    ).select_related('workspace', 'workspace__owner', 'plan')

    stats = {'day3_emails': 0, 'day7_emails': 0, 'downgrades': 0, 'errors': 0}

    for sub in past_due_qs:
        try:
            _advance_dunning(sub, now, stats)
        except Exception:
            logger.exception("process_grace_periods: error processing subscription %s", sub.id)
            stats['errors'] += 1

    logger.info("process_grace_periods complete: %s", stats)
    return stats


def _advance_dunning(sub, now, stats: dict) -> None:
    """Advance a single subscription through the dunning sequence."""
    from apps.subscriptions.models import Subscription

    grace_ends     = sub.grace_period_ends_at
    days_remaining = (grace_ends - now).days

    # ── Grace expired → downgrade ─────────────────────────────────────────
    if now >= grace_ends:
        _downgrade_to_free(sub, now)
        stats['downgrades'] += 1
        return

    # ── Day-7 final warning (≤ 1 day left, stage < DUNNING_DAY7) ──────────
    if days_remaining <= 1 and sub.dunning_stage < Subscription.DUNNING_DAY7:
        try:
            send_dunning_email(sub, stage=2)
        except Exception:
            logger.exception("Day-7 dunning email failed for subscription %s", sub.id)
        sub.dunning_stage = Subscription.DUNNING_DAY7
        sub.save(update_fields=['dunning_stage'])
        stats['day7_emails'] += 1
        return

    # ── Day-3 reminder (≤ 4 days left, stage < DUNNING_DAY3) ─────────────
    if days_remaining <= 4 and sub.dunning_stage < Subscription.DUNNING_DAY3:
        try:
            send_dunning_email(sub, stage=1)
        except Exception:
            logger.exception("Day-3 dunning email failed for subscription %s", sub.id)
        sub.dunning_stage = Subscription.DUNNING_DAY3
        sub.save(update_fields=['dunning_stage'])
        stats['day3_emails'] += 1


@transaction.atomic
def _downgrade_to_free(sub, now) -> None:
    """Downgrade a workspace to the free plan after the grace period expires."""
    from apps.subscriptions.models import Plan, PLAN_LIMITS

    free_plan, _ = Plan.objects.get_or_create(
        tier='free',
        defaults={'name': 'Free', **PLAN_LIMITS['free']},
    )
    sub.plan              = free_plan
    sub.status            = 'cancelled'
    sub.grace_period_ends_at = None
    sub.dunning_stage     = sub.DUNNING_EXPIRED
    sub.cancelled_at      = now
    sub.save(update_fields=['plan', 'status', 'grace_period_ends_at', 'dunning_stage', 'cancelled_at'])
    sub.apply_plan_limits()

    logger.info(
        "Workspace %s downgraded to free plan after grace period expiry",
        sub.workspace.name,
    )
