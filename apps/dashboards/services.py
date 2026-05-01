import json
import re
import pandas as pd
import numpy as np
from django.conf import settings
from django.core.cache import cache
import logging

from apps.dashboards.models import Record

logger = logging.getLogger(__name__)

class QueryEngine:
    """
    Enhanced query engine that understands user-defined schemas
    """

    def __init__(self):
        # Long TTL — cache is invalidated by data changes via the cache key itself
        # (table record_count + updated_at are baked into the key).
        self.cache_timeout = getattr(settings, 'WIDGET_CACHE_TTL', 60 * 60 * 24 * 30)


    def _generate_cache_key(self, widget, extra_filters=None):
        """Generate unique cache key for widget query"""
        import hashlib
        from django.core.serializers.json import DjangoJSONEncoder

        # Convert UUID to string for serialization
        table_id = str(widget.table_id) if widget.table_id else 'none'

        # Include table data fingerprint so cache is invalidated automatically
        # whenever records are added or the table is updated — no manual clearing needed.
        table = widget.table
        table_fingerprint = ''
        if table is not None:
            rc = getattr(table, 'record_count', None) or 0
            ua = getattr(table, 'updated_at', None)
            table_fingerprint = f'{rc}:{ua}'

        key_data = {
            'table_id': table_id,
            'table_data': table_fingerprint,
            'query_config': widget.query_config,
            'widget_type': widget.widget_type,
            'extra_filters': extra_filters or [],
        }

        key_str = json.dumps(key_data, sort_keys=True, cls=DjangoJSONEncoder)
        cache_key = hashlib.md5(key_str.encode()).hexdigest()
        return f"widget_query:{cache_key}"
    
    def _apply_filters(self, queryset, filters):
        """Apply filters to a queryset (filters on JSON data field)"""
        for f in filters:
            field = f.get('field')
            operator = f.get('operator', 'eq')
            value = f.get('value')
            if not field or value is None:
                continue
            if operator == 'eq':
                queryset = queryset.filter(**{f'data__{field}': value})
            elif operator == 'neq':
                queryset = queryset.exclude(**{f'data__{field}': value})
            elif operator == 'contains':
                queryset = queryset.filter(**{f'data__{field}__icontains': value})
            elif operator == 'gt':
                queryset = queryset.filter(**{f'data__{field}__gt': value})
            elif operator == 'gte':
                queryset = queryset.filter(**{f'data__{field}__gte': value})
            elif operator == 'lt':
                queryset = queryset.filter(**{f'data__{field}__lt': value})
            elif operator == 'lte':
                queryset = queryset.filter(**{f'data__{field}__lte': value})
        return queryset

    def _execute_table_query(self, widget, limit, extra_filters=None):
        """Execute table widget query - return raw records"""
        from .models import Record

        config = widget.query_config or {}

        # Base queryset
        queryset = Record.objects.filter(
            table=widget.table,
            is_active=True
        ).order_by('-created_at')

        # Apply widget-level filters then dashboard-level extra_filters
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        if extra_filters:
            queryset = self._apply_filters(queryset, extra_filters)

        # Get records
        records = list(queryset.values('data', 'created_at')[:limit])
    
        # Format for display
        result = []
        for record in records:
            row = record['data'].copy() if record['data'] else {}
            row['_created_at'] = record['created_at'].isoformat() if record['created_at'] else None
            result.append(row)
    
        return result

    def execute_widget_query(self, widget, limit=1000, extra_filters=None):
        """Execute widget query with better error handling.

        Args:
            widget: Widget model instance.
            limit: Maximum rows to return.
            extra_filters: List of dashboard-level filter dicts to merge with
                widget-level filters.  Same schema as query_config['filters']:
                [{"field": "region", "operator": "eq", "value": "EMEA"}]
        """
        try:
            # Route to the appropriate connector engine for external data-source widgets
            if getattr(widget, 'data_source_id', None) and widget.source_table_name:
                ds = widget.data_source
                if ds.connector_type == 'google_sheets':
                    return GoogleSheetsQueryEngine(ds).execute_widget_query(widget, extra_filters=extra_filters)
                ds_engine = DataSourceQueryEngine(ds)
                return ds_engine.execute_widget_query(widget, limit=limit, extra_filters=extra_filters)

            # Validate widget has a table
            if not widget.table:
                return {"error": "No table selected"}

            # Normalise extra_filters to a list
            extra_filters = extra_filters or []

            # Generate cache key (includes extra_filters so per-filter results cache separately)
            cache_key = self._generate_cache_key(widget, extra_filters)

            # Try cache (skip if extra_filters present — live results expected)
            if not extra_filters:
                cached = cache.get(cache_key)
                if cached:
                    return cached

            # Execute based on widget type
            if widget.widget_type in ('metric', 'number', 'gauge'):
                data = self._execute_metric_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type == 'table':
                data = self._execute_table_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type in ['line_chart', 'area_chart', 'bar_chart', 'pie_chart',
                                         'heatmap', 'treemap', 'funnel_chart', 'waterfall', 'donut']:
                data = self._execute_chart_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type == 'scatter':
                data = self._execute_scatter_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type == 'histogram':
                data = self._execute_histogram_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type == 'box_plot':
                data = self._execute_box_plot_query(widget, limit, extra_filters=extra_filters)
            elif widget.widget_type == 'cohort':
                data = CohortAnalysisEngine().execute(widget)
            elif widget.widget_type == 'funnel':
                data = FunnelAnalysisEngine().execute(widget)
            else:
                data = {"error": f"Unknown widget type: {widget.widget_type}"}

            # Cache only when no extra_filters (cache baseline results only)
            if not extra_filters and data and 'error' not in data:
                cache.set(cache_key, data, self.cache_timeout)

            return data

        except Exception as e:
            logger.error(f"Widget query error: {str(e)}", exc_info=True)
            return {"error": str(e)}
    
    # ------------------------------------------------------------------
    # DB-side aggregation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _db_numeric_expr(field):
        """Return a Django expression that casts a JSON text field to float.

        Works on both SQLite (3.38+, ships with Python 3.12+) and PostgreSQL
        via Django's KeyTextTransform + Cast.
        """
        from django.db.models import FloatField
        from django.db.models.functions import Cast
        from django.db.models.fields.json import KeyTextTransform
        return Cast(KeyTextTransform(field, 'data'), output_field=FloatField())

    @staticmethod
    def _db_scalar_agg(queryset, agg_type, field):
        """Run a single scalar DB aggregation; returns float or None."""
        from django.db.models import Sum, Avg, Min, Max
        expr = QueryEngine._db_numeric_expr(field)
        fn   = {'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}[agg_type]
        return queryset.aggregate(_r=fn(expr))['_r']

    def _execute_metric_query(self, widget, _limit, extra_filters=None):
        """Execute metric widget query using DB-side aggregation.

        All count/sum/avg/min/max operations are pushed into the database.
        This eliminates the previous O(n) Python loop that fetched up to
        10,000 records into memory.
        """
        from .models import Record

        config = widget.query_config or {}
        aggregations = config.get('aggregations', [])

        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        if extra_filters:
            queryset = self._apply_filters(queryset, extra_filters)

        # Separate calculated aggs — they must be resolved AFTER base aggs
        base_aggs = [a for a in aggregations if a.get('type') != 'calculated']
        calc_aggs = [a for a in aggregations if a.get('type') == 'calculated']

        if not base_aggs and not calc_aggs:
            return {'val': queryset.count()}

        result = {}
        for agg in base_aggs:
            agg_type = agg.get('type', 'count')
            field    = agg.get('field')
            name     = agg.get('name', 'val')

            try:
                if agg_type == 'count':
                    result[name] = queryset.count()
                elif agg_type in ('sum', 'avg', 'min', 'max') and field:
                    value = self._db_scalar_agg(queryset, agg_type, field)
                    result[name] = round(float(value), 4) if value is not None else 0
                else:
                    result[name] = 0
            except Exception as exc:
                # DB-side cast fails for non-numeric values (e.g. SQLite without
                # JSON1 or mixed-type columns). Fall back to Python aggregation.
                logger.warning(
                    f"DB aggregation failed for field='{field}' agg='{agg_type}': {exc}. "
                    "Falling back to Python-side aggregation."
                )
                result[name] = self._python_scalar_agg(queryset, agg_type, field)

        # Resolve calculated aggs — pass queryset for column-level expression support
        if calc_aggs:
            result = self._resolve_calculated_aggs(calc_aggs, result, queryset=queryset)

        return result

    def _python_scalar_agg(self, queryset, agg_type, field):
        """Python-side scalar aggregation fallback (loads data into memory)."""
        max_records = getattr(settings, 'QUERY_ENGINE_MAX_RECORDS', 10_000)
        values = []
        for record_data in queryset.values_list('data', flat=True)[:max_records]:
            if record_data and field in record_data:
                try:
                    values.append(float(record_data[field]))
                except (ValueError, TypeError):
                    pass
        if not values:
            return 0
        return {
            'sum': sum(values),
            'avg': sum(values) / len(values),
            'min': min(values),
            'max': max(values),
        }.get(agg_type, 0)
    
    # ------------------------------------------------------------------
    # DB-side helpers for chart aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _chart_sort_key(k):
        """Sort key: ISO dates sort chronologically first; strings alphabetically."""
        try:
            from datetime import date as _date
            return (0, _date.fromisoformat(str(k)))
        except (ValueError, TypeError):
            return (1, str(k))

    @classmethod
    def _apply_grouped_result_options(cls, grouped, config):
        """
        Apply optional grouped-chart shaping after aggregation.

        Supported query_config keys:
          - group_order: `value_desc`, `value_asc`, `label_desc`, `label_asc`
          - group_limit / top_n: max number of groups to keep
        """
        if not isinstance(grouped, dict):
            return grouped

        items = list(grouped.items())
        group_order = (config or {}).get('group_order')
        group_limit = (config or {}).get('group_limit', (config or {}).get('top_n'))

        if group_order == 'value_desc':
            items.sort(key=lambda kv: float(kv[1] or 0), reverse=True)
        elif group_order == 'value_asc':
            items.sort(key=lambda kv: float(kv[1] or 0))
        elif group_order == 'label_desc':
            items.sort(key=lambda kv: cls._chart_sort_key(kv[0]), reverse=True)
        elif group_order == 'label_asc':
            items.sort(key=lambda kv: cls._chart_sort_key(kv[0]))

        try:
            if group_limit is not None:
                group_limit = max(1, int(group_limit))
                items = items[:group_limit]
        except (TypeError, ValueError):
            pass

        return {str(k): v for k, v in items}

    def _db_grouped_agg(self, queryset, agg_type, field, group_by, limit):
        """
        DB-side grouped aggregation.

        Returns a label→value dict, sorted chronologically for date labels
        and alphabetically for categorical labels.  Raises on DB error so the
        caller can fall back to the Python path.
        """
        from django.db.models import Count, Sum, Avg, Min, Max, FloatField
        from django.db.models.functions import Cast, TruncDate
        from django.db.models.fields.json import KeyTextTransform

        _SCALAR_FN = {'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}

        # --- label expression ------------------------------------------------
        if group_by in ('created_at', '_created_at', 'created_at_date'):
            qs = queryset.annotate(label=TruncDate('created_at'))
        else:
            qs = queryset.annotate(label=KeyTextTransform(group_by, 'data'))

        # --- value expression ------------------------------------------------
        if agg_type == 'count':
            val_expr = Count('id')
        elif agg_type in _SCALAR_FN and field:
            numeric_expr = Cast(
                KeyTextTransform(field, 'data'),
                output_field=FloatField(),
            )
            val_expr = _SCALAR_FN[agg_type](numeric_expr)
        else:
            val_expr = Count('id')

        rows = (
            qs
            .values('label')
            .annotate(val=val_expr)
            .exclude(label__isnull=True)
            .exclude(label='')
            .order_by('label')[:limit]
        )

        raw = {}
        for row in rows:
            label = row['label']
            if label is None:
                continue
            # Normalise datetime objects → ISO date strings
            if hasattr(label, 'date'):
                label = label.date().isoformat()
            elif hasattr(label, 'isoformat'):
                label = label.isoformat()
            else:
                label = str(label)
                # "2025-11-24T00:00:00" → "2025-11-24"
                if len(label) > 10 and label[10] in ('T', ' ') and label[:4].isdigit():
                    label = label[:10]
            val = row['val']
            raw[label] = round(float(val), 4) if val is not None else 0

        return {k: v for k, v in sorted(raw.items(), key=lambda kv: self._chart_sort_key(kv[0]))}

    def _python_grouped_agg(self, records, agg_type, field, group_by):
        """
        Python-side grouped aggregation (fallback for DB failures).

        `records` is a list of dicts from `.values('data', 'created_at')`.
        Returns a label→value dict sorted via _chart_sort_key.
        """
        from collections import defaultdict

        sums   = defaultdict(float)
        counts = defaultdict(int)

        for rec in records:
            rdata = rec['data'] or {}

            if group_by in ('created_at', '_created_at', 'created_at_date'):
                label = rec['created_at'].date().isoformat()
            elif group_by in rdata:
                raw = rdata[group_by]
                if raw is None or str(raw).strip() in ('', 'None', 'nan'):
                    continue
                raw_str = str(raw)
                if len(raw_str) > 10 and raw_str[10] in ('T', ' ') and raw_str[:4].isdigit():
                    raw_str = raw_str[:10]
                label = raw_str
            else:
                continue

            if agg_type == 'count':
                val = 1.0
            elif field and field in rdata:
                try:
                    val = float(rdata[field])
                except (ValueError, TypeError):
                    continue
            else:
                val = 1.0 if agg_type == 'count' else 0.0

            sums[label]   += val
            counts[label] += 1

        if not sums:
            return {}

        grouped = {k: sums[k] / counts[k] for k in sums} if agg_type == 'avg' else dict(sums)
        return {
            str(k): round(v, 4)
            for k, v in sorted(grouped.items(), key=lambda kv: self._chart_sort_key(kv[0]))
        }

    def _execute_chart_query(self, widget, limit, extra_filters=None):
        """
        Execute chart widget query and return data formatted for the frontend.

        Aggregations are pushed to the DB via values().annotate().  If the DB
        path raises (e.g. unsupported SQLite version, malformed JSON data) the
        method transparently falls back to the Python-side aggregation path and
        logs a warning so the issue can be investigated without breaking users.
        """
        from .models import Record

        config       = widget.query_config or {}
        aggregations = config.get('aggregations', [])

        if not aggregations:
            return {"error": "No aggregations configured"}

        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        if extra_filters:
            queryset = self._apply_filters(queryset, extra_filters)

        # Separate calculated aggs — resolve AFTER base aggs
        base_aggs = [a for a in aggregations if a.get('type') != 'calculated']
        calc_aggs = [a for a in aggregations if a.get('type') == 'calculated']

        # Python-fallback records are fetched lazily — only if a DB path fails.
        _fallback_records = None

        result = {}
        for agg in base_aggs:
            agg_type = agg.get('type', 'count')
            field    = agg.get('field')
            group_by = agg.get('group_by')
            name     = agg.get('name', 'val')

            try:
                if group_by:
                    # Attempt DB-side grouped aggregation
                    try:
                        grouped = self._db_grouped_agg(queryset, agg_type, field, group_by, limit)
                        result[name] = self._apply_grouped_result_options(grouped, config)
                    except Exception as db_exc:
                        logger.warning(
                            f"DB grouped agg failed (group_by='{group_by}', agg='{agg_type}'): "
                            f"{db_exc}. Falling back to Python-side aggregation."
                        )
                        if _fallback_records is None:
                            _fallback_records = list(queryset.values('data', 'created_at')[:limit])
                        if not _fallback_records:
                            result[name] = {}
                        else:
                            grouped = self._python_grouped_agg(
                                _fallback_records, agg_type, field, group_by
                            )
                            result[name] = self._apply_grouped_result_options(grouped, config)
                else:
                    # Scalar aggregation — reuse the metric path helpers
                    if agg_type == 'count':
                        result[name] = queryset.count()
                    elif field and agg_type in ('sum', 'avg', 'min', 'max'):
                        try:
                            val = self._db_scalar_agg(queryset, agg_type, field)
                            result[name] = round(float(val), 4) if val is not None else 0
                        except Exception as db_exc:
                            logger.warning(
                                f"DB scalar agg failed (field='{field}', agg='{agg_type}'): "
                                f"{db_exc}. Falling back to Python-side aggregation."
                            )
                            result[name] = self._python_scalar_agg(queryset, agg_type, field)
                    else:
                        result[name] = 0

            except Exception as exc:
                logger.error(f"Chart aggregation error for '{name}': {exc}", exc_info=True)
                result[name] = {"error": str(exc)}

        # Resolve calculated aggs using scalar results as context
        # (group-by calculated fields are not supported in chart mode)
        if calc_aggs:
            scalar_context = {k: v for k, v in result.items() if isinstance(v, (int, float))}
            result = self._resolve_calculated_aggs(calc_aggs, {**result, **scalar_context}, queryset=queryset)

        return result

    def _execute_histogram_query(self, widget, limit, extra_filters=None):
        config = widget.query_config or {}
        field = config.get('field')
        if not field:
            return {"error": "Histogram requires query_config.field"}

        bins = config.get('bins', 12)
        try:
            bins = max(5, min(int(bins), 40))
        except (TypeError, ValueError):
            bins = 12

        values = self._get_numeric_values(widget, field, limit, extra_filters=extra_filters)
        if not values:
            return {"message": "No numeric data available"}

        kde = self._build_kde(values) if config.get('show_kde', True) else None
        return {
            'field': field,
            'values': values,
            'bins': bins,
            'kde': kde,
        }

    def _execute_box_plot_query(self, widget, limit, extra_filters=None):
        config = widget.query_config or {}
        field = config.get('field')
        if not field:
            return {"error": "Box plot requires query_config.field"}

        values = self._get_numeric_values(widget, field, limit, extra_filters=extra_filters)
        if not values:
            return {"message": "No numeric data available"}

        series = pd.Series(values, dtype='float64')
        q1 = float(series.quantile(0.25))
        median = float(series.median())
        q3 = float(series.quantile(0.75))
        return {
            'field': field,
            'values': values,
            'summary': {
                'min': round(float(series.min()), 4),
                'q1': round(q1, 4),
                'median': round(median, 4),
                'q3': round(q3, 4),
                'max': round(float(series.max()), 4),
            }
        }

    def _execute_scatter_query(self, widget, limit, extra_filters=None):
        from .models import Record

        config = widget.query_config or {}
        x_field = config.get('x_field')
        y_field = config.get('y_field')
        label_field = config.get('label_field')
        if not x_field or not y_field:
            return {"error": "Scatter plot requires query_config.x_field and query_config.y_field"}

        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        if extra_filters:
            queryset = self._apply_filters(queryset, extra_filters)

        points = []
        for record in queryset.values('data')[:limit]:
            payload = record.get('data') or {}
            try:
                x_val = float(payload.get(x_field))
                y_val = float(payload.get(y_field))
            except (TypeError, ValueError):
                continue
            point = {'x': round(x_val, 4), 'y': round(y_val, 4)}
            if label_field and payload.get(label_field) not in (None, ''):
                point['label'] = str(payload.get(label_field))
            points.append(point)

        if not points:
            return {"message": "No numeric data available"}

        return {
            'x_field': x_field,
            'y_field': y_field,
            'points': points,
        }

    def _get_numeric_values(self, widget, field, limit, extra_filters=None):
        from .models import Record

        config = widget.query_config or {}
        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        if extra_filters:
            queryset = self._apply_filters(queryset, extra_filters)

        values = []
        for payload in queryset.values_list('data', flat=True)[:limit]:
            if not payload or field not in payload:
                continue
            try:
                values.append(round(float(payload[field]), 4))
            except (TypeError, ValueError):
                continue
        return values

    def _build_kde(self, values):
        arr = np.array(values, dtype=float)
        if arr.size < 2:
            return None

        std = float(np.std(arr, ddof=1))
        if std == 0:
            return None

        bandwidth = 1.06 * std * (arr.size ** (-1 / 5))
        if bandwidth <= 0:
            return None

        xs = np.linspace(float(arr.min()), float(arr.max()), 60)
        density = np.zeros_like(xs)
        norm = arr.size * bandwidth * np.sqrt(2 * np.pi)

        for value in arr:
            density += np.exp(-0.5 * ((xs - value) / bandwidth) ** 2)
        density = density / norm

        return {
            'x': [round(float(x), 4) for x in xs],
            'y': [round(float(y), 6) for y in density],
        }

    # ------------------------------------------------------------------
    # Calculated field resolution
    # ------------------------------------------------------------------

    def _resolve_calculated_aggs(self, aggs, base_result, queryset=None):
        """
        Process ``type: 'calculated'`` aggregations in *aggs*.

        Two evaluation modes are auto-detected per expression:

        * **Column-level** (expression contains ``sum(``, ``count(``, etc.) —
          fetches up to 5 000 records from *queryset* and calls
          ``CalculatedFieldEvaluator.evaluate_against_records``.  Requires
          *queryset* to be provided.

        * **Context-level** (plain arithmetic referencing other agg names) —
          calls ``CalculatedFieldEvaluator.evaluate_against_context`` with the
          current *base_result* dict as the variable namespace.

        Returns an updated copy of *base_result* with calculated values filled in.
        Only scalar results are supported; group-by calculated aggregations are
        not yet implemented.
        """
        result = dict(base_result)
        _records_cache = None  # lazy-fetched once if needed

        for agg in aggs:
            if agg.get('type') != 'calculated':
                continue
            name = agg.get('name', 'calc')
            expression = agg.get('expression', '')
            try:
                if CalculatedFieldEvaluator._COL_AGG_RE.search(expression) and queryset is not None:
                    # Column-level expression (sum(col), count(col), …) — needs records
                    if _records_cache is None:
                        _records_cache = list(queryset.values_list('data', flat=True)[:5_000])
                    result[name] = CalculatedFieldEvaluator.evaluate_against_records(
                        expression, _records_cache
                    )
                else:
                    # Context-level expression — references other aggregation names
                    result[name] = CalculatedFieldEvaluator.evaluate_against_context(
                        expression, result
                    )
            except Exception as exc:
                logger.warning(f"Calculated agg '{name}' failed: {exc}")
                result[name] = None
        return result


class CalculatedFieldEvaluator:
    """
    Safe arithmetic expression evaluator for calculated metrics.

    Two usage modes
    ───────────────
    1. **Widget-level** (``evaluate_against_context``):
       The expression references *names* of other aggregation results.
       Context = ``{"total_revenue": 1500, "total_users": 30}``
       Expression = ``"total_revenue / total_users"`` → ``50.0``

    2. **Table-level** (``evaluate_against_records``):
       The expression uses aggregation *functions* on column names.
       Expression = ``"sum(revenue) / count(user_id)"``
       The evaluator pre-computes each ``agg(col)`` call, replaces them
       with safe variable names, then delegates to ``simpleeval``.

    Both modes use ``simpleeval`` with no allowed attribute access or
    imports — only arithmetic, comparisons, and a small set of built-ins.
    """

    # Pattern: word_func( column name — may contain spaces )
    _COL_AGG_RE = re.compile(r'\b(sum|count|avg|min|max)\s*\(\s*([^)]+?)\s*\)')

    # Built-in functions allowed in expressions
    _SAFE_FUNCTIONS = {
        'abs': abs,
        'round': round,
    }

    @staticmethod
    def _safe_var(func: str, col: str) -> str:
        """Convert agg(col) to a safe Python identifier."""
        sanitised = re.sub(r'[^a-zA-Z0-9]', '_', col)
        return f'_agg_{func}_{sanitised}'

    @classmethod
    def _precompute_col_aggs(cls, expression: str, records: list) -> tuple:
        """
        Scan *expression* for ``agg(column)`` calls.

        Pre-compute each aggregation from *records* (list of plain dicts).
        Returns ``(safe_expression, names_dict)``.
        """
        names: dict = {}
        for m in cls._COL_AGG_RE.finditer(expression):
            func, col = m.group(1), m.group(2).strip()
            var = cls._safe_var(func, col)
            if var in names:
                continue
            all_present = []  # all non-None values (for count)
            nums = []         # numeric-castable values (for sum/avg/min/max)
            for rec in records:
                v = rec.get(col)
                if v is not None:
                    all_present.append(v)
                    try:
                        nums.append(float(v))
                    except (ValueError, TypeError):
                        pass
            if func == 'sum':
                names[var] = sum(nums)
            elif func == 'count':
                # count(col) = number of rows that have a non-null value for col
                names[var] = float(len(all_present))
            elif func == 'avg':
                names[var] = (sum(nums) / len(nums)) if nums else 0.0
            elif func == 'min':
                names[var] = min(nums) if nums else 0.0
            elif func == 'max':
                names[var] = max(nums) if nums else 0.0

        safe_expr = cls._COL_AGG_RE.sub(
            lambda m: cls._safe_var(m.group(1), m.group(2).strip()),
            expression,
        )
        return safe_expr, names

    @classmethod
    def evaluate_against_context(cls, expression: str, names: dict) -> float:
        """
        Evaluate an arithmetic *expression* with *names* as the variable context.

        Used for widget-level ``type: "calculated"`` aggregations where the
        expression references names of other aggregations in the same widget.

        Raises ``ValueError`` on evaluation errors (unknown name, syntax error,
        feature not available).  Callers should catch and handle gracefully.
        """
        from simpleeval import simple_eval, NameNotDefined, FeatureNotAvailable

        # Filter names to only include numeric values to prevent injection
        safe_names = {
            k: v for k, v in names.items()
            if isinstance(v, (int, float)) and v is not None
        }
        try:
            result = simple_eval(
                expression,
                names=safe_names,
                functions=cls._SAFE_FUNCTIONS,
            )
            if result is None:
                return 0.0
            return round(float(result), 6)
        except ZeroDivisionError:
            return None
        except (NameNotDefined, FeatureNotAvailable) as exc:
            raise ValueError(f"Expression references unknown name: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Expression evaluation error: {exc}") from exc

    @classmethod
    def evaluate_against_records(cls, expression: str, records: list) -> float:
        """
        Evaluate an expression that uses ``agg(column)`` functions against *records*.

        *records* is a list of plain dicts (each dict is one record's data payload).

        Returns a float result.  Raises ``ValueError`` on any evaluation failure.
        """
        from simpleeval import simple_eval, NameNotDefined, FeatureNotAvailable

        safe_expr, names = cls._precompute_col_aggs(expression, records)
        try:
            result = simple_eval(
                safe_expr,
                names=names,
                functions=cls._SAFE_FUNCTIONS,
            )
            if result is None:
                return 0.0
            return round(float(result), 6)
        except ZeroDivisionError:
            return None
        except (NameNotDefined, FeatureNotAvailable) as exc:
            raise ValueError(f"Expression references unknown column/function: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Expression evaluation error: {exc}") from exc

    @classmethod
    def validate_expression(cls, expression: str) -> tuple:
        """
        Validate *expression* syntax without evaluating against real data.

        Returns ``(True, '')`` if valid, ``(False, error_message)`` otherwise.
        Uses a dummy context where every ``agg(col)`` is replaced with 1.0.
        """
        safe_expr, dummy_names = cls._precompute_col_aggs(
            expression, [{'__dummy__': '1'}]
        )
        # Supplement with any remaining bare names by setting them to 1.0
        try:
            from simpleeval import simple_eval, NameNotDefined, FeatureNotAvailable
            # Seed pre-computed agg vars with 1.0 to avoid divide-by-zero in validation
            for k in list(dummy_names.keys()):
                dummy_names[k] = 1.0
            # Collect unknown bare names and retry (up to 10 iterations)
            for _ in range(10):
                try:
                    simple_eval(safe_expr, names=dummy_names, functions=cls._SAFE_FUNCTIONS)
                    break
                except NameNotDefined as nd:
                    dummy_names[str(nd.name)] = 1.0
                except ZeroDivisionError:
                    # Valid expression syntax — just divides by zero with dummy data
                    break
            return True, ''
        except FeatureNotAvailable as exc:
            return False, f'Unsupported operation: {exc}'
        except Exception as exc:
            return False, str(exc)


class CohortAnalysisEngine:
    """
    Compute a cohort retention matrix from DataTable records.

    A *cohort* is the group of distinct users (identified by ``user_field``)
    whose **first** event falls in a given calendar period.  The matrix then
    shows what fraction of those users performed any event in each subsequent
    period (period 0 = cohort origin, period 1 = next period, …).

    query_config schema
    -------------------
    {
      "user_field":        "user_id",           # required — column identifying the user
      "event_date_field":  "created_at",        # optional (default: record created_at)
      "period":            "week",              # "day" | "week" | "month"  (default: week)
      "periods":           8,                  # max number of periods to show (max 52)
      "filters":           []                  # optional pre-filter (same schema as widget filters)
    }

    Response
    --------
    {
      "cohort_labels":  ["2024-W01", "2024-W02", ...],
      "period_labels":  ["Start", "Period 1", "Period 2", ...],
      "matrix": [
        {"absolute": [120, 85, 60, ...], "percentage": [100.0, 70.8, 50.0, ...]},
        ...
      ],
      "total_users": 350
    }
    """

    MAX_RECORDS = 50_000
    _FREQ_MAP   = {'day': 'D', 'week': 'W', 'month': 'M'}

    def execute(self, widget):
        config          = widget.query_config or {}
        user_field      = config.get('user_field')
        date_field      = config.get('event_date_field', 'created_at')
        period          = config.get('period', 'week')
        num_periods     = min(int(config.get('periods', 8)), 52)
        filters         = config.get('filters', [])

        if not user_field:
            return {'error': 'user_field is required in query_config'}

        freq = self._FREQ_MAP.get(period, 'W')

        # ── Fetch records ──────────────────────────────────────────────────
        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if filters:
            queryset = QueryEngine()._apply_filters(queryset, filters)
        rows = list(queryset.values('data', 'created_at')[:self.MAX_RECORDS])
        if not rows:
            return {'cohort_labels': [], 'period_labels': [], 'matrix': [], 'total_users': 0}

        # ── Build DataFrame ────────────────────────────────────────────────
        from django.utils.dateparse import parse_datetime, parse_date
        records = []
        for row in rows:
            data = row.get('data') or {}
            user = data.get(user_field)
            if not user:
                continue
            if date_field == 'created_at' or date_field not in data:
                event_date = row['created_at']
            else:
                raw = data.get(date_field)
                event_date = parse_datetime(str(raw)) or parse_date(str(raw))
            if not event_date:
                continue
            records.append({'user': str(user), 'event_date': pd.Timestamp(event_date)})

        if not records:
            return {'cohort_labels': [], 'period_labels': [], 'matrix': [], 'total_users': 0}

        df = pd.DataFrame(records)
        df['event_period']  = df['event_date'].dt.to_period(freq)

        # Each user's cohort = their first-seen period
        first_seen = df.groupby('user')['event_period'].min().rename('cohort_period')
        df = df.join(first_seen, on='user')

        # Period offset: 0 = cohort period, 1 = next period, …
        def _period_diff(row):
            try:
                diff = row['event_period'] - row['cohort_period']
                return int(diff) if isinstance(diff, int) else int(diff.n)
            except Exception:
                return -1

        df['period_number'] = df.apply(_period_diff, axis=1)
        df = df[df['period_number'].between(0, num_periods - 1)]

        # Distinct users per (cohort, offset)
        cohort_data = (
            df.groupby(['cohort_period', 'period_number'])['user']
            .nunique()
            .reset_index(name='users')
        )

        if cohort_data.empty:
            return {'cohort_labels': [], 'period_labels': [], 'matrix': [], 'total_users': 0}

        matrix_df = cohort_data.pivot(
            index='cohort_period', columns='period_number', values='users'
        ).fillna(0).sort_index()

        # Ensure all period columns exist (0 … num_periods-1)
        all_cols = list(range(num_periods))
        for c in all_cols:
            if c not in matrix_df.columns:
                matrix_df[c] = 0
        matrix_df = matrix_df[all_cols]

        cohort_labels = [str(p) for p in matrix_df.index]
        period_labels = ['Start'] + [f'Period {i}' for i in range(1, num_periods)]

        matrix = []
        for _, row in matrix_df.iterrows():
            base = float(row[0]) if float(row[0]) > 0 else 1.0
            matrix.append({
                'absolute':   [int(row[c]) for c in all_cols],
                'percentage': [round(float(row[c]) / base * 100, 1) for c in all_cols],
            })

        return {
            'cohort_labels': cohort_labels,
            'period_labels': period_labels,
            'matrix':        matrix,
            'total_users':   int(df['user'].nunique()),
        }


class FunnelAnalysisEngine:
    """
    Compute a sequential funnel from DataTable records.

    Each *step* defines a filter set; the engine counts the distinct users
    (or record count when no ``user_field`` is configured) who pass each
    step's filters.  When ``ordered=True`` (default), a user must appear at
    step N before being counted at step N+1.

    query_config schema
    -------------------
    {
      "user_field": "user_id",    # optional — column identifying the user
      "ordered":    true,         # enforce step ordering (default: true)
      "steps": [
        {"name": "Signed Up",  "filters": [{"field": "event", "operator": "eq", "value": "signup"}]},
        {"name": "Activated",  "filters": [{"field": "event", "operator": "eq", "value": "activate"}]},
        {"name": "Purchased",  "filters": [{"field": "event", "operator": "eq", "value": "purchase"}]}
      ]
    }

    Response
    --------
    {
      "steps": [
        {"name": "Signed Up",  "count": 500, "conversion_rate": 100.0, "overall_rate": 100.0, "dropped": 0},
        {"name": "Activated",  "count": 350, "conversion_rate": 70.0,  "overall_rate": 70.0,  "dropped": 150},
        {"name": "Purchased",  "count": 140, "conversion_rate": 40.0,  "overall_rate": 28.0,  "dropped": 210}
      ],
      "total_users": 500
    }
    """

    MAX_RECORDS = 50_000

    def execute(self, widget):
        config     = widget.query_config or {}
        user_field = config.get('user_field')
        steps      = config.get('steps', [])
        ordered    = config.get('ordered', True)

        if not steps:
            return {'error': 'steps is required in query_config'}

        # ── Fetch all records once ─────────────────────────────────────────
        queryset = Record.objects.filter(table=widget.table, is_active=True)
        rows = list(queryset.values('data', 'created_at')[:self.MAX_RECORDS])
        if not rows:
            return {'steps': [{'name': s.get('name', f'Step {i+1}'), 'count': 0,
                                'conversion_rate': 0.0, 'overall_rate': 0.0, 'dropped': 0}
                               for i, s in enumerate(steps)],
                    'total_users': 0}

        # Flatten records into DataFrame
        flat = []
        for row in rows:
            data = row.get('data') or {}
            entry = {str(k): v for k, v in data.items()}
            entry['_created_at'] = row['created_at']
            flat.append(entry)
        df = pd.DataFrame(flat)

        def _apply_filters(df_in, step_filters):
            """Return filtered DataFrame for a single step."""
            mask = pd.Series([True] * len(df_in), index=df_in.index)
            for f in step_filters:
                field = f.get('field')
                op    = f.get('operator', 'eq')
                value = f.get('value')
                if not field or value is None or field not in df_in.columns:
                    continue
                col = df_in[field]
                if op == 'eq':
                    mask &= (col.astype(str) == str(value))
                elif op == 'neq':
                    mask &= (col.astype(str) != str(value))
                elif op == 'contains':
                    mask &= col.astype(str).str.contains(str(value), na=False, case=False)
                elif op in ('gt', 'gte', 'lt', 'lte'):
                    try:
                        numeric_col = pd.to_numeric(col, errors='coerce')
                        fval = float(value)
                        if   op == 'gt':  mask &= (numeric_col >  fval)
                        elif op == 'gte': mask &= (numeric_col >= fval)
                        elif op == 'lt':  mask &= (numeric_col <  fval)
                        elif op == 'lte': mask &= (numeric_col <= fval)
                    except Exception:
                        pass
            return df_in[mask]

        step_results = []
        prev_users   = None   # set of user identifiers from the previous step
        first_count  = None

        for i, step in enumerate(steps):
            step_name     = step.get('name', f'Step {i + 1}')
            step_filters  = step.get('filters', [])

            step_df = _apply_filters(df, step_filters)

            if user_field and user_field in df.columns:
                if ordered and prev_users is not None:
                    step_df = step_df[step_df[user_field].astype(str).isin(prev_users)]
                users  = set(step_df[user_field].astype(str).dropna().tolist())
                count  = len(users)
                prev_users = users
            else:
                count = len(step_df)

            if first_count is None:
                first_count = count

            step_results.append({'name': step_name, 'count': count})

        # Annotate conversion and drop rates
        first_count = first_count or 1
        for i, step in enumerate(step_results):
            prev_count            = step_results[i - 1]['count'] if i > 0 else first_count
            step['conversion_rate'] = round(step['count'] / max(prev_count, 1) * 100, 1)
            step['overall_rate']    = round(step['count'] / max(first_count, 1) * 100, 1)
            step['dropped']         = (step_results[i - 1]['count'] - step['count']) if i > 0 else 0

        return {'steps': step_results, 'total_users': first_count}


class DataImportService:
    """
    Handle importing data from various sources
    """
    
    def parse_file(self, file_obj, file_type=None):
        """
        Parse uploaded file into a DataFrame
        """
        try:
            if file_type == 'csv' or (file_type is None and file_obj.name.endswith('.csv')):
                # Handle potential encoding issues
                try:
                    df = pd.read_csv(file_obj)
                except UnicodeDecodeError:
                    file_obj.seek(0)
                    df = pd.read_csv(file_obj, encoding='latin1')
            elif file_type in ['xlsx', 'xls', 'excel'] or (file_type is None and file_obj.name.endswith(('.xlsx', '.xls'))):
                df = pd.read_excel(file_obj)
            else:
                raise ValueError(f"Unsupported file format for: {file_obj.name}")

            # Basic cleanup: remove completely empty rows/cols
            df = df.dropna(how='all').dropna(axis=1, how='all')

            # Replace NaN with None for JSON compatibility
            df = df.replace({np.nan: None})

            return df
        except Exception as e:
            raise ValueError(f"Error parsing file: {str(e)}")

    def import_data(self, table, df, user, mapping=None,
                    import_mode='append', primary_key_field=''):
        """
        Import data from a DataFrame into a table.

        Args:
            import_mode: ``'append'`` (default) | ``'replace'`` | ``'upsert'``
                - append:  insert new rows; existing data untouched
                - replace: soft-delete all existing active rows, then insert fresh rows
                - upsert:  for each row, update the first matching active record keyed on
                           ``primary_key_field``; insert if no match found
            primary_key_field: field name used as the unique key in ``upsert`` mode

        Row-by-row Record.save() triggered a COUNT + UPDATE per row (O(n²) on large files);
        bulk_create collapses that to a single INSERT batch + one count refresh at the end.
        Upsert mode necessarily falls back to row-by-row to handle update vs insert branching.
        """
        from django.db import transaction
        from .models import Record

        # Auto-detect schema if not provided and table has no schema
        if not table.schema and not mapping:
            table.schema = self._detect_schema_from_df(df)
            table.save()

        # Replace NaN with None for JSON compatibility
        df = df.replace({np.nan: None})
        records_data = df.to_dict('records')

        success_count = 0
        updated_count = 0
        error_count = 0
        errors = []
        BATCH_SIZE = getattr(settings, 'IMPORT_BATCH_SIZE', 200)

        with transaction.atomic():
            # Replace mode: soft-delete all existing active records first
            if import_mode == 'replace':
                Record.objects.filter(table=table, is_active=True).update(is_active=False)

            if import_mode == 'upsert' and primary_key_field:
                # Row-by-row to support update vs insert branching
                for row_num, row in enumerate(records_data, start=1):
                    try:
                        if mapping:
                            row = {target: row[src] for target, src in mapping.items() if src in row}
                        row = self._sanitize_row(dict(row))
                        pk_val = row.get(primary_key_field)
                        if pk_val is not None:
                            existing = Record.objects.filter(
                                table=table,
                                is_active=True,
                                **{f'data__{primary_key_field}': pk_val},
                            ).first()
                            if existing:
                                existing.data = row
                                existing.save(update_fields=['data'])
                                updated_count += 1
                                continue
                        Record.objects.create(table=table, data=row, created_by=user)
                        success_count += 1
                    except Exception as exc:
                        error_count += 1
                        errors.append(f'Row {row_num}: {exc}')
            else:
                # Append / replace → bulk-insert path
                pending = []
                for row_num, row in enumerate(records_data, start=1):
                    try:
                        if mapping:
                            row = {target: row[src] for target, src in mapping.items() if src in row}
                        mapped_row = self._sanitize_row(dict(row))
                        pending.append(Record(table=table, data=mapped_row, created_by=user))
                    except Exception as e:
                        error_count += 1
                        errors.append(f'Row {row_num}: {e}')

                for i in range(0, len(pending), BATCH_SIZE):
                    batch = pending[i:i + BATCH_SIZE]
                    try:
                        Record.objects.bulk_create(batch)
                        success_count += len(batch)
                    except Exception:
                        # Batch-level failure: fall back row-by-row to isolate bad rows
                        for rec in batch:
                            try:
                                rec.save()
                                success_count += 1
                            except Exception as row_exc:
                                error_count += 1
                                errors.append(str(row_exc))

            # Update record_count + updated_at exactly once after all writes.
            # updated_at changing invalidates widget cache for this table automatically.
            table.record_count = table.records.filter(is_active=True).count()
            table.save(update_fields=['record_count', 'updated_at'])

        return {
            'success': success_count,
            'updated': updated_count,
            'errors': error_count,
            'error_details': errors[:10],
        }

    def _sanitize_row(self, row):
        """Convert a row dict to contain only JSON-serializable Python native types."""
        clean = {}
        for k, v in row.items():
            if v is None:
                clean[k] = None
            elif hasattr(v, 'isoformat'):  # datetime, date, pd.Timestamp
                clean[k] = v.isoformat()
            elif hasattr(v, 'item'):  # numpy scalar (int64, float64, bool_, etc.)
                item = v.item()
                clean[k] = None if (isinstance(item, float) and (item != item)) else item
            elif isinstance(v, float) and v != v:  # plain float NaN
                clean[k] = None
            else:
                clean[k] = v
        return clean

    def clean_dataframe(self, df):
        """
        Clean a DataFrame before import and return (cleaned_df, report).
        Report is a list of human-readable strings describing what was changed.
        """
        import re
        report = []
        original_rows = len(df)

        # 1. Strip whitespace from column names
        new_cols = [str(c).strip() for c in df.columns]
        renamed = {old: new for old, new in zip(df.columns, new_cols) if old != new}
        if renamed:
            df = df.rename(columns=renamed)
            report.append(f"Trimmed whitespace from {len(renamed)} column name(s): {', '.join(renamed.values())}")

        # 2. Strip whitespace from all string values
        str_cols = df.select_dtypes(include='object').columns.tolist()
        for col in str_cols:
            df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)
            # Normalise empty strings to None
            df[col] = df[col].replace('', None)

        if str_cols:
            report.append(f"Stripped whitespace and normalised empty strings in {len(str_cols)} text column(s)")

        # 3. Remove completely duplicate rows
        dupes = df.duplicated().sum()
        if dupes:
            df = df.drop_duplicates()
            report.append(f"Removed {dupes} duplicate row(s)")

        # 4. Clean numeric-looking columns that contain currency symbols or thousands separators
        for col in str_cols:
            sample = df[col].dropna().head(20)
            numeric_looking = sample.map(
                lambda v: bool(re.match(r'^[\$£€\s]?[\d,]+\.?\d*[\s%]?$', str(v)))
            ).sum()
            if numeric_looking >= len(sample) * 0.8 and len(sample) > 0:
                cleaned = df[col].map(
                    lambda v: re.sub(r'[^\d.\-]', '', str(v)) if pd.notna(v) else v
                )
                try:
                    df[col] = pd.to_numeric(cleaned, errors='raise')
                    report.append(f"Converted '{col}' to numeric (removed currency/separator characters)")
                except (ValueError, TypeError):
                    pass  # leave as-is if conversion fails

        # 5. Attempt to parse date-like string columns — store as ISO strings.
        #    Try an explicit priority list of formats to avoid the mm/dd vs dd/mm
        #    ambiguity that comes with pandas' infer_datetime_format heuristic.
        #    Priority order: unambiguous ISO first, then day-first (KE/EU), then
        #    month-first (US).  The format that parses the most rows without NaT wins.
        _DATE_FORMATS = [
            # ISO / unambiguous (year always 4 digits)
            '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
            '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
            # Day-first — common in Kenya / Europe
            '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y',
            '%d-%m-%y', '%d/%m/%y',
            # Long written forms
            '%d %B %Y', '%d-%b-%Y', '%d %b %Y',
            # Month-first — US style (lowest priority)
            '%m-%d-%Y', '%m/%d/%Y',
            '%m-%d-%y', '%m/%d/%y',
        ]

        for col in df.select_dtypes(include='object').columns:
            sample = df[col].dropna().head(30)
            if len(sample) == 0:
                continue

            best_fmt   = None
            best_score = -1

            for fmt in _DATE_FORMATS:
                try:
                    trial = pd.to_datetime(sample, format=fmt, errors='coerce')
                    score = trial.notna().sum()
                    if score > best_score:
                        best_score = score
                        best_fmt   = fmt
                except Exception:
                    continue

            # Require at least 70 % of the sample rows to parse cleanly
            if best_fmt is None or best_score < len(sample) * 0.7:
                continue

            parsed = pd.to_datetime(df[col], format=best_fmt, errors='coerce')
            nat_new = parsed.isna().sum() - df[col].isna().sum()
            if nat_new / max(len(df), 1) >= 0.2:
                continue  # too many failures on full column — leave as-is

            df[col] = parsed.dt.strftime('%Y-%m-%d').where(parsed.notna(), None)
            report.append(
                f"Standardised '{col}' to ISO date format (YYYY-MM-DD) "
                f"using format '{best_fmt}'"
            )

        # 5b. Normalise datetime64 columns that pandas already parsed (e.g. Excel
        #     native date cells).  These never reach step 5 because their dtype is
        #     datetime64, not object.  Store as plain YYYY-MM-DD strings so every
        #     date value in the JSON is consistent.
        for col in df.select_dtypes(include='datetime64').columns:
            df[col] = df[col].dt.strftime('%Y-%m-%d').where(df[col].notna(), None)
            report.append(f"Normalised datetime column '{col}' to ISO date format (YYYY-MM-DD)")

        # 6. Report rows with all-None values (already dropped by parse_file, but re-check after cleaning)
        all_null = df.isnull().all(axis=1).sum()
        if all_null:
            df = df[~df.isnull().all(axis=1)]
            report.append(f"Removed {all_null} fully empty row(s)")

        rows_removed = original_rows - len(df)
        if rows_removed and not any('duplicate' in r or 'empty' in r for r in report):
            report.append(f"Removed {rows_removed} row(s) during cleaning")

        # 7. Normalise boolean-like text columns → Python True/False
        #    Require ≥85 % of non-null values to match known boolean words.
        BOOL_TRUE  = {'yes', 'y', 'true', 't', '1', 'on', 'active', 'enabled',
                      'correct', 'valid', 'present', 'pass'}
        BOOL_FALSE = {'no', 'n', 'false', 'f', '0', 'off', 'inactive', 'disabled',
                      'incorrect', 'invalid', 'absent', 'fail'}
        ALL_BOOL   = BOOL_TRUE | BOOL_FALSE
        bool_cols  = []
        for col in df.select_dtypes(include='object').columns:
            samp = df[col].dropna()
            if len(samp) == 0:
                continue
            lower = samp.map(lambda x: str(x).strip().lower())
            if lower.isin(ALL_BOOL).sum() / len(samp) >= 0.85:
                df[col] = df[col].map(
                    lambda x: (True  if str(x).strip().lower() in BOOL_TRUE  else
                               False if str(x).strip().lower() in BOOL_FALSE else None)
                    if pd.notna(x) else None
                )
                bool_cols.append(col)
        if bool_cols:
            report.append(
                f"Normalised {len(bool_cols)} boolean column(s) to True/False: "
                f"{', '.join(bool_cols)}"
            )

        # 8. Strip percentage signs and convert to numeric
        #    e.g. "45.3 %" or "12%" → 45.3, 12.0
        pct_cols = []
        for col in df.select_dtypes(include='object').columns:
            samp = df[col].dropna().head(20)
            if len(samp) == 0:
                continue
            pct_like = samp.map(
                lambda x: bool(re.match(r'^-?\s*\d+\.?\d*\s*%$', str(x).strip()))
            ).sum()
            if pct_like / len(samp) >= 0.75:
                cleaned = df[col].map(
                    lambda x: float(re.sub(r'[^\d.\-]', '', str(x).strip()))
                    if pd.notna(x) and str(x).strip() else None
                )
                try:
                    df[col] = pd.to_numeric(cleaned, errors='raise')
                    pct_cols.append(col)
                except (ValueError, TypeError):
                    pass
        if pct_cols:
            report.append(
                f"Converted {len(pct_cols)} percentage column(s) to numeric (stripped %): "
                f"{', '.join(pct_cols)}"
            )

        # 9. Standardise categorical text to Title Case
        #    Only applies to low-cardinality text columns (≤30 unique, ≤30 % cardinality ratio)
        #    to avoid mangling free-text names / descriptions.
        titled_cols = []
        for col in df.select_dtypes(include='object').columns:
            n_non_null = df[col].notna().sum()
            if n_non_null == 0:
                continue
            n_unique = df[col].nunique(dropna=True)
            if n_unique > 30 or n_unique / max(n_non_null, 1) > 0.3:
                continue
            df[col] = df[col].map(lambda x: x.title() if isinstance(x, str) else x)
            titled_cols.append(col)
        if titled_cols:
            report.append(
                f"Standardised {len(titled_cols)} categorical column(s) to Title Case "
                f"for consistent grouping: {', '.join(titled_cols)}"
            )

        # 10. Data-quality flag: columns with >30 % missing values
        #     (informational — data is kept, user is informed)
        low_fill = []
        for col in df.columns:
            null_pct = df[col].isna().mean()
            if null_pct >= 0.30:
                low_fill.append(f"'{col}' ({null_pct:.0%} missing)")
        if low_fill:
            report.append(
                f"High missing-value rate detected — consider reviewing: "
                f"{'; '.join(low_fill)}"
            )

        if not report:
            report.append("No issues found — data looks clean")

        return df, report

    def import_csv(self, table, file_obj, user, mapping=None):
        """
        Legacy support for import_csv
        """
        df = self.parse_file(file_obj, 'csv')
        return self.import_data(table, df, user, mapping)
    
    def _detect_schema_from_df(self, df):
        """
        Detect field types using majority-vote sampling over up to 50 non-null values
        per column, supporting text, number, date, datetime, boolean, email, url,
        phone, percentage, and currency types.
        """
        import re as _re
        schema    = []
        _EMAIL    = _re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
        _URL      = _re.compile(r'^https?://', _re.I)
        _PHONE    = _re.compile(r'^\+?[\d][\d\s\-\(\)\.]{6,17}$')
        _PCT      = _re.compile(r'^-?\s*\d+\.?\d*\s*%$')
        # Currency-prefixed or thousands-separated numeric values
        _NUMERIC  = _re.compile(r'^[\$£€KES\s]*-?[\d,]+\.?\d*$')
        _BOOL_SET = {'yes', 'no', 'true', 'false', 'y', 'n', '1', '0',
                     'on', 'off', 'active', 'inactive', 'enabled', 'disabled',
                     'pass', 'fail', 'correct', 'incorrect', 'present', 'absent'}

        for column in df.columns:
            non_null = df[column].dropna()

            # Fast path: unambiguous pandas dtype
            if pd.api.types.is_bool_dtype(df[column]):
                field_type = 'boolean'
            elif pd.api.types.is_datetime64_any_dtype(df[column]):
                field_type = 'datetime'
            elif pd.api.types.is_integer_dtype(df[column]):
                field_type = 'number'
            elif pd.api.types.is_float_dtype(df[column]):
                field_type = 'number'
            elif pd.api.types.is_numeric_dtype(df[column]):
                field_type = 'number'
            elif non_null.empty:
                field_type = 'text'
            else:
                # Sample-based majority vote (up to 50 values)
                sample = non_null.head(50).astype(str).str.strip()
                n = max(len(sample), 1)
                votes = {t: 0 for t in (
                    'email', 'url', 'boolean', 'percentage',
                    'phone', 'number', 'date', 'text'
                )}

                for val in sample:
                    if not val or val in ('None', 'nan', ''):
                        continue
                    vl = val.lower()
                    if _EMAIL.match(val):
                        votes['email'] += 1
                    elif _URL.match(val):
                        votes['url'] += 1
                    elif vl in _BOOL_SET:
                        votes['boolean'] += 1
                    elif _PCT.match(val):
                        votes['percentage'] += 1
                    elif self._is_date(val):
                        # Date before phone: ISO dates like 2024-01-15 look
                        # like phone numbers to the PHONE regex.
                        votes['date'] += 1
                    elif _PHONE.match(val):
                        votes['phone'] += 1
                    else:
                        # Try numeric (strip known currency/separator chars)
                        stripped = _re.sub(r'[\$£€,\s]', '', val)
                        try:
                            float(stripped)
                            votes['number'] += 1
                            continue
                        except ValueError:
                            pass
                        votes['text'] += 1

                best_type  = max(votes, key=votes.get)
                best_score = votes[best_type]
                # Require >55 % agreement for a non-text type
                field_type = best_type if best_score / n >= 0.55 else 'text'

                # 'percentage' is supported in DataTable.FIELD_TYPES; others fall back to text
                allowed = {'text', 'number', 'date', 'datetime', 'boolean',
                           'email', 'url', 'phone', 'currency', 'percentage'}
                if field_type not in allowed:
                    field_type = 'text'

            schema.append({'name': str(column), 'type': field_type, 'required': False})

        return schema
    
    def _is_date(self, value):
        """Return True if *value* can be parsed as an ISO date string."""
        from django.utils.dateparse import parse_date
        try:
            return parse_date(str(value)) is not None
        except (ValueError, TypeError, AttributeError):
            return False


class WorkspaceInsightService:
    """
    Automatically generate BI insights from workspace tables.

    Insight generation is purely query-config-driven: every widget spec
    contains a `query_config` that QueryEngine can execute at render time.
    No hardcoded data is embedded in widget specs.
    """

    # Field-type buckets used throughout
    NUMERIC_TYPES = {'number', 'currency', 'percentage', 'integer', 'float', 'decimal'}
    DATE_TYPES    = {'date', 'datetime'}
    TEXT_TYPES    = {'text', 'string', 'category', 'email', 'url'}

    # KPI card visual palette (cycles for multiple numeric fields)
    _KPI_COLORS = ['blue',   'green',  'orange', 'purple']
    _KPI_ICONS  = ['fa-database', 'fa-chart-bar', 'fa-coins', 'fa-percent']

    # ------------------------------------------------------------------ #
    #  Public entry point
    # ------------------------------------------------------------------ #
    def generate_workspace_overview(self, workspace, user):
        from .models import Dashboard, Widget

        logger.info(f"Generating workspace insights for workspace {workspace.id}")

        dashboard, created = Dashboard.objects.get_or_create(
            workspace=workspace,
            slug='workspace-overview',
            defaults={
                'name': 'Workspace Insights',
                'description': 'Auto-generated insights from all your data',
                'created_by': user,
                'layout_config': {"columns": 12, "rowHeight": 100, "compact": True},
            }
        )

        if not created:
            # Regenerate: wipe previously auto-generated widgets.
            # Suppress the empty-dashboard signal so the dashboard itself isn't deleted.
            from .models import _skip_dashboard_cleanup
            _skip_dashboard_cleanup.active = True
            try:
                dashboard.widgets.filter(title__startswith='[Auto]').delete()  # type: ignore[attr-defined]
            finally:
                _skip_dashboard_cleanup.active = False

        from django.db.models import Exists, OuterRef
        _has_records = Record.objects.filter(table=OuterRef('pk'), is_active=True)
        tables = (
            workspace.tables
            .filter(is_active=True)
            .annotate(_has_data=Exists(_has_records))
            .filter(_has_data=True)
        )
        current_y   = 0
        total_count = 0

        for table in tables:
            logger.info(f"  Profiling table: {table.name} ({table.record_count} records)")

            insights = self._generate_table_insights(table)
            if not insights:
                continue

            # ── Place KPI cards in their own row(s) ────────────────────
            kpis   = [i for i in insights if i['type'] == 'metric']
            charts = [i for i in insights if i['type'] not in ('metric',)]

            kpi_row_h = 2  # all KPIs are h=2
            kpi_x, kpi_y = 0, current_y
            for spec in kpis:
                w = spec['width']
                if kpi_x + w > 12:
                    kpi_x  = 0
                    kpi_y += kpi_row_h
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type=spec['type'],
                    title=f"[Auto] {spec['title']}",
                    table=table,
                    query_config=spec['query_config'],
                    viz_config=spec.get('viz_config', {}),
                    position={'x': kpi_x, 'y': kpi_y, 'w': w, 'h': kpi_row_h},
                )
                kpi_x    += w
                total_count += 1

            # Charts start below the last KPI row
            chart_start_y = (kpi_y + kpi_row_h) if kpis else current_y
            chart_x, chart_y = 0, chart_start_y

            for spec in charts:
                w, h = spec['width'], spec['height']
                if chart_x + w > 12:
                    chart_x  = 0
                    chart_y += h
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type=spec['type'],
                    title=f"[Auto] {spec['title']}",
                    table=table,
                    query_config=spec['query_config'],
                    viz_config=spec.get('viz_config', {}),
                    position={'x': chart_x, 'y': chart_y, 'w': w, 'h': h},
                )
                chart_x     += w
                total_count += 1

            # ── Data table preview at full width ───────────────────────
            last_chart_h = charts[-1]['height'] if charts else 0
            table_y = (chart_y + last_chart_h) if charts else chart_start_y
            Widget.objects.create(
                dashboard=dashboard,
                widget_type='table',
                title=f"[Auto] {table.name} Records",
                table=table,
                query_config={"limit": 20},
                viz_config={},
                position={'x': 0, 'y': table_y, 'w': 12, 'h': 5},
            )
            total_count += 1

            # Leave a 1-unit gap before the next table's section
            current_y = table_y + 5 + 1

        logger.info(f"Generated {total_count} widgets from {tables.count()} internal tables")

        # ── External DataSources (Google Sheets, PostgreSQL, MySQL) ────────
        from .models import DataSource
        connected_sources = DataSource.objects.filter(
            workspace=workspace,
            is_active=True,
            last_test_ok=True,
        )
        for ds in connected_sources:
            logger.info("  Processing DataSource: %s (%s)", ds.name, ds.connector_type)
            try:
                if ds.connector_type == 'google_sheets':
                    engine = GoogleSheetsQueryEngine(ds)
                else:
                    engine = DataSourceQueryEngine(ds)
                sheet_list = engine.list_tables()
            except Exception as exc:
                logger.warning("  Could not list tables for %s: %s", ds.name, exc)
                continue

            for tbl_info in sheet_list:
                sheet_name = tbl_info['name']
                logger.info("    Sheet/table: %s", sheet_name)

                insights = self._generate_datasource_sheet_insights(ds, sheet_name)
                if not insights:
                    # At minimum create a table-preview widget
                    Widget.objects.create(
                        dashboard=dashboard,
                        widget_type='table',
                        title=f"[Auto] {ds.name} — {sheet_name}",
                        table=None,
                        data_source=ds,
                        source_table_name=sheet_name,
                        query_config={"limit": 20},
                        viz_config={},
                        position={'x': 0, 'y': current_y, 'w': 12, 'h': 5},
                    )
                    current_y += 6
                    total_count += 1
                    continue

                kpis   = [i for i in insights if i['type'] == 'metric']
                charts = [i for i in insights if i['type'] not in ('metric',)]

                kpi_row_h = 2
                kpi_x, kpi_y = 0, current_y
                for spec in kpis:
                    w = spec['width']
                    if kpi_x + w > 12:
                        kpi_x  = 0
                        kpi_y += kpi_row_h
                    Widget.objects.create(
                        dashboard=dashboard,
                        widget_type=spec['type'],
                        title=f"[Auto] {spec['title']}",
                        table=None,
                        data_source=ds,
                        source_table_name=sheet_name,
                        query_config=spec['query_config'],
                        viz_config=spec.get('viz_config', {}),
                        position={'x': kpi_x, 'y': kpi_y, 'w': w, 'h': kpi_row_h},
                    )
                    kpi_x += w
                    total_count += 1

                chart_start_y = (kpi_y + kpi_row_h) if kpis else current_y
                chart_x, chart_y = 0, chart_start_y
                for spec in charts:
                    w, h = spec['width'], spec['height']
                    if chart_x + w > 12:
                        chart_x  = 0
                        chart_y += h
                    Widget.objects.create(
                        dashboard=dashboard,
                        widget_type=spec['type'],
                        title=f"[Auto] {spec['title']}",
                        table=None,
                        data_source=ds,
                        source_table_name=sheet_name,
                        query_config=spec['query_config'],
                        viz_config=spec.get('viz_config', {}),
                        position={'x': chart_x, 'y': chart_y, 'w': w, 'h': h},
                    )
                    chart_x += w
                    total_count += 1

                last_chart_h = charts[-1]['height'] if charts else 0
                table_y = (chart_y + last_chart_h) if charts else chart_start_y
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='table',
                    title=f"[Auto] {ds.name} — {sheet_name} Preview",
                    table=None,
                    data_source=ds,
                    source_table_name=sheet_name,
                    query_config={"limit": 20},
                    viz_config={},
                    position={'x': 0, 'y': table_y, 'w': 12, 'h': 5},
                )
                total_count += 1
                current_y = table_y + 5 + 1

        logger.info("Generated %d widgets total", total_count)
        return dashboard

    # ------------------------------------------------------------------ #
    #  Data profiling + insight spec generation
    # ------------------------------------------------------------------ #
    def _generate_table_insights(self, table):
        """Profile an internal DataTable and return insight specs."""
        from .models import Record

        schema = {f['name']: f['type'] for f in (table.schema or [])}
        if not schema:
            return []

        profile_sample = getattr(settings, 'QUERY_ENGINE_PROFILE_SAMPLE', 1000)
        sample = list(
            Record.objects.filter(table=table, is_active=True)
            .values_list('data', flat=True)[:profile_sample]
        )
        if not sample:
            return []
        return self._profile_and_generate(schema, sample)

    def _generate_datasource_sheet_insights(self, data_source, sheet_name):
        """
        Profile one sheet / table from an external DataSource and return
        the same insight-spec list that _generate_table_insights returns.
        Widgets produced by the caller must set data_source + source_table_name.
        """
        import pandas as pd
        try:
            if data_source.connector_type == 'google_sheets':
                engine = GoogleSheetsQueryEngine(data_source)
                df = engine._sheet_to_df(sheet_name, max_rows=500)
            else:
                engine = DataSourceQueryEngine(data_source)
                df = engine.fetch_sample(sheet_name, max_rows=500)
        except Exception as exc:
            logger.warning(
                "Could not read %s / %s for insight generation: %s",
                data_source.name, sheet_name, exc,
            )
            return []

        if df.empty:
            return []

        # ── Preprocess: coerce currency strings and date strings so the
        #    profiler can detect numeric and date columns correctly.
        #    (This copy is only used for profiling — widget queries hit the
        #     live source independently.)
        df_prof = df.copy()
        _currency_re = r'[\$£€¥₹,\s]'
        for col in df_prof.columns:
            raw = df_prof[col].astype(str).str.strip()
            # Try currency / numeric: strip symbols then coerce
            stripped = raw.str.replace(_currency_re, '', regex=True)
            numeric  = pd.to_numeric(stripped, errors='coerce')
            if numeric.notna().mean() >= 0.7:
                df_prof[col] = numeric
                continue
            # Try date
            try:
                df_prof[col] = pd.to_datetime(raw, infer_datetime_format=True, errors='raise')
            except (ValueError, TypeError):
                pass   # leave as text

        # Build schema from inferred column types on the preprocessed data
        schema = {
            col: GoogleSheetsQueryEngine._infer_column_type(df_prof[col])
            for col in df_prof.columns
        }
        sample = df_prof.where(df_prof.notna(), None).to_dict('records')
        return self._profile_and_generate(schema, sample)

    def _profile_and_generate(self, schema, sample):

        # ── Step 1: Detect domain ─────────────────────────────────────────
        domain      = self._detect_data_domain(schema, sample)
        record_noun = self._domain_record_noun(domain)

        # ── Step 2: Classify schema fields ───────────────────────────────
        numeric_fields = [n for n, t in schema.items() if t in self.NUMERIC_TYPES]
        date_fields    = [n for n, t in schema.items() if t in self.DATE_TYPES]
        text_fields    = [n for n, t in schema.items() if t in self.TEXT_TYPES]

        # Keep only numeric fields that actually contain numbers
        valid_numeric = self._filter_numeric_fields(numeric_fields, sample)
        valid_numeric = sorted(
            valid_numeric,
            key=lambda f: self._score_field_for_kpi(f, schema.get(f, 'number')),
            reverse=True,
        )

        # Detect percentage/rate fields — better as gauge than sum KPI
        pct_field_names = {'percent', '%', 'rate', 'ratio', 'score',
                           'satisfaction', 'nps', 'csat', 'gpa', 'grade'}
        pct_fields = [
            f for f in valid_numeric
            if any(k in self._normalize_name(f) for k in pct_field_names)
        ]

        # Demote low-signal numeric fields to categorical chart dimensions
        _pending_demotions = []
        kept_numeric = []
        for f in valid_numeric:
            kpi_s = self._score_field_for_kpi(f, schema.get(f, 'number'))
            if kpi_s <= -5:
                n_unique = len(self._unique_values(f, sample))
                if self._score_field_for_chart(f) > 0 and n_unique <= 30:
                    _pending_demotions.append((f, n_unique))
            else:
                kept_numeric.append(f)
        valid_numeric = kept_numeric

        primary_value = valid_numeric[0] if valid_numeric else None
        display_pv    = self._clean_display_name(primary_value) if primary_value else ''
        prefix_pv     = (self._currency_prefix(primary_value, schema.get(primary_value))
                         if primary_value else '')

        # ── Step 3: Classify categorical fields ───────────────────────────
        very_low_card, low_card = [], []
        for field in text_fields:
            chart_score = self._score_field_for_chart(field)
            if chart_score <= -4:       # ID / free-text / contact field — skip
                continue
            unique = self._unique_values(field, sample)
            n = len(unique)
            if 2 <= n <= 8:
                very_low_card.append((field, n))
            elif n <= 25:
                low_card.append(field)

        # Merge demoted numeric fields
        for f, n in _pending_demotions:
            if 2 <= n <= 8:
                very_low_card.append((f, n))
            elif n <= 25:
                low_card.append(f)

        very_low_card_for_bar = sorted(
            [f for f, _ in very_low_card],
            key=self._score_field_for_chart, reverse=True
        )
        very_low_card_for_pie = sorted(
            very_low_card,
            key=lambda x: self._score_field_for_chart(x[0]) * 2 - x[1],
            reverse=True
        )
        very_low_card_for_pie = [f for f, _ in very_low_card_for_pie]
        low_card = sorted(low_card, key=self._score_field_for_chart, reverse=True)
        all_categorical = very_low_card_for_bar + low_card

        # ── Step 4: Date dimension ────────────────────────────────────────
        primary_date = date_fields[0] if date_fields else None
        group_by     = primary_date if primary_date else 'created_at_date'
        date_label   = self._clean_display_name(primary_date) if primary_date else 'Date'
        has_dates    = bool(date_fields) #or True   # created_at is always available

        insights = []
        color_i  = 0

        # 👇 ADD THIS RIGHT HERE
        date_ok = False
        if primary_date:
            date_ok = self._has_meaningful_date_variation(primary_date, sample)
        elif len(sample) > 1:
            date_ok = True

        # ════════════════════════════════════════════════════════════════
        #  KPI CARDS (up to 4, always width=3 so they fill one row)
        # ════════════════════════════════════════════════════════════════

        # Card 1 — Total record count, domain-labelled
        count_label = self._domain_count_label(domain)
        count_desc  = (f"Total number of {record_noun} captured in this dataset. "
                       f"Use this as your baseline volume metric.")
        insights.append(self._kpi_spec(
            count_label, 'count', None, color_i, description=count_desc
        ))
        color_i += 1

        # Card 2 — Sum of primary value field

        if date_ok:
                    
            if primary_value:
                sum_title = self._agg_title('sum', display_pv)
                sum_desc  = (f"Grand total of {display_pv.lower()} across all {record_noun}. "
                            f"This is your headline performance number.")
                insights.append(self._kpi_spec(
                    sum_title, 'sum', primary_value, color_i,
                    prefix=prefix_pv, description=sum_desc
                ))
                color_i += 1

                # Card 3 — Average of primary value field
            if primary_value:
                avg_title = self._agg_title('avg', display_pv)
                avg_desc  = (f"Average {display_pv.lower()} per {record_noun[:-1] if record_noun.endswith('s') else record_noun}. "
                            f"Benchmark this against targets to spot under- or over-performance.")
                insights.append(self._kpi_spec(
                    avg_title, 'avg', primary_value, color_i,
                    prefix=prefix_pv, description=avg_desc
                ))
                color_i += 1



        # Card 4 — Second meaningful numeric (skip semantically redundant fields)
        primary_display_lower = display_pv.lower() if primary_value else ''
        secondary_fields = [
            f for f in valid_numeric[1:]
            if self._score_field_for_kpi(f, schema.get(f, 'number')) >= 0
            and f != primary_value
            and self._clean_display_name(f).lower() not in (primary_display_lower, 'revenue', 'total')
            and primary_display_lower not in self._clean_display_name(f).lower()
            and f not in pct_fields     # percentage fields get a separate treatment
        ]
        if secondary_fields:
            sec         = secondary_fields[0]
            display_sec = self._clean_display_name(sec)
            prefix_sec  = self._currency_prefix(sec, schema.get(sec))
            sec_desc    = (f"Total {display_sec.lower()} — track alongside "
                           f"{display_pv.lower() or 'the primary metric'} for a "
                           f"complete picture of performance.")
            insights.append(self._kpi_spec(
                self._agg_title('sum', display_sec), 'sum', sec,
                color_i, prefix=prefix_sec, description=sec_desc
            ))
            color_i += 1

        # ════════════════════════════════════════════════════════════════
        #  TREND CHARTS — value + volume over time
        # ════════════════════════════════════════════════════════════════
        if primary_value:
            insights.append({
                'type': 'line_chart',
                'title': f'{display_pv} Over Time',
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [
                    {'type': 'sum', 'field': primary_value,
                     'group_by': group_by, 'name': 'val'}
                ]},
                'viz_config': {
                    'x_axis': date_label, 'y_axis': display_pv,
                    'description': (
                        f"How {display_pv.lower()} changes over time. "
                        f"Look for growth trends, seasonal peaks, and sudden dips "
                        f"that may signal issues or opportunities."
                    ),
                },
            })

        vol_title = f'{self._domain_count_label(domain)} Over Time'
        insights.append({
            'type': 'line_chart',
            'title': vol_title,
            'width': 6, 'height': 4,
            'query_config': {'aggregations': [
                {'type': 'count', 'group_by': group_by, 'name': 'val'}
            ]},
            'viz_config': {
                'x_axis': date_label, 'y_axis': 'Volume',
                'description': (
                    f"Volume of {record_noun} over time. "
                    f"Spikes indicate busy periods; flat lines may reveal inactivity. "
                    f"Compare against the value trend above to spot efficiency gaps."
                ),
            },
        })

        donut_field = self._pick_focus_donut_field(all_categorical, domain)

        # ════════════════════════════════════════════════════════════════
        #  BAR CHARTS — categorical breakdowns (up to 4)
        # ════════════════════════════════════════════════════════════════
        bar_count = 0
        for field in all_categorical[:6]:
            if bar_count >= 4:
                break
            display_field = self._clean_display_name(field)

            if primary_value:
                agg   = {'type': 'sum', 'field': primary_value,
                         'group_by': field, 'name': 'val'}
                title = f'{display_pv} by {display_field}'
                y_lbl = display_pv
                desc  = (
                    f"Which {display_field.lower()} drives the most {display_pv.lower()}? "
                    f"Taller bars are your top contributors — focus resources there."
                )
            else:
                agg   = {'type': 'count', 'group_by': field, 'name': 'val'}
                title = f'{display_field} Distribution'
                y_lbl = 'Count'
                desc  = (
                    f"How {record_noun} are distributed across {display_field.lower()} values. "
                    f"Imbalances may highlight concentration risk or growth opportunities."
                )

            insights.append({
                'type': 'bar_chart',
                'title': title,
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [agg]},
                'viz_config': {'x_axis': display_field, 'y_axis': y_lbl, 'description': desc},
            })
            bar_count += 1

            # Place a focused donut comparison in the second-row right slot:
            # row 1 = two trend charts, row 2 left = first bar chart, row 2
            # right = this donut comparing the top contributors using an
            # explicit preferred dimension such as product category.
            if bar_count == 1 and primary_value and donut_field:
                donut_display_field = self._clean_display_name(donut_field)
                insights.append({
                    'type': 'pie_chart',
                    'title': f'{display_pv} — Top 3 {donut_display_field}',
                    'width': 6, 'height': 4,
                    'query_config': {
                        'aggregations': [{
                            'type': 'sum',
                            'field': primary_value,
                            'group_by': donut_field,
                            'name': 'val',
                        }],
                        'group_order': 'value_desc',
                        'group_limit': 3,
                    },
                    'viz_config': {
                        'description': (
                            f"Focused donut comparison of the top {donut_display_field.lower()} "
                            f"contributors to {display_pv.lower()}. Use it to compare the leaders at a glance."
                        ),
                    },
                })

        # ════════════════════════════════════════════════════════════════
        #  PIE CHARTS — part-of-whole (2–8 unique values only)
        # ════════════════════════════════════════════════════════════════
        for field in very_low_card_for_pie[:2]:
            display_field = self._clean_display_name(field)

            if primary_value:
                agg   = {'type': 'sum', 'field': primary_value,
                         'group_by': field, 'name': 'val'}
                title = f'{display_pv} Share by {display_field}'
                desc  = (
                    f"How {display_pv.lower()} is split across {display_field.lower()} segments. "
                    f"Each slice shows that segment's contribution to the total."
                )
            else:
                agg   = {'type': 'count', 'group_by': field, 'name': 'val'}
                title = f'{display_field} Breakdown'
                desc  = (
                    f"Proportional breakdown of {record_noun} by {display_field.lower()}. "
                    f"The largest slice is your dominant category."
                )

            insights.append({
                'type': 'pie_chart',
                'title': title,
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [agg]},
                'viz_config': {'description': desc},
            })

        # ════════════════════════════════════════════════════════════════
        #  AVERAGE BY CATEGORY — reveal which segment performs best
        #  (only when we have both a primary value AND categorical dims)
        # ════════════════════════════════════════════════════════════════
        if primary_value and all_categorical:
            # Pick the highest-scoring categorical field not already in bar charts
            already_used = set(all_categorical[:bar_count])
            avg_candidates = [f for f in all_categorical if f not in already_used]
            if avg_candidates:
                avg_field   = avg_candidates[0]
                display_avg = self._clean_display_name(avg_field)
                insights.append({
                    'type': 'bar_chart',
                    'title': f'Avg {display_pv} per {display_avg}',
                    'width': 6, 'height': 4,
                    'query_config': {'aggregations': [
                        {'type': 'avg', 'field': primary_value,
                         'group_by': avg_field, 'name': 'val'}
                    ]},
                    'viz_config': {
                        'x_axis': display_avg, 'y_axis': f'Avg {display_pv}',
                        'description': (
                            f"Average {display_pv.lower()} per {display_avg.lower()}. "
                            f"Use this to benchmark {display_avg.lower()} groups against each other "
                            f"and identify outliers worth investigating."
                        ),
                    },
                })

        # ════════════════════════════════════════════════════════════════
        #  SECONDARY NUMERIC CORRELATION — when 2+ valid numeric fields
        #  show Avg(primary) by buckets of the secondary field
        # ════════════════════════════════════════════════════════════════
        if len(valid_numeric) >= 2 and primary_value:
            sec_num = [f for f in valid_numeric[1:] if f not in pct_fields][:1]
            for sec_f in sec_num:
                display_sec = self._clean_display_name(sec_f)
                n_unique    = len(self._unique_values(sec_f, sample))
                if n_unique <= 30:   # discrete enough for a grouped bar
                    insights.append({
                        'type': 'bar_chart',
                        'title': f'{display_pv} vs {display_sec}',
                        'width': 6, 'height': 4,
                        'query_config': {'aggregations': [
                            {'type': 'avg', 'field': primary_value,
                             'group_by': sec_f, 'name': 'val'}
                        ]},
                        'viz_config': {
                            'x_axis': display_sec, 'y_axis': f'Avg {display_pv}',
                            'description': (
                                f"Relationship between {display_sec.lower()} and "
                                f"average {display_pv.lower()}. "
                                f"A clear pattern here may indicate a strong correlation "
                                f"worth acting on."
                            ),
                        },
                    })

        # ════════════════════════════════════════════════════════════════
        #  TEXT-ONLY / NO NUMERIC edge case
        #  When there are no numeric fields at all, focus on distribution
        #  charts across the most informative categorical dimensions.
        # ════════════════════════════════════════════════════════════════
        if not valid_numeric and all_categorical:
            # We already emitted bar charts above; add one more pie if possible
            extra_pies = [f for f in very_low_card_for_pie[2:4]]
            for field in extra_pies:
                display_field = self._clean_display_name(field)
                insights.append({
                    'type': 'pie_chart',
                    'title': f'{display_field} Composition',
                    'width': 6, 'height': 4,
                    'query_config': {'aggregations': [
                        {'type': 'count', 'group_by': field, 'name': 'val'}
                    ]},
                    'viz_config': {
                        'description': (
                            f"Composition of {record_noun} by {display_field.lower()}. "
                            f"Each slice represents one distinct value's share of the total."
                        ),
                    },
                })

        return insights

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _kpi_spec(self, title, agg_type, field, color_i,
                  prefix='', suffix='', description=''):
        """Build a metric widget spec with branded icon/colour and narrative description."""
        agg = {'type': agg_type, 'name': 'val'}
        if field:
            agg['field'] = field
        color = self._KPI_COLORS[color_i % len(self._KPI_COLORS)]
        icon  = self._KPI_ICONS[color_i  % len(self._KPI_ICONS)]
        viz   = {'icon': icon, 'color': color, 'prefix': prefix, 'suffix': suffix}
        if description:
            viz['description'] = description
        return {
            'type': 'metric',
            'title': title,
            'width': 3, 'height': 2,
            'query_config': {'aggregations': [agg]},
            'viz_config': viz,
        }

    def _normalize_name(self, field_name):
        """
        Return a lowercase, space-separated version of a field name so keyword
        checks work regardless of whether the source used CamelCase, snake_case,
        spaces, or a mix.

        Examples:
          'TotalRevenue'   → 'total revenue'
          'unit_price'     → 'unit price'
          'UnitPrice'      → 'unit price'
          'Total (KES)'    → 'total  kes '   (parens stripped by callers)
          'CustomerType'   → 'customer type'
        """
        import re
        # 1. Insert space before each uppercase letter that follows a lowercase/digit
        name = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', field_name)
        # 2. Insert space before a run of uppercase letters followed by lowercase
        #    e.g. "OrderID" → "Order ID"
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
        # 3. Replace underscores and hyphens with spaces
        name = name.replace('_', ' ').replace('-', ' ')
        return name.lower().strip()

    def _score_field_for_kpi(self, field_name, field_type='number'):  # noqa: ARG002
        """
        Return an integer score for how useful a numeric field is as a KPI.
        Higher = more meaningful.  Negative scores are excluded from KPI cards.

        Rules applied (cumulative):
          +5  field name contains a revenue/total/amount keyword
          +2  field name contains 'subtotal' (useful but secondary to 'total')
          +1  field name contains quantity/count/units keyword
          -6  field name contains percent / % / rate / discount / tax
              (summing these produces a nonsense number)
          -4  field name suggests a unit price rather than a line total

        Uses _normalize_name so CamelCase / snake_case fields are handled
        identically to space-separated names.
        """
        name = self._normalize_name(field_name)
        words = set(name.split())
        score = 0
        if any(k in name for k in ('revenue', 'sales', 'income', 'earnings')):
            score += 5
        elif 'total' in name and 'subtotal' not in name and 'sub total' not in name:
            score += 5
        elif 'subtotal' in name or 'sub total' in name:
            score -= 1  # always redundant when a 'total' field exists
        elif any(k in name for k in ('amount', 'value', 'price')) and \
                not any(k in name for k in ('unit price', 'price per', 'rate per', 'cost per')):
            score += 3
        if any(k in name for k in ('quantity', 'qty', 'units', 'pieces', 'count',
                                   'mileage', 'odometer', 'volume', 'headcount',
                                   'enrollment', 'patients', 'responses')):
            score += 1
        # Finance / HR specific positive signals
        if any(k in name for k in ('salary', 'payroll', 'wage', 'compensation',
                                   'balance', 'budget', 'expense', 'profit', 'margin',
                                   'loan', 'credit', 'debit', 'asset', 'liability')):
            score += 4
        # Healthcare / inventory positive
        if any(k in name for k in ('dose', 'dosage', 'stock', 'inventory', 'stock level',
                                   'reorder', 'issued', 'received', 'dispensed')):
            score += 2
        # Survey scores
        if any(k in name for k in ('nps', 'csat', 'satisfaction score', 'rating')):
            score += 3
        if any(k in name for k in ('percent', ' %', '%', 'rate', 'ratio', 'discount', 'tax')):
            score -= 6
        if any(k in name for k in ('unit price', 'price per', 'cost per', 'rate per')):
            score -= 4
        # Year / model-year columns — summing years is meaningless as a KPI
        if any(k in words for k in ('year', 'yr')):
            score -= 8
        # ID/code fields
        if any(k in words for k in ('id', 'code', 'ref', 'no', 'num', 'number', 'index')):
            score -= 6
        return score

    def _score_field_for_chart(self, field_name):
        """
        Return an integer score for how useful a text field is as a chart dimension.
        Higher = more meaningful breakdown axis.

        Fields like 'Category', 'Species', 'Staff', 'Payment Method', 'Status'
        score highly.  ID / name / description fields score negatively.

        Uses _normalize_name so CamelCase / snake_case fields are handled
        identically to space-separated names.
        """
        name = self._normalize_name(field_name)
        words = set(name.split())
        score = 0
        if any(k in name for k in ('category', 'type', 'kind', 'class', 'group', 'segment')):
            score += 5
        if any(k in name for k in ('species', 'breed', 'product', 'service',
                                   'item', 'department', 'brand',
                                   'make', 'manufacturer', 'vendor', 'supplier',
                                   'model', 'variant', 'trim', 'edition', 'version')):
            score += 4
        if any(k in name for k in ('staff', 'agent', 'employee', 'rep', 'handler',
                                   'doctor', 'vet', 'nurse', 'technician', 'assigned',
                                   'salesperson', 'seller', 'sales person')):
            score += 4
        if any(k in name for k in ('payment', 'method', 'channel', 'medium')) or 'mode' in words:
            score += 3
        if any(k in name for k in ('status', 'state', 'stage', 'result', 'outcome')):
            score += 3
        if any(k in name for k in ('region', 'location', 'area', 'zone', 'branch',
                                   'store', 'outlet', 'site', 'territory')):
            score += 3
        if any(k in name for k in ('gender', 'sex', 'age group', 'tier', 'segment',
                                   'priority', 'severity', 'level', 'rank',
                                   'ward', 'shift', 'grade', 'class', 'faculty')):
            score += 2
        # Year / model-year columns make excellent bar-chart dimensions
        if any(k in words for k in ('year', 'yr', 'vintage', 'semester', 'quarter')):
            score += 3
        # Logistics / healthcare breakdowns
        if any(k in name for k in ('route', 'destination', 'origin', 'diagnosis',
                                   'procedure', 'treatment', 'medication',
                                   'channel', 'source', 'platform')):
            score += 3
        # Education / survey dimensions
        if any(k in name for k in ('subject', 'course', 'module', 'question',
                                   'response', 'outcome', 'result')):
            score += 2
        # Penalise identifier / free-text fields — match on word boundaries
        # by checking after normalisation (e.g. 'order id' not 'salesperson')
        words = set(name.split())
        if any(k in words for k in ('id', 'ref', 'code', 'no', 'num', 'number', 'index')):
            score -= 4
        if any(k in name for k in ('reference', 'description', 'note', 'comment',
                                   'remark', 'detail', 'info', 'full name',
                                   'client name', 'customer name', 'address',
                                   'email', 'phone', 'contact')):
            score -= 4
        return score

    def _pick_focus_donut_field(self, categorical_fields, domain):
        """
        Pick a stable dimension for the executive donut widget.

        For sales-style dashboards we prefer dimensions like product category
        over more generic ranked categorical fields so the second-row-right
        donut stays predictable across regenerations.
        """
        if not categorical_fields:
            return None

        preferences = {
            'sales': [
                ('product', 'category'),
                ('customer', 'segment'),
            ],
            'healthcare': [
                ('diagnosis',),
                ('customer', 'segment'),
                ('channel',),
                ('payment', 'status'),
            ],
            'inventory': [
                ('product', 'category'),
                ('supplier',),
                ('warehouse',),
                ('status',),
            ],
            'generic': [
                ('product', 'category'),
                ('category',),
                ('segment',),
                ('channel',),
                ('status',),
                ('type',),
                ('group',),
            ],
        }

        keyword_sets = preferences.get(domain, []) + preferences['generic']
        normalized = [(field, self._normalize_name(field)) for field in categorical_fields]

        for keywords in keyword_sets:
            for field, norm in normalized:
                if all(keyword in norm for keyword in keywords):
                    return field

        if domain == 'sales':
            return None

        return categorical_fields[0]

    def _clean_display_name(self, field_name):
        """
        Convert a raw field name to a clean axis / title label.

        Examples:
          'Total (KES)'          → 'Revenue'
          'Subtotal (KES)'       → 'Subtotal'
          'Unit Price (KES)'     → 'Unit Price'
          'Discount %'           → 'Discount'
          'created_at_date'      → 'Date'
          'payment_method'       → 'Payment Method'

        When the bare name after stripping units is just 'Total' we return
        'Revenue' instead — 'Total Total' as a KPI title reads poorly.
        """
        import re
        if not field_name or field_name in ('created_at_date', 'created_at', '_created_at'):
            return 'Date'
        # Strip parenthetical units: "Total (KES)" → "Total"
        name = re.sub(r'\s*\([^)]*\)', '', field_name).strip()
        # Strip trailing punctuation / unit symbols
        name = re.sub(r'[\s%/\\|]+$', '', name).strip()
        # Split CamelCase: "TotalRevenue" → "Total Revenue", "OrderID" → "Order ID"
        name = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name)
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
        # Replace underscores / hyphens with spaces
        name = name.replace('_', ' ').replace('-', ' ').strip()
        # Avoid generic single-word names that produce doubled KPI titles
        # e.g. "Total (KES)" → bare "Total" → rename to "Revenue"
        if name.lower() == 'total':
            return 'Revenue'
        if name.lower() == 'amount':
            return 'Amount'
        return name if name else field_name

    def _agg_title(self, agg_type, display_name):
        """
        Build a KPI title without doubling the aggregation word.

        _agg_title('sum', 'Revenue')        → 'Total Revenue'
        _agg_title('sum', 'Total Revenue')  → 'Total Revenue'   (no double)
        _agg_title('avg', 'Total Revenue')  → 'Avg Revenue'     (strip existing prefix)
        _agg_title('sum', 'Units Sold')     → 'Total Units Sold'
        """
        agg_words = {'sum': 'Total', 'avg': 'Avg', 'count': 'Count',
                     'max': 'Max', 'min': 'Min'}
        prefix = agg_words.get(agg_type, 'Total')
        # Strip any existing aggregation prefix from the display name so we
        # don't produce 'Total Total Revenue' or 'Avg Total Revenue'.
        clean = display_name
        for word in agg_words.values():
            if clean.lower().startswith(word.lower() + ' '):
                clean = clean[len(word):].strip()
                break
        return f'{prefix} {clean}'

    def _currency_prefix(self, field_name, field_type=None):
        """
        Return a short currency prefix for KPI cards based on field name / type.
        'Total (KES)' → 'KES '   'Revenue ($)' → '$'   fallback → ''
        """
        name = (field_name or '').lower()
        if 'kes' in name:
            return 'KES '
        if 'usd' in name or '($)' in name:
            return '$'
        if 'gbp' in name or '(£)' in name:
            return '£'
        if 'eur' in name or '(€)' in name:
            return '€'
        if field_type == 'currency':
            return '$'
        return ''

    def _filter_numeric_fields(self, fields, sample):
        """Return only fields that actually contain parseable numbers."""
        valid = []
        for field in fields:
            for rec in sample[:200]:
                if rec and field in rec:
                    try:
                        float(rec[field])
                        valid.append(field)
                        break
                    except (ValueError, TypeError):
                        pass
        return valid

    def _unique_values(self, field, sample):
        """Return set of non-null unique string values for a field."""
        seen = set()
        for rec in sample:
            if rec and field in rec:
                v = rec[field]
                if v not in (None, '', 'None', 'nan'):
                    seen.add(str(v))
        return seen

    # ------------------------------------------------------------------ #
    #  Domain detection
    # ------------------------------------------------------------------ #
    _DOMAIN_SIGNALS = {
        'sales':      ['invoice', 'customer', 'product', 'revenue', 'sale', 'order',
                       'item', 'receipt', 'purchase', 'client', 'discount', 'transaction',
                       'basket', 'cart', 'coupon', 'loyalty', 'crm'],
        'hr':         ['employee', 'staff', 'salary', 'department', 'hire', 'leave',
                       'payroll', 'position', 'attendance', 'performance', 'headcount',
                       'appraisal', 'contract', 'onboarding', 'termination', 'shift'],
        'healthcare': ['patient', 'diagnosis', 'doctor', 'hospital', 'treatment',
                       'medication', 'appointment', 'prescription', 'clinic', 'ward',
                       'nurse', 'procedure', 'icu', 'discharge', 'admission', 'vitals'],
        'inventory':  ['stock', 'sku', 'warehouse', 'reorder', 'supplier', 'batch',
                       'expiry', 'shelf', 'bin', 'issued', 'received', 'units',
                       'location', 'barcode', 'lead time', 'goods', 'asset'],
        'finance':    ['account', 'debit', 'credit', 'balance', 'ledger', 'journal',
                       'budget', 'expense', 'asset', 'liability', 'equity', 'loan',
                       'interest', 'repayment', 'invoice', 'payment', 'reconcil'],
        'logistics':  ['shipment', 'delivery', 'route', 'driver', 'vehicle',
                       'dispatch', 'tracking', 'destination', 'origin', 'freight',
                       'waybill', 'manifest', 'consignment', 'eta', 'fleet'],
        'education':  ['student', 'grade', 'course', 'teacher', 'class', 'score',
                       'exam', 'subject', 'school', 'enrollment', 'marks', 'module',
                       'lecture', 'assignment', 'semester', 'gpa', 'faculty'],
        'survey':     ['response', 'rating', 'satisfaction', 'feedback', 'likert',
                       'nps', 'sentiment', 'agree', 'strongly', 'question', 'score',
                       'net promoter', 'csat', 'opinion', 'respondent'],
        'iot':        ['sensor', 'temperature', 'humidity', 'pressure', 'reading',
                       'device', 'signal', 'voltage', 'meter', 'threshold', 'alert',
                       'telemetry', 'firmware', 'gateway', 'kwh', 'rpm'],
    }

    def _detect_data_domain(self, schema, sample):
        """
        Detect the business domain from field names and a sample of record values.
        Returns one of: 'sales' | 'hr' | 'healthcare' | 'inventory' | 'finance' |
                        'logistics' | 'education' | 'survey' | 'iot' | 'generic'
        """
        all_fields = ' '.join(schema.keys()).lower()
        scores     = {d: 0.0 for d in self._DOMAIN_SIGNALS}

        for domain, keywords in self._DOMAIN_SIGNALS.items():
            for kw in keywords:
                if kw in all_fields:
                    scores[domain] += 1.0

        # Lighter-weight signal from actual values
        if sample:
            value_text = ' '.join(
                str(v).lower()
                for rec in sample[:40]
                for v in (rec or {}).values()
                if v and isinstance(v, str) and len(str(v)) < 60
            )
            for domain, keywords in self._DOMAIN_SIGNALS.items():
                for kw in keywords:
                    if kw in value_text:
                        scores[domain] += 0.3

        best = max(scores, key=scores.get)
        return best if scores[best] >= 1.0 else 'generic'

    def _domain_count_label(self, domain):
        """Human-readable KPI label for total record count, tailored to domain."""
        return {
            'sales':      'Total Transactions',
            'hr':         'Total Employees',
            'healthcare': 'Total Patients',
            'inventory':  'Total Items',
            'finance':    'Total Entries',
            'logistics':  'Total Shipments',
            'education':  'Total Students',
            'survey':     'Total Responses',
            'iot':        'Total Readings',
            'generic':    'Total Records',
        }.get(domain, 'Total Records')

    def _domain_record_noun(self, domain):
        """Singular/plural noun for records in this domain (used in descriptions)."""
        return {
            'sales':      'transactions',
            'hr':         'employees',
            'healthcare': 'patients',
            'inventory':  'items',
            'finance':    'entries',
            'logistics':  'shipments',
            'education':  'students',
            'survey':     'responses',
            'iot':        'readings',
            'generic':    'records',
        }.get(domain, 'records')
    
    def _has_meaningful_date_variation(self, field_name, sample):
        values = []
        for rec in sample:
            if not rec:
                continue
            if field_name == 'created_at_date':
                continue
            val = rec.get(field_name)
            if val not in (None, '', 'None', 'nan'):
                values.append(str(val)[:10])
        return len(set(values)) > 1


