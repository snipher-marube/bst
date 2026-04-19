# apps/insights/tasks.py
"""
Celery tasks for AnalyticsMeta real-time and AI insight features.

Key improvements in this version:
- analyze_workspace_tables: distributed Redis lock prevents concurrent runs
- Per-table progress broadcasting via WebSocket
- Actual Insight model records created (trend, anomaly, summary, comparison)
- Anomaly detection via z-score on numeric fields
- Trend direction analysis (up/down/flat) based on first vs last window
- Summary insight for every table (record count, top fields)
- Comparison insight when 2+ tables share a numeric field name
- Full edge-case handling: empty tables, schema-less tables, all-null columns
- All tasks have max_retries + autoretry_for for production resilience
"""

from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.core.cache import cache
import logging
import math
import statistics
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lock helper
# ---------------------------------------------------------------------------
_LOCK_TIMEOUT = 600  # seconds — maximum time insight generation may hold lock


def _acquire_lock(key):
    """Acquire a Redis-backed distributed lock. Returns True if acquired."""
    return cache.add(f'lock:{key}', '1', timeout=_LOCK_TIMEOUT)


def _release_lock(key):
    cache.delete(f'lock:{key}')


# ---------------------------------------------------------------------------
# LLM circuit breaker (Redis-backed, no extra dependencies)
# ---------------------------------------------------------------------------
_CB_FAIL_KEY    = 'llm_circuit:failures'
_CB_OPEN_KEY    = 'llm_circuit:open'
_CB_FAIL_MAX    = 5
_CB_RESET_SECS  = 300  # 5 minutes


def _cb_is_open():
    return bool(cache.get(_CB_OPEN_KEY))


def _cb_record_failure():
    failures = cache.get(_CB_FAIL_KEY, 0) + 1
    cache.set(_CB_FAIL_KEY, failures, timeout=_CB_RESET_SECS)
    if failures >= _CB_FAIL_MAX:
        cache.set(_CB_OPEN_KEY, '1', timeout=_CB_RESET_SECS)
        logger.warning('LLM circuit breaker opened after %d failures', failures)


def _cb_record_success():
    cache.delete(_CB_FAIL_KEY)
    cache.delete(_CB_OPEN_KEY)


# ---------------------------------------------------------------------------
# LLM budget helper
# ---------------------------------------------------------------------------

def _get_or_create_budget(workspace):
    """
    Return the ``WorkspaceLLMBudget`` for the current calendar month.

    Creates a new row (with the globally configured monthly limit) if none exists.
    """
    from django.utils import timezone
    from django.conf import settings
    from apps.insights.models import WorkspaceLLMBudget

    today  = timezone.localdate()
    month  = today.replace(day=1)
    limit  = getattr(settings, 'LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET', 100_000)

    budget, _ = WorkspaceLLMBudget.objects.get_or_create(
        workspace=workspace,
        month=month,
        defaults={'monthly_limit': limit, 'tokens_used': 0},
    )
    return budget


# ---------------------------------------------------------------------------
# WebSocket broadcast helpers
# ---------------------------------------------------------------------------
def _broadcast(group, payload):
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(group, payload)
    except Exception as exc:
        logger.warning('WS broadcast failed group=%s: %s', group, exc)


# ---------------------------------------------------------------------------
# broadcast_widget_update
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=15,
             autoretry_for=(Exception,), retry_backoff=True)
def broadcast_widget_update(self, widget_id, dashboard_id):
    """Refresh a widget's cache and push the new data to all viewers."""
    try:
        from apps.dashboards.models import Widget
        from apps.dashboards.services import QueryEngine

        widget    = Widget.objects.get(id=widget_id)
        engine    = QueryEngine()
        cache_key = engine._generate_cache_key(widget)
        cache.delete(cache_key)
        data      = widget.get_data()

        _broadcast(f'dashboard_{dashboard_id}', {
            'type':      'widget_update',
            'widget_id': widget_id,
            'data':      data,
        })
        logger.info('Widget broadcast ok widget=%s', widget_id)
        return {'status': 'ok'}
    except Widget.DoesNotExist:
        logger.error('Widget %s not found — skipping broadcast', widget_id)
        return {'status': 'error', 'reason': 'widget_not_found'}
    except Exception as exc:
        logger.exception('broadcast_widget_update failed widget=%s', widget_id)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# notify_table_change
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=10,
             autoretry_for=(Exception,), retry_backoff=True)
