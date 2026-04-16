"""
apps/dashboards/tasks.py
========================
Celery beat tasks for the dashboards app.

Tasks
-----
prune_audit_logs
    Deletes AuditLog rows older than AUDIT_LOG_RETENTION_DAYS (default 90).
    Runs daily.  Deletes in batches of 1000 to avoid long-running transactions.

check_data_alerts
    Evaluates every active DataAlert against the current aggregate value of
    its target field.  Fires a Notification (+ optional email) for all workspace
    members when the threshold condition is met and the alert is not in cooldown.
    Runs every 15 minutes.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import connection
from django.db.models import Avg, Count, Max, Min, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUDIT_LOG_RETENTION_DAYS = int(getattr(settings, 'AUDIT_LOG_RETENTION_DAYS', 90))
_BATCH_SIZE = 1000

_AGG_FN = {
    'sum':   Sum,
    'avg':   Avg,
    'count': Count,
    'min':   Min,
    'max':   Max,
}


# ---------------------------------------------------------------------------
# Task 1: AuditLog retention
# ---------------------------------------------------------------------------

@shared_task(
    name='dashboards.prune_audit_logs',
    bind=True,
    max_retries=3,
    default_retry_delay=300,
    autoretry_for=(Exception,),
)
def prune_audit_logs(self):
    """
    Delete AuditLog rows older than AUDIT_LOG_RETENTION_DAYS.

    Uses batched deletes so the DB does not hold a single giant transaction.
    Returns the total number of rows deleted.
    """
    from apps.dashboards.models import AuditLog

    cutoff   = timezone.now() - timedelta(days=AUDIT_LOG_RETENTION_DAYS)
    total    = 0

    while True:
        # Fetch a batch of PKs older than cutoff
        ids = list(
            AuditLog.objects.filter(timestamp__lt=cutoff)
            .values_list('id', flat=True)[:_BATCH_SIZE]
        )
        if not ids:
            break
        deleted, _ = AuditLog.objects.filter(id__in=ids).delete()
        total += deleted
        logger.info('prune_audit_logs: deleted %d rows (total so far: %d)', deleted, total)

    logger.info('prune_audit_logs: finished, total deleted = %d (cutoff=%s)', total, cutoff.date())
    return {'deleted': total, 'cutoff': cutoff.isoformat()}


# ---------------------------------------------------------------------------
# Task 2: DataAlert checker
# ---------------------------------------------------------------------------

@shared_task(
    name='dashboards.check_data_alerts',
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
)
def check_data_alerts(self):
    """
    Evaluate all active DataAlerts and fire notifications when triggered.

    For each alert:
    1. Compute aggregate(field_name) from the target table's Records.
    2. Evaluate alert.operator against alert.threshold.
    3. If triggered and not in cooldown → notify all workspace members.
    4. Update last_triggered and last_value.
    """
    from apps.dashboards.models import DataAlert, Record
    from apps.notifications.models import Notification
    from apps.workspaces.models import WorkspaceMembership

    alerts = DataAlert.objects.filter(is_active=True).select_related('table', 'workspace')
    triggered_count = 0

    for alert in alerts:
        try:
            current_value = _compute_aggregate(alert)
        except Exception as exc:
            logger.warning('check_data_alerts: could not compute aggregate for alert %s: %s', alert.id, exc)
            continue

        if current_value is None:
            continue

        # Persist last seen value regardless of trigger
        DataAlert.objects.filter(pk=alert.pk).update(last_value=current_value)

        if not alert.evaluate(current_value):
            continue  # condition not met

        if alert.is_in_cooldown():
            logger.debug('check_data_alerts: alert %s in cooldown, skipping', alert.id)
            continue

        # --- Fire ---
        _fire_alert(alert, current_value)
        DataAlert.objects.filter(pk=alert.pk).update(last_triggered=timezone.now())
        triggered_count += 1

    logger.info('check_data_alerts: evaluated %d alerts, triggered %d', alerts.count(), triggered_count)
    return {'triggered': triggered_count}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_aggregate(alert):
    """
    Run the aggregate query for a DataAlert.

    Records store data in a JSONField ``data``.  We extract numeric values
    via a raw annotation for aggregate functions that need numbers.

    Returns float or None if no records / field not found.
    """
    from apps.dashboards.models import Record

    records = Record.objects.filter(table=alert.table, is_active=True)
    if not records.exists():
        return None

    agg_name = alert.aggregate   # 'sum' | 'avg' | 'count' | 'min' | 'max'
    field    = alert.field_name

    if agg_name == 'count':
        # Count records that have the field present (non-null)
        return float(records.filter(**{f'data__{field}__isnull': False}).count())

    # For numeric aggregates we need to cast the JSON value.
    # Pull values into Python and aggregate there (safe for typical SaaS volumes).
    values = list(records.values_list(f'data__{field}', flat=True))
    numeric = []
    for v in values:
        try:
            numeric.append(float(v))
        except (TypeError, ValueError):
            pass

    if not numeric:
        return None

    if agg_name == 'sum':
        return sum(numeric)
    if agg_name == 'avg':
        return sum(numeric) / len(numeric)
    if agg_name == 'min':
        return min(numeric)
    if agg_name == 'max':
        return max(numeric)

    return None


def _fire_alert(alert, current_value):
    """Create Notification records for all workspace members."""
    from apps.notifications.models import Notification
    from apps.workspaces.models import WorkspaceMembership

    members = WorkspaceMembership.objects.filter(
        workspace=alert.workspace
    ).select_related('user')

    message = (
        f"Alert '{alert.name}': "
        f"{alert.get_aggregate_display()} of '{alert.field_name}' "
        f"is {current_value:,.2f} "
        f"({alert.get_operator_display()} {alert.threshold:,.2f})"
    )

    for membership in members:
        Notification.notify(
            user=membership.user,
            title=f"Data Alert: {alert.name}",
            message=message,
            notif_type='warning',
            workspace=alert.workspace,
            action_url=f"/dashboard/alerts/",
            metadata={
                'alert_id':     str(alert.id),
                'current_value': current_value,
                'threshold':    alert.threshold,
            },
        )
    logger.info('check_data_alerts: fired alert %s (value=%.4f)', alert.id, current_value)