class AuditService:
    """
    Service for creating audit logs
    """
    
    @staticmethod
    def log(request, action, content_type, object_id, object_repr, changes=None):
        """
        Create an audit log entry
        """
        from .models import AuditLog
        
        workspace = None
        if hasattr(request.user, 'current_workspace'):
            workspace = request.user.current_workspace
        
        AuditLog.objects.create(
            workspace=workspace,
            user=request.user if request.user.is_authenticated else None,
            action=action,
            content_type=content_type,
            object_id=object_id,
            object_repr=object_repr,
            changes=changes or {},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )

# =============================================================================
# DataSourceQueryEngine — live queries against external databases
# =============================================================================

class DataSourceQueryEngine:
    """
    Execute widget queries against a directly connected external database.

    Supports the same widget types and query_config schema as the internal
    QueryEngine, but translates them into parameterised SQL executed against
    the connector specified by *data_source*.

    Security notes
    --------------
    * Only SELECT statements are executed.  No DDL/DML is possible because
      queries are built from the structured query_config (no raw SQL input).
    * All identifiers (table, column names) are validated against
      Widget._SAFE_FIELD_RE before being interpolated — no injection possible.
    * Queries are executed with a per-statement timeout (default 30 s).
    * Only PostgreSQL is supported in this release.  MySQL support is planned.
    """

    QUERY_TIMEOUT_SECONDS = 30

    def __init__(self, data_source):
        self.data_source = data_source

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute_widget_query(self, widget, limit=1000, extra_filters=None):
        """Run a widget query against the external database.

        Returns the same dict shape as QueryEngine.execute_widget_query so
        the front-end does not need separate rendering paths.
        """
        if self.data_source.connector_type != 'postgresql':
            return {'error': f"Connector type '{self.data_source.connector_type}' is not yet supported."}

        table_name = widget.source_table_name
        if not self._validate_identifier(table_name):
            return {'error': f"Invalid source_table_name: {table_name!r}"}

        try:
            if widget.widget_type in ('metric', 'number', 'gauge'):
                return self._exec_metric(widget, table_name, extra_filters)
            elif widget.widget_type == 'table':
                return self._exec_table(widget, table_name, limit, extra_filters)
            elif widget.widget_type in ('line_chart', 'area_chart', 'bar_chart', 'pie_chart', 'heatmap'):
                return self._exec_chart(widget, table_name, limit, extra_filters)
            elif widget.widget_type == 'scatter':
                return self._exec_scatter(widget, table_name, limit, extra_filters)
            elif widget.widget_type == 'histogram':
                return self._exec_histogram(widget, table_name, limit, extra_filters)
            else:
                return {'error': f"Unknown widget type: {widget.widget_type}"}
        except Exception as exc:
            logger.error("DataSourceQueryEngine error for widget %s: %s", widget.id, exc, exc_info=True)
            return {'error': str(exc)}

    # ------------------------------------------------------------------
    # Metric (scalar aggregation)
    # ------------------------------------------------------------------

    def _exec_metric(self, widget, table_name, extra_filters):
        config = widget.query_config or {}
        aggs   = config.get('aggregations', [])
        where_clause, params = self._build_where(config.get('filters', []), extra_filters or [])

        result = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                if not aggs:
                    sql = f'SELECT COUNT(*) FROM {self._qi(table_name)}'
                    if where_clause:
                        sql += f' WHERE {where_clause}'
                    cur.execute(sql, params)
                    result['val'] = cur.fetchone()[0]
                else:
                    for agg in aggs:
                        agg_type = agg.get('type', 'count')
                        field    = agg.get('field')
                        name     = agg.get('name', 'val')
                        if not self._validate_agg_type(agg_type):
                            result[name] = None
                            continue
                        if agg_type == 'count':
                            expr = 'COUNT(*)'
                        elif field and self._validate_identifier(field):
                            expr = f'{agg_type.upper()}({self._qi(field)})'
                        else:
                            result[name] = None
                            continue

                        sql = f'SELECT {expr} FROM {self._qi(table_name)}'
                        if where_clause:
                            sql += f' WHERE {where_clause}'
                        cur.execute(sql, params)
                        row = cur.fetchone()
                        result[name] = float(row[0]) if row and row[0] is not None else 0
        return result

    # ------------------------------------------------------------------
    # Table (row listing)
    # ------------------------------------------------------------------

    def _exec_table(self, widget, table_name, limit, extra_filters):
        config = widget.query_config or {}
        row_limit = min(int(config.get('limit', limit)), 1000)
        where_clause, params = self._build_where(config.get('filters', []), extra_filters or [])

        sql = f'SELECT * FROM {self._qi(table_name)}'
        if where_clause:
            sql += f' WHERE {where_clause}'
        sql += f' LIMIT {row_limit}'

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols    = [desc[0] for desc in cur.description]
                rows    = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # Chart (grouped aggregation)
    # ------------------------------------------------------------------

    def _exec_chart(self, widget, table_name, limit, extra_filters):
        config = widget.query_config or {}
        aggs   = config.get('aggregations', [])
        where_clause, params = self._build_where(config.get('filters', []), extra_filters or [])

        if not aggs:
            return {'val': {}}

        agg      = aggs[0]
        agg_type = agg.get('type', 'count')
        field    = agg.get('field')
        group_by = agg.get('group_by')
        name     = agg.get('name', 'val')

        if not self._validate_agg_type(agg_type):
            return {'error': f"Unsupported aggregation type: {agg_type}"}
        if not group_by or not self._validate_identifier(group_by):
            return {'error': f"Invalid or missing group_by field: {group_by!r}"}

        if agg_type == 'count':
            val_expr = 'COUNT(*)'
        elif field and self._validate_identifier(field):
            val_expr = f'{agg_type.upper()}({self._qi(field)})'
        else:
            return {'error': f"Invalid field: {field!r}"}

        sql = (
            f'SELECT {self._qi(group_by)}, {val_expr} '
            f'FROM {self._qi(table_name)}'
        )
        if where_clause:
            sql += f' WHERE {where_clause}'
        sql += f' GROUP BY {self._qi(group_by)} ORDER BY {self._qi(group_by)} LIMIT {min(limit, 500)}'

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        grouped = {}
        for label, value in rows:
            grouped[str(label)] = float(value) if value is not None else 0.0
        grouped = QueryEngine._apply_grouped_result_options(grouped, config)
        return {name: grouped}

    def _exec_histogram(self, widget, table_name, limit, extra_filters):
        config = widget.query_config or {}
        field = config.get('field')
        if not field or not self._validate_identifier(field):
            return {'error': 'Histogram requires a valid query_config.field'}

        bins = config.get('bins', 12)
        try:
            bins = max(5, min(int(bins), 40))
        except (TypeError, ValueError):
            bins = 12

        where_clause, params = self._build_where(config.get('filters', []), extra_filters or [])
        expr = self._numeric_expr(field)
        sql = f'SELECT v FROM (SELECT {expr} AS v FROM {self._qi(table_name)}'
        if where_clause:
            sql += f' WHERE {where_clause}'
        sql += ') AS histogram_source WHERE v IS NOT NULL '
        sql += f'LIMIT {min(limit, 5000)}'

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        values = [round(float(row[0]), 4) for row in rows if row and row[0] is not None]
        if not values:
            return {'message': 'No numeric data available'}

        kde = QueryEngine()._build_kde(values) if config.get('show_kde', True) else None
        return {
            'field': field,
            'values': values,
            'bins': bins,
            'kde': kde,
        }

    def _exec_scatter(self, widget, table_name, limit, extra_filters):
        config = widget.query_config or {}
        x_field = config.get('x_field')
        y_field = config.get('y_field')
        label_field = config.get('label_field')

        if not x_field or not y_field:
            return {'error': 'Scatter plot requires query_config.x_field and query_config.y_field'}
        if not self._validate_identifier(x_field) or not self._validate_identifier(y_field):
            return {'error': 'Scatter plot requires valid x_field and y_field identifiers'}
        if label_field and not self._validate_identifier(label_field):
            return {'error': 'Scatter plot label_field is invalid'}

        where_clause, params = self._build_where(config.get('filters', []), extra_filters or [])
        x_expr = self._numeric_expr(x_field)
        y_expr = self._numeric_expr(y_field)
        label_sql = f', {self._qi(label_field)}::text AS label' if label_field else ''
        sql = (
            f'SELECT x, y{", label" if label_field else ""} FROM ('
            f'SELECT {x_expr} AS x, {y_expr} AS y{label_sql} '
            f'FROM {self._qi(table_name)}'
        )
        if where_clause:
            sql += f' WHERE {where_clause}'
        sql += ') AS scatter_source WHERE x IS NOT NULL AND y IS NOT NULL '
        sql += f'LIMIT {min(limit, 2000)}'

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        points = []
        for row in rows:
            point = {
                'x': round(float(row[0]), 4),
                'y': round(float(row[1]), 4),
            }
            if label_field and len(row) > 2 and row[2] not in (None, ''):
                point['label'] = str(row[2])
            points.append(point)

        if not points:
            return {'message': 'No numeric data available'}

        return {
            'x_field': x_field,
            'y_field': y_field,
            'points': points,
        }

    # ------------------------------------------------------------------
    # WHERE clause builder
    # ------------------------------------------------------------------

    def _build_where(self, widget_filters, extra_filters):
        """Build a parameterised WHERE clause from filter lists.

        Returns (clause_str, params_list).  clause_str is empty string when
        there are no filters.  Uses %s placeholders (psycopg3 default).
        """
        parts  = []
        params = []
        OPERATOR_MAP = {
            'eq':       '=',
            'neq':      '!=',
            'gt':       '>',
            'gte':      '>=',
            'lt':       '<',
            'lte':      '<=',
            'contains': 'ILIKE',
        }

        for f in list(widget_filters) + list(extra_filters):
            field = f.get('field')
            op    = f.get('operator', 'eq')
            value = f.get('value')
            if not field or value is None:
                continue
            if not self._validate_identifier(field):
                continue
            sql_op = OPERATOR_MAP.get(op, '=')
            col    = self._qi(field)
            if op == 'contains':
                parts.append(f'{col} {sql_op} %s')
                params.append(f'%{value}%')
            else:
                parts.append(f'{col} {sql_op} %s')
                params.append(value)

        clause = ' AND '.join(parts)
        return clause, params

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self):
        """Return a psycopg3 connection with statement_timeout set."""
        import psycopg
        timeout_ms = self.QUERY_TIMEOUT_SECONDS * 1000
        conn = psycopg.connect(
            host=self.data_source.host,
            port=self.data_source.port,
            dbname=self.data_source.database,
            user=self.data_source.username,
            password=self.data_source.get_plaintext_password(),
            sslmode=self.data_source.ssl_mode,
            connect_timeout=10,
            options=f'-c statement_timeout={timeout_ms}ms',
            **(self.data_source.extra_options or {}),
        )
        conn.autocommit = True  # read-only; no need for explicit transaction management
        return conn

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    def list_tables(self) -> list[dict]:
        """Return all user tables in the connected database.

        Each entry: {"schema": str, "name": str, "row_estimate": int}.
        """
        sql = """
            SELECT n.nspname AS schema,
                   c.relname AS name,
                   c.reltuples::bigint AS row_estimate
            FROM   pg_class c
            JOIN   pg_namespace n ON n.oid = c.relnamespace
            WHERE  c.relkind IN ('r', 'v', 'm')   -- tables, views, materialised views
              AND  n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER  BY n.nspname, c.relname
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return [{'schema': r[0], 'name': r[1], 'row_estimate': r[2]} for r in rows]

    def list_columns(self, table_name: str) -> list[dict]:
        """Return columns for a specific table.

        Each entry: {"name": str, "type": str, "nullable": bool}.
        Raises ValueError on invalid table_name.
        """
        if not self._validate_identifier(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")

        sql = """
            SELECT column_name, data_type, is_nullable
            FROM   information_schema.columns
            WHERE  table_name = %s
            ORDER  BY ordinal_position
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [table_name])
                rows = cur.fetchall()
        return [
            {'name': r[0], 'type': r[1], 'nullable': r[2] == 'YES'}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_identifier(name: str) -> bool:
        """Return True only when *name* is safe to embed as a SQL identifier."""
        import re
        return bool(re.match(r'^[^\x00\n\r"\';\x7f]{1,128}$', name or ''))

    @staticmethod
    def _validate_agg_type(agg_type: str) -> bool:
        return agg_type in ('count', 'sum', 'avg', 'min', 'max')

    @staticmethod
    def _qi(name: str) -> str:
        """Quote a SQL identifier by wrapping in double-quotes."""
        return f'"{name}"'

    def _numeric_expr(self, field: str) -> str:
        """Return a Postgres expression that coerces messy numeric text to float."""
        col = self._qi(field)
        return (
            "NULLIF("
            f"regexp_replace({col}::text, '[^0-9.\\-]', '', 'g'),"
            "''"
            ")::double precision"
        )

    def fetch_sample(self, table_name: str, max_rows: int = 500):
        """
        Return a pandas DataFrame of the first *max_rows* rows from *table_name*.
        Used by WorkspaceInsightService for external-DB insight profiling.
        """
        import pandas as pd
        if not self._validate_identifier(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT * FROM {self._qi(table_name)} LIMIT %(limit)s',
                    {'limit': max_rows},
                )
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)