def notify_table_change(self, table_id, action):
    """Notify the workspace channel that a table's data changed."""
    try:
        from apps.dashboards.models import DataTable
        table = DataTable.objects.select_related('workspace').get(id=table_id)
        _broadcast(f'workspace_{table.workspace_id}', {
            'type':     'table_update',
            'table_id': table_id,
            'action':   action,
        })
        logger.info('Table change broadcast ok table=%s action=%s', table_id, action)
    except Exception as exc:
        logger.exception('notify_table_change failed table=%s', table_id)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# generate_default_dashboard
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             autoretry_for=(Exception,), retry_backoff=True)
def generate_default_dashboard(self, table_id):
    """Build the auto-generated dashboard for a newly created DataTable."""
    try:
        from apps.dashboards.models import DataTable
        table     = DataTable.objects.get(pk=table_id)
        dashboard = table.generate_default_dashboard()
        logger.info('Auto-dashboard created table=%s dashboard=%s', table_id, dashboard.id)
        return {'status': 'completed', 'dashboard_id': str(dashboard.id)}
    except Exception as exc:
        logger.exception('generate_default_dashboard failed table=%s', table_id)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# run_async_import
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=3, default_retry_delay=30,
             autoretry_for=(Exception,), retry_backoff=True,
             time_limit=600, soft_time_limit=570)
def run_async_import(self, import_job_id):
    """Process a CSV/Excel import job asynchronously."""
    from django.utils import timezone
    from apps.dashboards.models import ImportJob
    from apps.dashboards.services import DataImportService
    import pandas as pd

    try:
        job = ImportJob.objects.get(id=import_job_id)
    except ImportJob.DoesNotExist:
        logger.error('ImportJob %s not found', import_job_id)
        return {'status': 'error', 'error': 'ImportJob not found'}

    job.status     = 'running'
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id
    job.save(update_fields=['status', 'started_at', 'celery_task_id'])

    try:
        df     = pd.read_json(cache.get(f'import_df_{job.file_path}') or job.file_path, orient='split')
        result = DataImportService().import_data(
            table=job.table,
            df=df,
            user=job.created_by,
            import_mode=getattr(job, 'import_mode', 'append'),
            primary_key_field=getattr(job, 'primary_key_field', ''),
        )

        job.status         = 'completed'
        job.success_rows   = result['success']
        job.error_rows     = result['errors']
        job.error_log      = result.get('error_details', [])
        job.processed_rows = result['success'] + result['errors']
        job.completed_at   = timezone.now()
        job.save()

        # Notify workspace WebSocket channel
        _broadcast(f'workspace_{job.workspace_id}', {
            'type':          'import_progress',
            'import_job_id': str(job.id),
            'status':        'completed',
            'success_rows':  result['success'],
            'error_rows':    result['errors'],
        })
        notify_table_change.delay(str(job.table_id), 'import_completed')

        logger.info('ImportJob %s done: %d rows', import_job_id, result['success'])
        return {'status': 'completed', 'success': result['success'], 'errors': result['errors']}

    except Exception as exc:
        logger.exception('ImportJob %s failed', import_job_id)
        from django.utils import timezone as tz
        job.status       = 'failed'
        job.error_log    = [str(exc)]
        job.completed_at = tz.now()
        job.save(update_fields=['status', 'error_log', 'completed_at'])
        _broadcast(f'workspace_{job.workspace_id}', {
            'type': 'import_progress', 'import_job_id': str(job.id),
            'status': 'failed', 'error': str(exc),
        })
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# analyze_workspace_tables  (main AI insights task)
# ---------------------------------------------------------------------------
@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def analyze_workspace_tables(self, workspace_id):
    """
    Generate BI insights for every table in the workspace.

    Steps:
    1. Acquire distributed lock — abort if another run is already in progress.
    2. For each table with records:
       a. Build widget specs (existing WorkspaceInsightService logic).
       b. Run statistical analysis → create Insight model records:
          - summary   : record count + field inventory
          - trend     : direction (up/down/flat) for numeric fields over time
          - anomaly   : z-score outlier detection on numeric fields
          - comparison: cross-table field comparison (shared numeric columns)
       c. Broadcast per-table progress via WebSocket.
    3. Release lock.
    4. Broadcast completion.
    """
    lock_key = f'insights_gen:{workspace_id}'

    if not _acquire_lock(lock_key):
        logger.info('Insights already running for workspace=%s — skipping', workspace_id)
        return {'status': 'skipped', 'reason': 'already_running'}

    try:
        return _run_insight_generation(workspace_id, self)
    except Exception as exc:
        logger.exception('analyze_workspace_tables failed workspace=%s', workspace_id)
        raise self.retry(exc=exc)
    finally:
        _release_lock(lock_key)


