import json
import pandas as pd
import numpy as np
from django.conf import settings
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from typing import Dict, List, Any, Optional
import hashlib
import logging

from apps.dashboards.models import Record

logger = logging.getLogger(__name__)

class QueryEngine:
    """
    Enhanced query engine that understands user-defined schemas
    """

    def __init__(self):
        self.cache_timeout = getattr(settings, 'WIDGET_CACHE_TTL', 300)


    def _generate_cache_key(self, widget):
        """Generate unique cache key for widget query"""
        import hashlib
        from django.core.serializers.json import DjangoJSONEncoder
    
        # Convert UUID to string for serialization
        table_id = str(widget.table_id) if widget.table_id else 'none'
    
        key_data = {
            'table_id': table_id,
            'query_config': widget.query_config,
            'widget_type': widget.widget_type,
            'updated_at': str(widget.updated_at) if hasattr(widget, 'updated_at') else '',
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

    def _execute_table_query(self, widget, limit):
        """Execute table widget query - return raw records"""
        from .models import Record
    
        config = widget.query_config or {}
    
        # Base queryset
        queryset = Record.objects.filter(
            table=widget.table,
            is_active=True
        ).order_by('-created_at')
    
        # Apply filters if any
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
    
        # Get records
        records = list(queryset.values('data', 'created_at')[:limit])
    
        # Format for display
        result = []
        for record in records:
            row = record['data'].copy() if record['data'] else {}
            row['_created_at'] = record['created_at'].isoformat() if record['created_at'] else None
            result.append(row)
    
        return result

    def execute_widget_query(self, widget, limit=1000):
        """Execute widget query with better error handling"""
        try:
            # Validate widget has a table
            if not widget.table:
                return {"error": "No table selected"}
            
            # Generate cache key
            cache_key = self._generate_cache_key(widget)
            
            # Try cache
            cached = cache.get(cache_key)
            if cached:
                return cached
            
            # Execute based on widget type
            if widget.widget_type in ('metric', 'number', 'gauge'):
                data = self._execute_metric_query(widget, limit)
            elif widget.widget_type == 'table':
                data = self._execute_table_query(widget, limit)
            elif widget.widget_type in ['line_chart', 'bar_chart', 'pie_chart', 'scatter', 'heatmap']:
                data = self._execute_chart_query(widget, limit)
            else:
                data = {"error": f"Unknown widget type: {widget.widget_type}"}
            
            # Cache if successful
            if data and 'error' not in data:
                cache.set(cache_key, data, self.cache_timeout)
            
            return data
            
        except Exception as e:
            logger.error(f"Widget query error: {str(e)}", exc_info=True)
            return {"error": str(e)}
    
    def _execute_metric_query(self, widget, limit):
        """Execute metric widget query"""
        from .models import Record
        
        config = widget.query_config or {}
        aggregations = config.get('aggregations', [])
        
        # Base queryset
        queryset = Record.objects.filter(
            table=widget.table,
            is_active=True
        )
        
        # Apply filters if any
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        
        if not aggregations:
            # Default to count
            return {'val': queryset.count()}
        
        # Handle different aggregation types
        result = {}
        for agg in aggregations:
            agg_type = agg.get('type', 'count')
            field = agg.get('field')
            name = agg.get('name', 'val')
            
            if agg_type == 'count':
                result[name] = queryset.count()
            elif agg_type in ['sum', 'avg', 'min', 'max'] and field:
                # Aggregate JSON field values in Python (Postgres JSONB aggregate
                # support requires raw SQL; this approach is simpler and safe up
                # to QUERY_ENGINE_MAX_RECORDS rows).
                max_records = getattr(settings, 'QUERY_ENGINE_MAX_RECORDS', 10000)
                records = queryset.values_list('data', flat=True)[:max_records]
                
                values = []
                for record_data in records:
                    if record_data and field in record_data:
                        try:
                            val = float(record_data[field])
                            values.append(val)
                        except (ValueError, TypeError):
                            pass
                
                if values:
                    if agg_type == 'sum':
                        result[name] = sum(values)
                    elif agg_type == 'avg':
                        result[name] = sum(values) / len(values)
                    elif agg_type == 'min':
                        result[name] = min(values)
                    elif agg_type == 'max':
                        result[name] = max(values)
                else:
                    result[name] = 0
        
        return result
    
    def _execute_chart_query(self, widget, limit):
        """Execute chart widget query and return data formatted for the frontend."""
        from .models import Record
        from collections import defaultdict

        config = widget.query_config or {}
        aggregations = config.get('aggregations', [])

        if not aggregations:
            return {"error": "No aggregations configured"}

        queryset = Record.objects.filter(table=widget.table, is_active=True)
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])

        records = list(queryset.values('data', 'created_at')[:limit])
        if not records:
            return {"message": "No data", "val": {}}

        result = {}
        for agg in aggregations:
            agg_type = agg.get('type', 'count')
            field    = agg.get('field')
            group_by = agg.get('group_by')
            name     = agg.get('name', 'val')

            try:
                if group_by:
                    # Grouped aggregation — builds {label: value} dict for charts
                    sums   = defaultdict(float)
                    counts = defaultdict(int)

                    for rec in records:
                        rdata = rec['data'] or {}

                        # Resolve the group-by label
                        if group_by in ('created_at', '_created_at', 'created_at_date'):
                            label = rec['created_at'].date().isoformat()
                        elif group_by in rdata:
                            raw = rdata[group_by]
                            # Skip None / blank labels
                            if raw is None or str(raw).strip() in ('', 'None', 'nan'):
                                continue
                            # Normalise datetime strings to date-only (YYYY-MM-DD)
                            # so labels are consistent regardless of whether the value
                            # was stored with a time component (e.g. "2025-11-24T00:00:00").
                            raw_str = str(raw)
                            if len(raw_str) > 10 and raw_str[10] in ('T', ' ') and raw_str[:4].isdigit():
                                raw_str = raw_str[:10]
                            label = raw_str
                        else:
                            continue

                        # Resolve the numeric value for this record
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
                        result[name] = {}
                        continue

                    # Compute final value per label
                    if agg_type == 'avg':
                        grouped = {k: sums[k] / counts[k] for k in sums}
                    else:
                        grouped = dict(sums)

                    # Sort keys chronologically when they look like ISO dates;
                    # fall back to plain string sort for categorical labels.
                    def _sort_key(k):
                        try:
                            from datetime import date as _date
                            return (0, _date.fromisoformat(str(k)))
                        except (ValueError, TypeError):
                            return (1, str(k))

                    result[name] = {
                        str(k): round(v, 4)
                        for k, v in sorted(grouped.items(), key=lambda kv: _sort_key(kv[0]))
                    }

                else:
                    # Scalar aggregation (used by metric widgets)
                    if agg_type == 'count':
                        result[name] = len(records)
                    elif field:
                        vals = []
                        for rec in records:
                            rdata = rec['data'] or {}
                            if field in rdata:
                                try:
                                    vals.append(float(rdata[field]))
                                except (ValueError, TypeError):
                                    pass
                        if not vals:
                            result[name] = 0
                        elif agg_type == 'sum':
                            result[name] = sum(vals)
                        elif agg_type == 'avg':
                            result[name] = sum(vals) / len(vals)
                        elif agg_type == 'min':
                            result[name] = min(vals)
                        elif agg_type == 'max':
                            result[name] = max(vals)
                        else:
                            result[name] = sum(vals)
                    else:
                        result[name] = 0

            except Exception as exc:
                logger.error(f"Chart aggregation error: {exc}", exc_info=True)
                result[name] = {"error": str(exc)}

        return result
    
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

    def import_data(self, table, df, user, mapping=None):
        """
        Import data from a DataFrame into a table using bulk_create for performance.
        Row-by-row Record.save() triggered a COUNT + UPDATE per row (O(n²) on large files);
        bulk_create collapses that to a single INSERT batch + one count refresh at the end.
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
        error_count = 0
        errors = []
        BATCH_SIZE = getattr(settings, 'IMPORT_BATCH_SIZE', 200)

        # Prepare all Record objects, collecting per-row errors
        pending = []
        for row_num, row in enumerate(records_data, start=1):
            try:
                if mapping:
                    mapped_row = {target: row[src] for target, src in mapping.items() if src in row}
                else:
                    mapped_row = dict(row)
                mapped_row = self._sanitize_row(mapped_row)
                pending.append(Record(table=table, data=mapped_row, created_by=user))
            except Exception as e:
                error_count += 1
                errors.append(f"Row {row_num}: {str(e)}")

        # Bulk-insert in batches inside a single transaction so a mid-import
        # failure never leaves partial data committed to the database.
        # bulk_create bypasses Record.save() intentionally — we update
        # record_count once after all batches finish.
        with transaction.atomic():
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

            # Update record_count exactly once after all inserts
            table.record_count = table.records.filter(is_active=True).count()
            table.save(update_fields=['record_count'])

        return {
            'success': success_count,
            'errors': error_count,
            'error_details': errors[:10],
        }

    def _sanitize_row(self, row):
        """Convert a row dict to contain only JSON-serializable Python native types."""
        import datetime
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
        original_cols = list(df.columns)

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

            best_parsed = None
            best_fmt    = None
            best_score  = -1

            for fmt in _DATE_FORMATS:
                try:
                    trial = pd.to_datetime(sample, format=fmt, errors='coerce')
                    score = trial.notna().sum()
                    if score > best_score:
                        best_score  = score
                        best_fmt    = fmt
                        best_parsed = trial
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
        Automatically detect field types from a DataFrame
        """
        schema = []
        
        for column in df.columns:
            # Get first non-null value for detection
            non_null = df[column].dropna()
            first_val = non_null.iloc[0] if not non_null.empty else ""

            # Improved detection logic
            field_type = 'text'

            if pd.api.types.is_numeric_dtype(df[column]):
                field_type = 'number'
            elif pd.api.types.is_bool_dtype(df[column]):
                field_type = 'boolean'
            elif pd.api.types.is_datetime64_any_dtype(df[column]):
                field_type = 'datetime'
            elif isinstance(first_val, str):
                val_lower = first_val.lower()
                if '@' in first_val and '.' in first_val:
                    field_type = 'email'
                elif val_lower.startswith(('http://', 'https://')):
                    field_type = 'url'
                elif val_lower in ['true', 'false', 'yes', 'no']:
                    field_type = 'boolean'
                elif self._is_date(first_val):
                    field_type = 'date'
            
            schema.append({
                'name': str(column),
                'type': field_type,
                'required': False
            })
        
        return schema
    
    def _is_date(self, value):
        """Check if string is a date"""
        from django.utils.dateparse import parse_date
        try:
            return parse_date(str(value)) is not None
        except:
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
            # Regenerate: wipe previously auto-generated widgets
            dashboard.widgets.filter(title__startswith='[Auto]').delete()

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
            if total_count >= 30:
                break
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

        logger.info(f"Generated {total_count} widgets across {tables.count()} tables")
        return dashboard

    # ------------------------------------------------------------------ #
    #  Data profiling + insight spec generation
    # ------------------------------------------------------------------ #
    def _generate_table_insights(self, table):
        """
        Profile actual record data and return a list of insight specs.

        Every spec is a dict with:
          type, title, width, height, query_config, viz_config

        The query_config follows the QueryEngine contract so widgets render
        without any hardcoded data.
        """
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

        # ── Classify schema fields ──────────────────────────────────────
        numeric_fields = [n for n, t in schema.items() if t in self.NUMERIC_TYPES]
        date_fields    = [n for n, t in schema.items() if t in self.DATE_TYPES]
        text_fields    = [n for n, t in schema.items() if t in self.TEXT_TYPES]

        # Keep only numeric fields that actually contain numbers in the data,
        # then rank them so the most meaningful field (revenue/total/amount)
        # is used as the primary metric throughout the dashboard.
        valid_numeric = self._filter_numeric_fields(numeric_fields, sample)
        valid_numeric = sorted(
            valid_numeric,
            key=lambda f: self._score_field_for_kpi(f, schema.get(f, 'number')),
            reverse=True,
        )
        # Primary value field — highest-scored numeric (e.g. Total KES, Revenue)
        primary_value = valid_numeric[0] if valid_numeric else None

        # Classify text fields by cardinality, then rank by chart usefulness.
        # very_low_card (2-8 unique)  → ideal for pie charts (also used in bar)
        # low_card      (9-25 unique) → bar charts only
        very_low_card, low_card = [], []
        for field in text_fields:
            unique = self._unique_values(field, sample)
            n = len(unique)
            if 2 <= n <= 8:
                very_low_card.append((field, n))   # (name, cardinality)
            elif n <= 25:
                low_card.append(field)

        # For bar charts: sort purely by business relevance score.
        # For pie charts: composite score = relevance - (cardinality / 10)
        # so Status (2 values, score 3) beats Category (6 values, score 5)
        # as a pie candidate: 3-0.2=2.8 vs 5-0.6=4.4 … actually Category still
        # wins here. Use a stronger cardinality bonus: relevance * 2 - cardinality.
        very_low_card_for_bar  = sorted([f for f, _ in very_low_card],
                                        key=self._score_field_for_chart, reverse=True)
        very_low_card_for_pie  = sorted(very_low_card,
                                        key=lambda x: self._score_field_for_chart(x[0]) * 2 - x[1],
                                        reverse=True)
        very_low_card_for_pie  = [f for f, _ in very_low_card_for_pie]

        low_card = sorted(low_card, key=self._score_field_for_chart, reverse=True)

        # All categorical fields for bar charts (best-ranked first)
        all_categorical = very_low_card_for_bar + low_card

        # Primary date field (first one detected)
        primary_date = date_fields[0] if date_fields else None
        group_by     = primary_date if primary_date else 'created_at_date'
        date_label   = self._clean_display_name(primary_date) if primary_date else 'Date'

        insights = []
        color_i  = 0

        # ── KPI row (up to 4 cards, always width=3) ──────────────────────
        #
        # Card 1: Total Transactions (always useful regardless of data type)
        insights.append(self._kpi_spec('Total Transactions', 'count', None, color_i))
        color_i += 1

        # Card 2: Sum of primary value field (e.g. Total Revenue)
        if primary_value:
            display_pv = self._clean_display_name(primary_value)
            prefix_pv  = self._currency_prefix(primary_value, schema.get(primary_value))
            insights.append(
                self._kpi_spec(f'Total {display_pv}', 'sum', primary_value,
                               color_i, prefix=prefix_pv)
            )
            color_i += 1

        # Card 3: Average of primary value field (e.g. Avg Revenue per transaction)
        if primary_value:
            insights.append(
                self._kpi_spec(f'Avg {display_pv}', 'avg', primary_value,
                               color_i, prefix=prefix_pv)
            )
            color_i += 1

        # Card 4: Second meaningful numeric field (skip fields with negative score
        # and skip fields whose display name is semantically redundant with the
        # primary value — e.g. 'Subtotal' when primary is 'Total / Revenue').
        primary_display_lower = display_pv.lower() if primary_value else ''
        secondary_fields = [
            f for f in valid_numeric[1:]
            if self._score_field_for_kpi(f, schema.get(f, 'number')) >= 0
            and f != primary_value
            and self._clean_display_name(f).lower() not in (primary_display_lower, 'revenue', 'total')
            and primary_display_lower not in self._clean_display_name(f).lower()
        ]
        if secondary_fields:
            sec = secondary_fields[0]
            display_sec = self._clean_display_name(sec)
            prefix_sec  = self._currency_prefix(sec, schema.get(sec))
            insights.append(
                self._kpi_spec(f'Total {display_sec}', 'sum', sec,
                               color_i, prefix=prefix_sec)
            )
            color_i += 1

        # ── Trend line charts ─────────────────────────────────────────────
        # Line 1: primary value over time  (e.g. Revenue Over Time)
        if primary_value:
            insights.append({
                'type': 'line_chart',
                'title': f'{display_pv} Over Time',
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [
                    {'type': 'sum', 'field': primary_value,
                     'group_by': group_by, 'name': 'val'}
                ]},
                'viz_config': {'x_axis': date_label, 'y_axis': display_pv},
            })

        # Line 2: transaction count over time (always informative)
        insights.append({
            'type': 'line_chart',
            'title': 'Transactions Over Time',
            'width': 6, 'height': 4,
            'query_config': {'aggregations': [
                {'type': 'count', 'group_by': group_by, 'name': 'val'}
            ]},
            'viz_config': {'x_axis': date_label, 'y_axis': 'Transactions'},
        })

        # ── Bar charts (up to 4 most meaningful categorical fields) ───────
        for field in all_categorical[:4]:
            display_field = self._clean_display_name(field)
            if primary_value:
                agg   = {'type': 'sum', 'field': primary_value,
                         'group_by': field, 'name': 'val'}
                title = f'{display_pv} by {display_field}'
                y_lbl = display_pv
            else:
                agg   = {'type': 'count', 'group_by': field, 'name': 'val'}
                title = f'{display_field} Distribution'
                y_lbl = 'Count'

            insights.append({
                'type': 'bar_chart',
                'title': title,
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [agg]},
                'viz_config': {'x_axis': display_field, 'y_axis': y_lbl},
            })

        # ── Pie charts (up to 2 very-low-cardinality fields, 2–8 values) ──
        for field in very_low_card_for_pie[:2]:
            display_field = self._clean_display_name(field)
            if primary_value:
                agg   = {'type': 'sum', 'field': primary_value,
                         'group_by': field, 'name': 'val'}
                title = f'{display_pv} by {display_field}'
            else:
                agg   = {'type': 'count', 'group_by': field, 'name': 'val'}
                title = f'{display_field} Breakdown'

            insights.append({
                'type': 'pie_chart',
                'title': title,
                'width': 6, 'height': 4,
                'query_config': {'aggregations': [agg]},
                'viz_config': {},
            })

        return insights

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _kpi_spec(self, title, agg_type, field, color_i, prefix='', suffix=''):
        """Build a metric widget spec with branded icon/colour."""
        agg = {'type': agg_type, 'name': 'val'}
        if field:
            agg['field'] = field
        color = self._KPI_COLORS[color_i % len(self._KPI_COLORS)]
        icon  = self._KPI_ICONS[color_i  % len(self._KPI_ICONS)]
        return {
            'type': 'metric',
            'title': title,
            'width': 3, 'height': 2,
            'query_config': {'aggregations': [agg]},
            'viz_config': {'icon': icon, 'color': color, 'prefix': prefix, 'suffix': suffix},
        }

    def _score_field_for_kpi(self, field_name, field_type='number'):
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
        """
        name = field_name.lower()
        score = 0
        if any(k in name for k in ('revenue', 'sales', 'income', 'earnings')):
            score += 5
        elif 'total' in name and 'subtotal' not in name and 'sub total' not in name:
            score += 5
        elif 'subtotal' in name or 'sub total' in name:
            score -= 1  # always redundant when a 'total' field exists
        elif any(k in name for k in ('amount', 'value', 'price') ) and \
                not any(k in name for k in ('unit price', 'price per', 'rate per', 'cost per')):
            score += 3
        if any(k in name for k in ('quantity', 'qty', 'units', 'pieces', 'count')):
            score += 1
        if any(k in name for k in ('percent', ' %', '%', 'rate', 'ratio', 'discount', 'tax')):
            score -= 6
        if any(k in name for k in ('unit price', 'price per', 'cost per', 'rate per')):
            score -= 4
        return score

    def _score_field_for_chart(self, field_name):
        """
        Return an integer score for how useful a text field is as a chart dimension.
        Higher = more meaningful breakdown axis.

        Fields like 'Category', 'Species', 'Staff', 'Payment Method', 'Status'
        score highly.  ID / name / description fields score negatively.
        """
        name = field_name.lower()
        score = 0
        if any(k in name for k in ('category', 'type', 'kind', 'class', 'group')):
            score += 5
        if any(k in name for k in ('species', 'breed', 'product', 'service', 'item', 'department')):
            score += 4
        if any(k in name for k in ('staff', 'agent', 'employee', 'rep', 'handler',
                                   'doctor', 'vet', 'nurse', 'technician', 'assigned')):
            score += 4
        if any(k in name for k in ('payment', 'method', 'channel', 'mode', 'medium')):
            score += 3
        if any(k in name for k in ('status', 'state', 'stage', 'result', 'outcome')):
            score += 3
        if any(k in name for k in ('region', 'location', 'area', 'zone', 'branch',
                                   'store', 'outlet', 'site')):
            score += 3
        if any(k in name for k in ('gender', 'sex', 'age group', 'tier', 'segment')):
            score += 2
        # Penalise identifier / free-text fields
        if any(k in name for k in ('id', ' ref', 'reference', 'code', 'number',
                                   'name', 'description', 'note', 'comment',
                                   'remark', 'detail', 'info')):
            score -= 4
        return score

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
        name = re.sub(r'\s*\([^)]*\)', '', field_name).strip()   # strip (units)
        name = re.sub(r'[\s%/\\|]+$', '', name).strip()          # strip trailing symbols
        name = name.replace('_', ' ').strip()
        # Avoid generic single-word names that produce double-prefixed KPI titles
        if name.lower() == 'total':
            return 'Revenue'
        if name.lower() == 'amount':
            return 'Amount'
        return name if name else field_name

    def _currency_prefix(self, field_name, field_type=None):
        """
        Return a short currency prefix for KPI cards based on field name / type.
        'Total (KES)' → 'KES '   'Revenue ($)' → '$'   fallback → ''
        """
        name = (field_name or '').lower()
        if 'kes' in name:
            return 'KES '
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