# ── GoogleSheetsQueryEngine ─────────────────────────────────────────────────

class GoogleSheetsQueryEngine:
    """
    Query engine for Google Sheets data sources.

    Authentication: Google Service Account JSON (encrypted with Fernet).
    The service account must have read access to the target spreadsheet
    (share the sheet with the service account e-mail address).

    Workflow:
        1. POST /api/v1/workspaces/<id>/data-sources/
           connector_type=google_sheets, google_spreadsheet_id=<id>,
           google_credentials_json=<service_account_json_string>
        2. POST /api/v1/data-sources/<id>/test/  — verifies connectivity
        3. GET  /api/v1/data-sources/<id>/schema/ — returns worksheet names + column types
        4. Create a Widget pointing at this DataSource with source_table_name=<sheet_title>
    """

    # Maximum rows we'll read from a sheet in one call to avoid memory issues.
    MAX_ROWS = 50_000

    def __init__(self, data_source):
        self.data_source = data_source

    # ── gspread client ──────────────────────────────────────────────────────

    def _get_client(self):
        """Return an authenticated gspread.Client using the stored service account."""
        import json
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheets dependencies are not installed. "
                "Run: pip install gspread google-auth"
            ) from exc

        creds_json = self.data_source.get_google_credentials()
        creds_dict = json.loads(creds_json)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets.readonly',
            'https://www.googleapis.com/auth/drive.readonly',
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)

    def _open_spreadsheet(self):
        client = self._get_client()
        return client.open_by_key(self.data_source.google_spreadsheet_id)

    # ── connectivity test ───────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """
        Verify credentials and spreadsheet access.
        Returns (ok: bool, message: str).
        """
        try:
            ss     = self._open_spreadsheet()
            sheets = ss.worksheets()
            titles = ', '.join(ws.title for ws in sheets[:5])
            extra  = f' (+{len(sheets)-5} more)' if len(sheets) > 5 else ''
            return True, f"Connected to \"{ss.title}\". Sheets: {titles}{extra}"
        except Exception as exc:
            return False, str(exc)

    # ── schema discovery ────────────────────────────────────────────────────

    def list_tables(self) -> list[dict]:
        """Return all worksheet names (each worksheet = one "table")."""
        ss = self._open_spreadsheet()
        return [
            {'name': ws.title, 'type': 'sheet', 'row_count': ws.row_count}
            for ws in ss.worksheets()
        ]

    def list_columns(self, sheet_name: str) -> list[dict]:
        """Return column names and inferred types for the given sheet."""
        df = self._sheet_to_df(sheet_name, max_rows=200)
        if df.empty:
            return []
        return [{'name': col, 'type': self._infer_column_type(df[col])} for col in df.columns]

    # ── widget query ────────────────────────────────────────────────────────

    def execute_widget_query(self, widget, limit=1000, extra_filters=None):
        """
        Execute a widget query against a Google Sheet.
        Returns the same dict/list shape as QueryEngine.execute_widget_query.
        """
        import pandas as pd

        sheet_name = widget.source_table_name
        if not sheet_name:
            return {'error': 'No sheet name configured on this widget.'}

        try:
            df = self._sheet_to_df(sheet_name)
        except Exception as exc:
            logger.error("GoogleSheetsQueryEngine sheet read error: %s", exc)
            return {'error': str(exc)}

        if df.empty:
            return {'message': 'No data in sheet'}

        qc = widget.query_config or {}

        # ── Apply filters ─────────────────────────────────────────────────
        all_filters = list(qc.get('filters', [])) + list(extra_filters or [])
        for f in all_filters:
            col = f.get('field', '')
            op  = f.get('operator', 'eq')
            val = f.get('value')
            if col not in df.columns or val is None:
                continue
            s = df[col].astype(str)
            if   op == 'eq':  df = df[s == str(val)]
            elif op == 'neq': df = df[s != str(val)]
            elif op in ('gt', 'gte', 'lt', 'lte'):
                numeric = pd.to_numeric(
                    df[col].astype(str).str.replace(r'[$£€¥₹,\s]', '', regex=True),
                    errors='coerce',
                )
                v       = float(val)
                if   op == 'gt':  df = df[numeric >  v]
                elif op == 'gte': df = df[numeric >= v]
                elif op == 'lt':  df = df[numeric <  v]
                elif op == 'lte': df = df[numeric <= v]
            elif op == 'contains':
                df = df[s.str.contains(str(val), case=False, na=False)]

        aggs = qc.get('aggregations')

        # ── Table mode — no aggregations ──────────────────────────────────
        if not aggs:
            limit = min(int(qc.get('limit', 20)), 1000)
            rows  = df.head(limit).where(df.notna(), None)
            return rows.to_dict('records')

        # ── Aggregation mode ──────────────────────────────────────────────
        agg      = aggs[0]
        agg_type = agg.get('type', 'count')
        field    = agg.get('field')
        group_by = agg.get('group_by')

        # Coerce the measure column to numeric, handling currency formatting
        # e.g. "$20,200.00" → 20200.0
        if field and field in df.columns:
            cleaned = df[field].astype(str).str.replace(r'[$£€¥₹,\s]', '', regex=True)
            df[field] = pd.to_numeric(cleaned, errors='coerce')

        if widget.widget_type == 'histogram':
            return self._exec_histogram_df(widget, df, limit)

        if widget.widget_type == 'scatter':
            return self._exec_scatter_df(widget, df, limit)

        if group_by and group_by in df.columns:
            grouped = df.groupby(group_by, sort=True)
            if agg_type == 'count':
                result = grouped.size()
            elif field and field in df.columns:
                agg_funcs = {'sum': 'sum', 'avg': 'mean', 'min': 'min', 'max': 'max'}
                fn = agg_funcs.get(agg_type, 'sum')
                result = getattr(grouped[field], fn)()
            else:
                result = grouped.size()
            grouped_dict = {
                str(k): (None if (v != v) else round(float(v), 6))
                for k, v in result.items()
            }
            grouped_dict = QueryEngine._apply_grouped_result_options(grouped_dict, qc)
            return {'val': grouped_dict}

        # Scalar aggregation
        if agg_type == 'count':
            return {'val': len(df)}
        if field and field in df.columns:
            col = df[field]
            agg_scalar = {
                'sum': float(col.sum()),
                'avg': float(col.mean()),
                'min': float(col.min()),
                'max': float(col.max()),
            }
            return {'val': round(agg_scalar.get(agg_type, float(col.sum())), 6)}
        return {'val': len(df)}

    def _exec_histogram_df(self, widget, df, limit):
        field = (widget.query_config or {}).get('field')
        if not field or field not in df.columns:
            return {'error': 'Histogram requires query_config.field'}

        bins = (widget.query_config or {}).get('bins', 12)
        try:
            bins = max(5, min(int(bins), 40))
        except (TypeError, ValueError):
            bins = 12

        cleaned = df[field].astype(str).str.replace(r'[$£€¥₹,\s]', '', regex=True)
        numeric = pd.to_numeric(cleaned, errors='coerce').dropna().head(min(limit, 5000))
        values = [round(float(v), 4) for v in numeric.tolist()]
        if not values:
            return {'message': 'No numeric data available'}

        kde = QueryEngine()._build_kde(values) if (widget.query_config or {}).get('show_kde', True) else None
        return {
            'field': field,
            'values': values,
            'bins': bins,
            'kde': kde,
        }

    def _exec_scatter_df(self, widget, df, limit):
        config = widget.query_config or {}
        x_field = config.get('x_field')
        y_field = config.get('y_field')
        label_field = config.get('label_field')
        if not x_field or not y_field:
            return {'error': 'Scatter plot requires query_config.x_field and query_config.y_field'}
        if x_field not in df.columns or y_field not in df.columns:
            return {'message': 'No numeric data available'}

        x_numeric = pd.to_numeric(
            df[x_field].astype(str).str.replace(r'[$£€¥₹,\s]', '', regex=True),
            errors='coerce',
        )
        y_numeric = pd.to_numeric(
            df[y_field].astype(str).str.replace(r'[$£€¥₹,\s]', '', regex=True),
            errors='coerce',
        )

        scatter_df = pd.DataFrame({'x': x_numeric, 'y': y_numeric})
        if label_field and label_field in df.columns:
            scatter_df['label'] = df[label_field].astype(str)

        scatter_df = scatter_df.dropna(subset=['x', 'y']).head(min(limit, 2000))
        if scatter_df.empty:
            return {'message': 'No numeric data available'}

        points = []
        for _, row in scatter_df.iterrows():
            point = {
                'x': round(float(row['x']), 4),
                'y': round(float(row['y']), 4),
            }
            if 'label' in scatter_df.columns and row.get('label') not in (None, '', 'nan'):
                point['label'] = str(row['label'])
            points.append(point)

        return {
            'x_field': x_field,
            'y_field': y_field,
            'points': points,
        }

    # ── helpers ─────────────────────────────────────────────────────────────

    def _sheet_to_df(self, sheet_name: str, max_rows: int | None = None):
        """Read a worksheet into a pandas DataFrame (first row = headers)."""
        import pandas as pd

        ss = self._open_spreadsheet()
        ws = ss.worksheet(sheet_name)
        # get_all_records() returns a list of dicts using the first row as keys.
        cap     = max_rows or self.MAX_ROWS
        records = ws.get_all_records(numericise_ignore=['all'])   # keep raw strings
        if len(records) > cap:
            records = records[:cap]
        return pd.DataFrame(records) if records else pd.DataFrame()

    @staticmethod
    def _infer_column_type(series) -> str:
        """Best-effort column type inference from a pandas Series sample."""
        import pandas as pd
        clean = series.dropna().astype(str).replace('', pd.NA).dropna()
        if clean.empty:
            return 'text'
        # Try numeric
        numeric = pd.to_numeric(clean, errors='coerce')
        if numeric.notna().mean() >= 0.8:
            return 'number'
        # Try date
        try:
            pd.to_datetime(clean, errors='raise', infer_datetime_format=True)
            return 'date'
        except (ValueError, TypeError):
            pass
        return 'text'


# ─────────────────────────────────────────────────────────────────────────────
# Data Profiler
# ─────────────────────────────────────────────────────────────────────────────

class TableProfileService:
    """
    Compute per-column statistics for a DataTable.

    Loads up to MAX_SAMPLE active records and returns a profile dict with:
    - Overall: total records, sample size, data quality score (0-100) + grade
    - Per column: completeness %, unique count, and type-specific stats
      - numeric  → mean, median, std, min, max, p25, p75, skewness
      - date/datetime → min_date, max_date, date_range_days
      - boolean  → true_count, false_count, true_pct
      - text/email/url/phone → top 5 most frequent values

    Results are cached for CACHE_TTL seconds keyed on (table PK + updated_at),
    so a new import or record edit automatically busts the cache.
    """

    MAX_SAMPLE = 10_000
    CACHE_TTL  = 300  # seconds

    _NUMERIC_TYPES  = {'number', 'currency', 'percentage'}
    _DATE_TYPES     = {'date', 'datetime'}
    _BOOL_TYPES     = {'boolean'}
    _BOOL_MAP       = {
        'true': True, 'false': False,
        '1': True,    '0': False,
        'yes': True,  'no': False,
    }

    def __init__(self, table):
        self.table = table

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def compute(self):
        """Return the profile, serving from cache when available."""
        cache_key = self._cache_key()
        cached = cache.get(cache_key)
        if cached:
            return cached

        result = self._compute_profile()
        cache.set(cache_key, result, self.CACHE_TTL)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cache_key(self):
        ts = self.table.updated_at.timestamp() if self.table.updated_at else 0
        return f"table_profile:{self.table.pk}:{ts}"

    def _compute_profile(self):
        from django.utils import timezone
        import pandas as pd

        schema       = self.table.schema or []
        total_records = self.table.record_count

        if total_records == 0 or not schema:
            return {
                'table_id':        str(self.table.pk),
                'table_name':      self.table.name,
                'total_records':   total_records,
                'sampled_records': 0,
                'is_sample':       False,
                'column_count':    len(schema),
                'quality_score':   100,
                'quality_grade':   'A',
                'columns':         [],
                'generated_at':    timezone.now().isoformat(),
            }

        # Load records (capped at MAX_SAMPLE)
        from apps.dashboards.models import Record as _Record
        raw = list(
            _Record.objects.filter(table=self.table, is_active=True)
            .values_list('data', flat=True)[:self.MAX_SAMPLE]
        )
        sampled   = len(raw)
        is_sample = total_records > self.MAX_SAMPLE

        # Build DataFrame — one column per schema field, all others ignored
        field_names = [f['name'] for f in schema]
        df = pd.DataFrame(raw, columns=field_names)

        columns            = []
        completeness_sum   = 0.0

        for field in schema:
            col_stats = self._profile_column(df, field['name'], field['type'], sampled)
            columns.append(col_stats)
            completeness_sum += col_stats['completeness_pct']

        avg_completeness = completeness_sum / len(schema) if schema else 100.0
        quality_score    = round(avg_completeness)
        quality_grade    = self._grade(quality_score)

        return {
            'table_id':        str(self.table.pk),
            'table_name':      self.table.name,
            'total_records':   total_records,
            'sampled_records': sampled,
            'is_sample':       is_sample,
            'column_count':    len(schema),
            'quality_score':   quality_score,
            'quality_grade':   quality_grade,
            'columns':         columns,
            'generated_at':    timezone.now().isoformat(),
        }

    def _profile_column(self, df, col_name, col_type, total_rows):
        import pandas as pd
        import numpy as np

        # Pull the series; missing column → treat as all-null
        series   = df[col_name] if col_name in df.columns else pd.Series([None] * total_rows)
        non_null = series.dropna()
        non_null = non_null[non_null.astype(str).str.strip() != '']

        null_count       = total_rows - len(non_null)
        completeness_pct = round(len(non_null) / total_rows * 100, 1) if total_rows else 100.0
        unique_count     = int(non_null.nunique())

        stats = {
            'name':             col_name,
            'type':             col_type,
            'total_count':      total_rows,
            'non_null_count':   int(len(non_null)),
            'null_count':       int(null_count),
            'completeness_pct': completeness_pct,
            'unique_count':     unique_count,
        }

        if col_type in self._NUMERIC_TYPES:
            numeric = pd.to_numeric(non_null, errors='coerce').dropna()
            if len(numeric) >= 1:
                p25 = round(float(numeric.quantile(0.25)), 4)
                median = round(float(numeric.median()), 4)
                p75 = round(float(numeric.quantile(0.75)), 4)
                stats.update({
                    'mean':     round(float(numeric.mean()), 4),
                    'median':   median,
                    'std':      round(float(numeric.std()), 4) if len(numeric) > 1 else 0.0,
                    'min':      round(float(numeric.min()), 4),
                    'max':      round(float(numeric.max()), 4),
                    'p25':      p25,
                    'p75':      p75,
                    'skewness': round(float(numeric.skew()), 4) if len(numeric) > 2 else 0.0,
                })
                stats['histogram'] = self._build_histogram(numeric)
                stats['box_plot'] = self._build_box_plot(stats)
            else:
                stats.update({k: None for k in ('mean', 'median', 'std', 'min', 'max', 'p25', 'p75', 'skewness')})
                stats['histogram'] = []
                stats['box_plot'] = None

        elif col_type in self._DATE_TYPES:
            dates = pd.to_datetime(non_null, errors='coerce').dropna()
            if len(dates) >= 1:
                stats.update({
                    'min_date':        dates.min().date().isoformat(),
                    'max_date':        dates.max().date().isoformat(),
                    'date_range_days': int((dates.max() - dates.min()).days),
                })

        elif col_type in self._BOOL_TYPES:
            coerced    = non_null.astype(str).str.lower().map(self._BOOL_MAP).dropna()
            true_count = int(coerced.sum())
            if len(coerced) >= 1:
                stats.update({
                    'true_count':  true_count,
                    'false_count': int(len(coerced)) - true_count,
                    'true_pct':    round(true_count / len(coerced) * 100, 1),
                })

        else:  # text, email, url, phone
            top = (
                non_null.astype(str)
                .value_counts()
                .head(5)
            )
            stats['top_values'] = [
                {'value': str(k), 'count': int(v)}
                for k, v in top.items()
            ]

        return stats

    def _build_histogram(self, numeric_series):
        values = numeric_series.astype(float).to_numpy()
        if values.size == 0:
            return []

        min_val = float(np.min(values))
        max_val = float(np.max(values))
        if min_val == max_val:
            return [{
                'start': round(min_val, 4),
                'end': round(max_val, 4),
                'count': int(values.size),
            }]

        bin_count = min(12, max(5, int(np.sqrt(values.size))))
        counts, edges = np.histogram(values, bins=bin_count)
        return [
            {
                'start': round(float(edges[idx]), 4),
                'end': round(float(edges[idx + 1]), 4),
                'count': int(counts[idx]),
            }
            for idx in range(len(counts))
        ]

    @staticmethod
    def _build_box_plot(stats):
        q1 = float(stats['p25'])
        q3 = float(stats['p75'])
        min_val = float(stats['min'])
        max_val = float(stats['max'])
        median = float(stats['median'])

        iqr = q3 - q1
        lower_fence = q1 - (1.5 * iqr)
        upper_fence = q3 + (1.5 * iqr)
        whisker_min = max(min_val, lower_fence)
        whisker_max = min(max_val, upper_fence)

        outlier_count = 0
        if min_val < lower_fence:
            outlier_count += 1
        if max_val > upper_fence:
            outlier_count += 1

        return {
            'min': round(min_val, 4),
            'q1': round(q1, 4),
            'median': round(median, 4),
            'q3': round(q3, 4),
            'max': round(max_val, 4),
            'whisker_min': round(whisker_min, 4),
            'whisker_max': round(whisker_max, 4),
            'iqr': round(iqr, 4),
            'outlier_count': int(outlier_count),
        }

    @staticmethod
    def _grade(score):
        if score >= 90: return 'A'
        if score >= 75: return 'B'
        if score >= 60: return 'C'
        if score >= 40: return 'D'
        return 'F'