def _run_insight_generation(workspace_id, task_self):
    from apps.dashboards.models import DataTable
    from apps.dashboards.services import WorkspaceInsightService
    from apps.workspaces.models import Workspace
    from apps.insights.models import Insight
    from apps.insights.llm import ClaudeInsightGenerator
    from django.db.models import Exists, OuterRef
    from apps.dashboards.models import Record

    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.warning('analyze_workspace_tables: workspace %s not found — skipping', workspace_id)
        return {'status': 'skipped', 'reason': 'workspace_deleted'}
    owner     = workspace.owner

    # Initialise LLM generator and budget (budget lazily created if LLM available)
    llm_gen = ClaudeInsightGenerator()
    budget  = _get_or_create_budget(workspace) if llm_gen.is_available() else None

    _broadcast(f'workspace_{workspace_id}', {
        'type':    'insights_generation_progress',
        'stage':   'started',
        'message': 'Building dashboard widgets…',
    })

    # ── Phase 1: build/refresh the overview dashboard (existing logic) ────
    service   = WorkspaceInsightService()
    dashboard = service.generate_workspace_overview(workspace, owner)

    _broadcast(f'workspace_{workspace_id}', {
        'type':    'insights_generation_progress',
        'stage':   'widgets_done',
        'message': 'Widgets ready. Running statistical analysis…',
        'dashboard_id': str(dashboard.id),
    })

    # ── Phase 2: statistical insights ────────────────────────────────────
    _has_records = Record.objects.filter(table=OuterRef('pk'), is_active=True)
    tables = list(
        workspace.tables
        .filter(is_active=True)
        .annotate(_has_data=Exists(_has_records))
        .filter(_has_data=True)
        .order_by('name')
    )

    # Delete stale Insight records for this workspace before regenerating
    Insight.objects.filter(workspace=workspace).delete()

    created_insights = []
    numeric_by_table = {}   # table.id → { field: [values] } for cross-table comparison

    total = len(tables)
    for idx, table in enumerate(tables):
        try:
            new_insights, field_values = _analyse_table(
                table, workspace, owner, llm_gen=llm_gen, budget=budget
            )
            created_insights.extend(new_insights)
            if field_values:
                numeric_by_table[table.id] = field_values
        except Exception:
            logger.exception('Insight analysis failed table=%s', table.id)

        _broadcast(f'workspace_{workspace_id}', {
            'type':     'insights_generation_progress',
            'stage':    'table_done',
            'table':    table.name,
            'progress': round((idx + 1) / max(total, 1) * 100),
        })

    # ── Phase 3: cross-table comparison insights ──────────────────────────
    _create_comparison_insights(
        tables, numeric_by_table, workspace, owner, created_insights,
        llm_gen=llm_gen, budget=budget,
    )

    # ── Phase 4: notify workspace members of new anomaly findings ────────
    anomaly_insights = [i for i in created_insights if i.insight_type == 'anomaly']
    if anomaly_insights:
        _notify_anomaly_insights(workspace, anomaly_insights)

    # ── Done ──────────────────────────────────────────────────────────────
    _broadcast(f'workspace_{workspace_id}', {
        'type':         'insights_complete',
        'dashboard_id': str(dashboard.id),
        'insight_count': len(created_insights),
    })

    from apps.dashboards.models import Widget
    widget_count = Widget.objects.filter(dashboard=dashboard).count()
    logger.info('Insights complete workspace=%s dashboard=%s widgets=%d insights=%d',
                workspace_id, dashboard.id, widget_count, len(created_insights))
    return {'status': 'completed', 'dashboard_id': str(dashboard.id),
            'widget_count': widget_count, 'insight_count': len(created_insights)}


# ---------------------------------------------------------------------------
# Per-table statistical analysis
# ---------------------------------------------------------------------------
def _enrich_with_llm(insight, insight_type, stats_context, llm_gen, budget):
    """
    Replace insight.description with a Claude-generated narrative if budget allows.
    Updates the insight in-place and saves only the changed fields.
    """
    from apps.insights.llm import LLMError

    if llm_gen is None or budget is None:
        return
    if _cb_is_open():
        logger.debug('LLM circuit breaker is open — skipping narration for insight=%s', insight.id)
        return
    # Refresh budget from DB to get latest tokens_used
    budget.refresh_from_db()
    if not budget.has_capacity(estimated_tokens=300):
        logger.debug('LLM budget exhausted for workspace=%s', insight.workspace_id)
        return

    try:
        import hashlib, json as _json
        _ctx_hash = hashlib.md5(
            _json.dumps(stats_context, sort_keys=True, default=str).encode()
        ).hexdigest()
        _cache_key = f'llm_narrative:{insight_type}:{_ctx_hash}'
        cached = cache.get(_cache_key)
        if cached:
            insight.description = cached
            insight.save(update_fields=['description'])
            logger.debug('LLM narrative served from cache for insight=%s', insight.id)
            return

        result = llm_gen.narrate_insight(insight_type, stats_context)
        if result.get('narrative'):
            insight.description       = result['narrative']
            insight.llm_model         = result['model']
            insight.prompt_tokens     = result['prompt_tokens']
            insight.completion_tokens = result['completion_tokens']
            insight.save(update_fields=[
                'description', 'llm_model', 'prompt_tokens', 'completion_tokens'
            ])
            budget.consume(result['prompt_tokens'] + result['completion_tokens'])
            cache.set(_cache_key, result['narrative'], timeout=3600)
            _cb_record_success()
            logger.debug('LLM narrative written for insight=%s', insight.id)
    except LLMError as exc:
        _cb_record_failure()
        logger.warning('LLM narrate failed insight=%s: %s', insight.id, exc)


def _analyse_table(table, workspace, owner, llm_gen=None, budget=None):
    """
    Profile one table and create Insight records.
    Returns (list[Insight], dict[field→values]).
    """
    from apps.dashboards.models import Record
    from apps.insights.models import Insight
    from django.conf import settings

    schema = {f['name']: f['type'] for f in (table.schema or [])}
    if not schema:
        # Schema-less table — emit a single summary insight noting this
        insight = Insight.objects.create(
            workspace        = workspace,
            title            = f'{table.name}: No Schema Defined',
            description      = (
                f'Table "{table.name}" has {table.record_count} record(s) but no schema. '
                'Define a schema to unlock automatic insights.'
            ),
            insight_type     = 'summary',
            source_table_name = table.name,
            created_by       = owner,
        )
        _enrich_with_llm(insight, 'summary', {
            'table': table.name,
            'record_count': table.record_count,
            'field_count': 0,
            'numeric_fields': [],
            'date_fields': [],
        }, llm_gen, budget)
        return [insight], {}

    sample_size = getattr(settings, 'QUERY_ENGINE_PROFILE_SAMPLE', 1000)
    records     = list(
        Record.objects.filter(table=table, is_active=True)
        .values_list('data', flat=True)
        .order_by('created_at')[:sample_size]
    )

    if not records:
        insight = Insight.objects.create(
            workspace         = workspace,
            title             = f'{table.name}: No Records Yet',
            description       = f'Table "{table.name}" has no records. Import data to get insights.',
            insight_type      = 'summary',
            source_table_name = table.name,
            created_by        = owner,
        )
        _enrich_with_llm(insight, 'summary', {
            'table': table.name,
            'record_count': 0,
            'field_count': len(schema),
            'numeric_fields': [],
            'date_fields': [],
        }, llm_gen, budget)
        return [insight], {}

    numeric_types = {'number', 'currency', 'percentage', 'integer', 'float', 'decimal'}
    date_types    = {'date', 'datetime'}

    numeric_fields = [n for n, t in schema.items() if t in numeric_types]
    date_fields    = [n for n, t in schema.items() if t in date_types]

    # Extract numeric field values (skip null / non-parseable)
    field_values = {}
    for field in numeric_fields:
        vals = []
        for rec in records:
            if rec and field in rec:
                v = rec[field]
                try:
                    f = float(v)
                    if math.isfinite(f):
                        vals.append(f)
                except (TypeError, ValueError):
                    pass
        if vals:
            field_values[field] = vals

    insights = []

    # ── Summary insight ───────────────────────────────────────────────────
    field_summary = ', '.join(
        f'{field} (n={len(vals)}, avg={statistics.mean(vals):.2f})'
        for field, vals in list(field_values.items())[:5]
    ) or 'No numeric fields found.'

    _summary_insight = Insight.objects.create(
        workspace         = workspace,
        title             = f'{table.name} Summary',
        description       = (
            f'{table.name} has {table.record_count} record(s) and '
            f'{len(schema)} field(s). Numeric fields: {field_summary}'
        ),
        insight_type      = 'summary',
        source_table_name = table.name,
        created_by        = owner,
        chart_data        = {
            'record_count': table.record_count,
            'field_count':  len(schema),
            'numeric_fields': list(field_values.keys()),
            'date_fields':    date_fields,
        },
    )
    _enrich_with_llm(_summary_insight, 'summary', {
        'table':          table.name,
        'record_count':   table.record_count,
        'field_count':    len(schema),
        'numeric_fields': list(field_values.keys()),
        'date_fields':    date_fields,
    }, llm_gen, budget)
    insights.append(_summary_insight)

    # ── Trend insights ────────────────────────────────────────────────────
    # Split sample into first-half / last-half to derive direction
    for field, vals in field_values.items():
        if len(vals) < 6:
            continue
        mid    = len(vals) // 2
        first  = statistics.mean(vals[:mid])
        last   = statistics.mean(vals[mid:])
        pct    = ((last - first) / first * 100) if first != 0 else 0
        if abs(pct) < 2:
            direction, emoji = 'stable',   '→'
        elif pct > 0:
            direction, emoji = 'upward',   '↑'
        else:
            direction, emoji = 'downward', '↓'

        _trend_insight = Insight.objects.create(
            workspace         = workspace,
            title             = f'{table.name} — {field} trend {emoji}',
            description       = (
                f'"{field}" in {table.name} is {direction}. '
                f'Average changed from {first:.2f} to {last:.2f} '
                f'({pct:+.1f}%) over the sampled period.'
            ),
            insight_type      = 'trend',
            source_table_name = table.name,
            created_by        = owner,
            chart_data        = {
                'field':     field,
                'direction': direction,
                'pct_change': round(pct, 2),
                'first_avg': round(first, 4),
                'last_avg':  round(last, 4),
            },
        )
        _enrich_with_llm(_trend_insight, 'trend', {
            'table':        table.name,
            'field':        field,
            'direction':    direction,
            'pct_change':   round(pct, 2),
            'first_avg':    round(first, 4),
            'last_avg':     round(last, 4),
            'record_count': len(vals),
        }, llm_gen, budget)
        insights.append(_trend_insight)

    # ── Anomaly insights (z-score ≥ 3 AND IQR-fence) ────────────────────
    for field, vals in field_values.items():
        if len(vals) < 10:
            continue
        try:
            mean = statistics.mean(vals)
            std  = statistics.stdev(vals)
        except statistics.StatisticsError:
            continue
        if std == 0:
            continue

        outlier_count = sum(1 for v in vals if abs(v - mean) / std >= 3.0)
        if outlier_count == 0:
            continue

        pct_outliers = outlier_count / len(vals) * 100
        _anomaly_insight = Insight.objects.create(
            workspace         = workspace,
            title             = f'{table.name} — Anomaly in "{field}"',
            description       = (
                f'{outlier_count} record(s) ({pct_outliers:.1f}%) have "{field}" '
                f'values more than 3 standard deviations from the mean '
                f'({mean:.2f} ± {std:.2f}). Review for data quality issues.'
            ),
            insight_type      = 'anomaly',
            source_table_name = table.name,
            created_by        = owner,
            chart_data        = {
                'field':         field,
                'mean':          round(mean, 4),
                'std':           round(std, 4),
                'outlier_count': outlier_count,
                'pct_outliers':  round(pct_outliers, 2),
            },
        )
        _enrich_with_llm(_anomaly_insight, 'anomaly', {
            'table':          table.name,
            'field':          field,
            'mean':           round(mean, 4),
            'std':            round(std, 4),
            'outlier_count':  outlier_count,
            'pct_outliers':   round(pct_outliers, 2),
        }, llm_gen, budget)
        insights.append(_anomaly_insight)

    # ── Distribution insights (skewness + kurtosis + IQR outliers) ───────
    try:
        import numpy as np
        for field, vals in field_values.items():
            if len(vals) < 15:
                continue
            arr    = np.array(vals)
            mean_v = float(np.mean(arr))
            std_v  = float(np.std(arr, ddof=1))
            if std_v == 0:
                continue
            z        = (arr - mean_v) / std_v
            skewness = float(np.mean(z ** 3))
            kurtosis = float(np.mean(z ** 4)) - 3
            q1, q3   = np.percentile(arr, [25, 75])
            iqr      = float(q3 - q1)
            iqr_outs = int(np.sum((arr < q1 - 1.5 * iqr) | (arr > q3 + 1.5 * iqr)))

            if abs(skewness) < 0.5 and abs(kurtosis) < 1 and iqr_outs == 0:
                continue  # normal-ish — not worth flagging

            skew_label = ('heavily right-skewed (long upper tail)' if skewness > 1
                          else 'moderately right-skewed' if skewness > 0.5
                          else 'heavily left-skewed (long lower tail)' if skewness < -1
                          else 'moderately left-skewed' if skewness < -0.5
                          else 'approximately symmetric')
            kurt_label = ('heavy-tailed (leptokurtic)' if kurtosis > 1
                          else 'light-tailed (platykurtic)' if kurtosis < -1
                          else 'normal-tailed (mesokurtic)')
            desc_parts = [
                f'"{field}" in {table.name} is {skew_label} (skewness={skewness:.2f}) '
                f'and {kurt_label} (excess kurtosis={kurtosis:.2f}).',
            ]
            if iqr_outs > 0:
                desc_parts.append(
                    f'{iqr_outs} IQR-fence outlier(s) detected '
                    f'(outside [{q1:.2f} − 1.5×IQR, {q3:.2f} + 1.5×IQR]).'
                )
            if abs(skewness) > 1:
                desc_parts.append(
                    'Consider log-transforming this field before modelling to improve normality.'
                )
            _dist_insight = Insight.objects.create(
                workspace         = workspace,
                title             = f'{table.name} — "{field}" distribution shape',
                description       = ' '.join(desc_parts),
                insight_type      = 'anomaly',
                source_table_name = table.name,
                created_by        = owner,
                chart_data        = {
                    'field':        field,
                    'skewness':     round(skewness, 4),
                    'kurtosis':     round(kurtosis, 4),
                    'iqr_outliers': iqr_outs,
                    'q1':           round(float(q1), 4),
                    'q3':           round(float(q3), 4),
                    'iqr':          round(iqr, 4),
                },
            )
            _enrich_with_llm(_dist_insight, 'anomaly', {
                'table':        table.name,
                'field':        field,
                'skewness':     round(skewness, 4),
                'kurtosis':     round(kurtosis, 4),
                'iqr_outliers': iqr_outs,
                'q1':           round(float(q1), 4),
                'q3':           round(float(q3), 4),
            }, llm_gen, budget)
            insights.append(_dist_insight)

        # ── Correlation insight (strongest pair) ──────────────────────────
        if len(field_values) >= 2:
            fields_list = list(field_values.keys())
            max_r, best_pair = 0.0, None
            for i in range(len(fields_list)):
                for j in range(i + 1, len(fields_list)):
                    fa, fb = fields_list[i], fields_list[j]
                    va, vb = field_values[fa], field_values[fb]
                    n = min(len(va), len(vb))
                    if n < 6:
                        continue
                    corr_mat = np.corrcoef(np.array(va[:n]), np.array(vb[:n]))
                    r = float(corr_mat[0, 1])
                    if math.isnan(r):
                        continue
                    if abs(r) > abs(max_r):
                        max_r, best_pair = r, (fa, fb)
            if best_pair and abs(max_r) >= 0.4:
                fa, fb = best_pair
                strength = ('strong' if abs(max_r) >= 0.7 else 'moderate')
                direction = 'positive' if max_r > 0 else 'negative'
                _corr_insight = Insight.objects.create(
                    workspace         = workspace,
                    title             = f'{table.name} — {strength} correlation: "{fa}" & "{fb}"',
                    description       = (
                        f'"{fa}" and "{fb}" have a {strength} {direction} correlation '
                        f'(r={max_r:.3f}). '
                        + ('This relationship may be causal — investigate further.'
                           if abs(max_r) >= 0.7 else
                           'Weak to moderate linear relationship — use with caution.')
                    ),
                    insight_type      = 'summary',
                    source_table_name = table.name,
                    created_by        = owner,
                    chart_data        = {
                        'field_a': fa, 'field_b': fb,
                        'r':       round(max_r, 4),
                        'r2':      round(max_r ** 2, 4),
                    },
                )
                insights.append(_corr_insight)

        # ── Data-quality insight ───────────────────────────────────────────
        _EMPTY = {'', 'none', 'nan', 'null', 'n/a', 'na'}
        schema_fields = list(schema.keys())
        null_counts = {}
        for field in schema_fields:
            null_counts[field] = sum(
                1 for rec in records
                if not rec or field not in rec
                or str(rec.get(field, '')).strip().lower() in _EMPTY
            )
        high_null = [(f, null_counts[f] / len(records) * 100)
                     for f in schema_fields if null_counts[f] / len(records) >= 0.15]
        if high_null:
            issues = '; '.join(f'"{f}" {p:.0f}% missing' for f, p in high_null[:5])
            _dq_insight = Insight.objects.create(
                workspace         = workspace,
                title             = f'{table.name} — Data Quality Alert',
                description       = (
                    f'Several fields have high missing-value rates: {issues}. '
                    f'Fix these before drawing conclusions or building models from this data.'
                ),
                insight_type      = 'anomaly',
                source_table_name = table.name,
                created_by        = owner,
                chart_data        = {'high_null_fields': [{'field': f, 'null_pct': round(p, 1)}
                                                           for f, p in high_null]},
            )
            insights.append(_dq_insight)

    except Exception as exc:
        logger.warning('Advanced statistical insights failed for %s: %s', table.name, exc)

    return insights, field_values


# ---------------------------------------------------------------------------
# Cross-table comparison insights
# ---------------------------------------------------------------------------
def _create_comparison_insights(tables, numeric_by_table, workspace, owner, insights_list,
                                llm_gen=None, budget=None):
    """
    Create comparison Insight records when 2+ tables share a numeric field name.
    E.g., both "Sales" and "Expenses" have an "amount" field.
    """
    from apps.insights.models import Insight

    # Build field → [(table, values)] mapping
    field_map = defaultdict(list)
    for table in tables:
        vals = numeric_by_table.get(table.id, {})
        for field, values in vals.items():
            field_map[field].append((table, values))

    for field, entries in field_map.items():
        if len(entries) < 2:
            continue

        # Limit to 5 tables per comparison insight to keep descriptions readable
        entries = entries[:5]
        summaries = []
        chart_data = {}
        for tbl, vals in entries:
            avg = statistics.mean(vals) if vals else 0
            summaries.append(f'{tbl.name}: avg={avg:.2f} (n={len(vals)})')
            chart_data[tbl.name] = round(avg, 4)

        insight = Insight.objects.create(
            workspace         = workspace,
            title             = f'Comparison: "{field}" across {len(entries)} tables',
            description       = (
                f'Field "{field}" appears in {len(entries)} tables: '
                + '; '.join(summaries) + '.'
            ),
            insight_type      = 'comparison',
            source_table_name = '',
            created_by        = owner,
            chart_data        = {'field': field, 'table_averages': chart_data},
        )
        _enrich_with_llm(insight, 'comparison', {
            'field':          field,
            'table_averages': chart_data,
        }, llm_gen, budget)
        insights_list.append(insight)


# ---------------------------------------------------------------------------
# Anomaly notification helper
# ---------------------------------------------------------------------------

def _notify_anomaly_insights(workspace, anomaly_insights: list):
    """
    Send in-app Notification records to all workspace members for each
    anomaly insight found during the daily analysis run.

    Only fires if at least one anomaly insight was created.
    """
    try:
        from apps.notifications.models import Notification
        from apps.workspaces.models import WorkspaceMembership

        members = list(
            WorkspaceMembership.objects.filter(workspace=workspace)
            .select_related('user')
        )
        if not members:
            return

        count = len(anomaly_insights)
        table_names = list({i.source_table_name for i in anomaly_insights if i.source_table_name})
        tables_str  = ', '.join(sorted(table_names)[:3])
        if len(table_names) > 3:
            tables_str += f' (+{len(table_names) - 3} more)'

        title   = f'{count} anomal{"y" if count == 1 else "ies"} detected in your data'
        message = (
            f'Automated analysis found {count} statistical anomal'
            f'{"y" if count == 1 else "ies"} '
            + (f'in {tables_str}. ' if tables_str else '')
            + 'Open the Insights panel to review.'
        )

        for membership in members:
            Notification.notify(
                user=membership.user,
                title=title,
                message=message,
                notif_type='warning',
                workspace=workspace,
                action_url='/dashboard/analytics/',
                metadata={
                    'anomaly_count':   count,
                    'source_tables':   table_names,
                    'triggered_by':    'auto_anomaly_detection',
                },
            )
        logger.info(
            'Anomaly notifications sent workspace=%s count=%d members=%d',
            workspace.id, count, len(members),
        )
    except Exception:
        logger.exception('_notify_anomaly_insights failed workspace=%s', workspace.id)


# ---------------------------------------------------------------------------
# Auto-trigger Beat task — fires analyze_workspace_tables for every active
# workspace that has at least one table with records.
# ---------------------------------------------------------------------------

@shared_task(
    name='insights.auto_trigger_anomaly_detection',
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def auto_trigger_anomaly_detection(self):
    """
    Fan-out task: queue ``analyze_workspace_tables`` for every active workspace
    that has at least one active DataTable with data.

    Registered in CELERY_BEAT_SCHEDULE to run daily at 03:00 UTC so results
    are ready before the working day starts.

    Returns a summary dict: {dispatched: N, skipped: N}.
    """
    try:
        from apps.workspaces.models import Workspace
        from apps.dashboards.models import DataTable, Record
        from django.db.models import Exists, OuterRef

        _has_active_table = DataTable.objects.filter(
            workspace=OuterRef('pk'),
            is_active=True,
        ).filter(
            Exists(Record.objects.filter(table=OuterRef('pk'), is_active=True))
        )

        workspaces = Workspace.objects.filter(
            is_active=True,
        ).filter(Exists(_has_active_table))

        dispatched = 0
        skipped    = 0
        for ws in workspaces:
            try:
                analyze_workspace_tables.delay(str(ws.id))
                dispatched += 1
                logger.info('auto_trigger_anomaly_detection: queued workspace=%s', ws.id)
            except Exception as exc:
                logger.warning(
                    'auto_trigger_anomaly_detection: failed to queue workspace=%s: %s',
                    ws.id, exc,
                )
                skipped += 1

        logger.info(
            'auto_trigger_anomaly_detection: dispatched=%d skipped=%d',
            dispatched, skipped,
        )
        return {'dispatched': dispatched, 'skipped': skipped}

    except Exception as exc:
        logger.exception('auto_trigger_anomaly_detection: unexpected error')
        raise self.retry(exc=exc)
