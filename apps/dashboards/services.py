import json
import pandas as pd
import numpy as np
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
        self.cache_timeout = 300


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
            elif operator == 'lt':
                queryset = queryset.filter(**{f'data__{field}__lt': value})
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
            if widget.widget_type == 'metric':
                data = self._execute_metric_query(widget, limit)
            elif widget.widget_type == 'table':
                data = self._execute_table_query(widget, limit)
            elif widget.widget_type in ['line_chart', 'bar_chart', 'pie_chart']:
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
                # We need to aggregate JSON field - this is tricky
                # For now, get all records and compute in Python
                records = queryset.values_list('data', flat=True)[:10000]  # Limit for performance
                
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
        """Execute chart widget query - return properly formatted data for charts"""
        from .models import Record
        import pandas as pd
        from collections import defaultdict
        from datetime import datetime
    
        config = widget.query_config or {}
        aggregations = config.get('aggregations', [])
    
        if not aggregations:
            return {"error": "No aggregations configured"}
    
        # Get records
        queryset = Record.objects.filter(
            table=widget.table,
            is_active=True
        )
    
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
    
        # Get records with their data
        records = list(queryset.values('data', 'created_at')[:limit])
    
        if not records:
            return {"message": "No data", "val": {}}
    
        # Process aggregations
        result = {}
        for agg in aggregations:
            agg_type = agg.get('type', 'count')
            field = agg.get('field')
            group_by = agg.get('group_by')
            name = agg.get('name', 'val')
        
            try:
                if group_by:
                    # Handle group by - this is for charts
                    grouped_data = defaultdict(int)
                
                    for record in records:
                        record_data = record['data'] or {}
                    
                        # Get group value
                        if group_by == 'created_at' or group_by == '_created_at':
                            # Group by date from created_at field
                            group_value = record['created_at'].date().isoformat()
                        elif group_by == 'created_at_date':
                            group_value = record['created_at'].date().isoformat()
                        elif group_by in record_data:
                            group_value = str(record_data[group_by])
                        else:
                            continue
                    
                        # Get value to aggregate
                        if agg_type == 'count':
                            value = 1
                        elif field and field in record_data:
                            try:
                                value = float(record_data[field])
                            except (ValueError, TypeError):
                                value = 1 if agg_type == 'count' else 0
                        else:
                            value = 1 if agg_type == 'count' else 0
                    
                        grouped_data[group_value] += value
                
                    # Convert to the format expected by frontend
                    if grouped_data:
                        # Sort by key (especially important for dates)
                        sorted_items = sorted(grouped_data.items())
                        result[name] = {
                            str(k): float(v) for k, v in sorted_items
                        }
                    else:
                        result[name] = {}
                else:
                    # Simple aggregation (for metrics)
                    if agg_type == 'count':
                        result[name] = len(records)
                    elif field:
                        total = 0
                        count = 0
                        for record in records:
                            record_data = record['data'] or {}
                            if field in record_data:
                                try:
                                    val = float(record_data[field])
                                    if agg_type == 'sum':
                                        total += val
                                    elif agg_type == 'avg':
                                        total += val
                                        count += 1
                                    elif agg_type == 'min':
                                        if 'min' not in locals() or val < min_val:
                                            min_val = val
                                    elif agg_type == 'max':
                                        if 'max' not in locals() or val > max_val:
                                            max_val = val
                                except (ValueError, TypeError):
                                    pass
                    
                        if agg_type == 'sum':
                            result[name] = total
                        elif agg_type == 'avg':
                            result[name] = total / count if count > 0 else 0
                        elif agg_type == 'min':
                            result[name] = min_val if 'min_val' in locals() else 0
                        elif agg_type == 'max':
                            result[name] = max_val if 'max_val' in locals() else 0
                
            except Exception as e:
                logger.error(f"Aggregation error: {str(e)}")
                result[name] = {"error": str(e)}
    
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
        Import data from a DataFrame into a table
        """
        from django.db import transaction
        from .models import Record
        
        # Auto-detect schema if not provided and table has no schema
        if not table.schema and not mapping:
            table.schema = self._detect_schema_from_df(df)
            table.save()
        
        success_count = 0
        error_count = 0
        errors = []
        
        # Replace NaN with None for JSON compatibility
        df = df.replace({np.nan: None})

        # Convert DF to list of dicts
        records_data = df.to_dict('records')

        for row_num, row in enumerate(records_data, start=1):
            try:
                with transaction.atomic():
                    # Apply field mapping if provided
                    if mapping:
                        mapped_row = {}
                        for target_field, source_field in mapping.items():
                            if source_field in row:
                                mapped_row[target_field] = row[source_field]
                    else:
                        mapped_row = dict(row)

                    # Ensure all values are JSON-serializable Python native types
                    mapped_row = self._sanitize_row(mapped_row)

                    # Create record
                    Record.objects.create(
                        table=table,
                        data=mapped_row,
                        created_by=user
                    )
                    success_count += 1

            except Exception as e:
                error_count += 1
                errors.append(f"Row {row_num}: {str(e)}")
        
        return {
            'success': success_count,
            'errors': error_count,
            'error_details': errors[:10]
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

        # 5. Attempt to parse date-like string columns — store as ISO strings
        for col in df.select_dtypes(include='object').columns:
            sample = df[col].dropna().head(20)
            if len(sample) == 0:
                continue
            try:
                pd.to_datetime(sample, infer_datetime_format=True, errors='raise')
                # Keep original values as backup before conversion
                original = df[col].copy()
                parsed = pd.to_datetime(df[col], infer_datetime_format=True, errors='coerce')
                nat_count = parsed.isna().sum() - df[col].isna().sum()
                if nat_count / max(len(df), 1) < 0.2:
                    # Store as ISO date strings so JSON serialisation is safe
                    df[col] = parsed.dt.strftime('%Y-%m-%d').where(parsed.notna(), None)
                    report.append(f"Standardised '{col}' to ISO date format (YYYY-MM-DD)")
                # else: leave original strings intact
            except Exception:
                pass

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
    Automatically generate insights from workspace tables
    """
    
    def generate_workspace_overview(self, workspace, user):
        from .models import Dashboard, DataTable, Widget
        from apps.insights.generators.sales_insights import SalesInsightGenerator
        
        logger.info(f"Generating insights for workspace {workspace.id}")
        
        # Create or get the overview dashboard
        dashboard, created = Dashboard.objects.get_or_create(
            workspace=workspace,
            slug='workspace-overview',
            defaults={
                'name': 'Workspace Insights',
                'description': 'AI-powered insights from all your data',
                'created_by': user,
                'layout_config': {
                    "columns": 12,
                    "rowHeight": 100,
                    "compact": True,
                    "margin": 10
                }
            }
        )
        
        if not created:
            # Clear existing auto-generated widgets
            dashboard.widgets.filter(title__startswith='[Auto]').delete()
        
        # Analyze each table
        tables = workspace.tables.filter(is_active=True, record_count__gt=0)
        pos_x, pos_y = 0, 0
        widget_count = 0
        
        for table in tables:
            logger.info(f"Analyzing table: {table.name}")
            
            # Get schema-based insights
            insights = self._generate_table_insights(table)
            
            # Add widgets for each insight
            for insight in insights:
                if pos_x + insight.get('width', 3) > 12:
                    pos_x = 0
                    pos_y += insight.get('height', 4)
                
                widget = Widget.objects.create(
                    dashboard=dashboard,
                    widget_type=insight['type'],
                    title=f"[Auto] {insight['title']}",
                    table=table,
                    query_config=insight.get('query_config', {}),
                    viz_config=insight.get('viz_config', {}),
                    position={
                        'x': pos_x,
                        'y': pos_y,
                        'w': insight.get('width', 3),
                        'h': insight.get('height', 4)
                    }
                )
                
                pos_x += insight.get('width', 3)
                widget_count += 1
                
                if widget_count >= 20:
                    break
            
            # Add table sample widget
            if widget_count < 20:
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='table',
                    title=f"[Auto] {table.name} Sample",
                    table=table,
                    query_config={"limit": 10},
                    viz_config={},
                    position={"x": 0, "y": pos_y + 4, "w": 12, "h": 4}
                )
                widget_count += 1
                pos_y += 8
        
        logger.info(f"Generated {widget_count} widgets")
        return dashboard
    
    def _generate_table_insights(self, table):
        """Generate insights based on table schema"""
        schema = {f['name']: f['type'] for f in table.schema}
        insights = []
        
        # Check if it's a sales pipeline
        if 'Deal Value' in schema and 'Status' in schema:
            generator = SalesInsightGenerator(table.workspace)
            sales_insights = generator.analyze_sales_pipeline(table)
            if sales_insights:
                return sales_insights
        
        # Generic insights for other tables
        # Always include record count
        insights.append({
            'type': 'metric',
            'title': 'Total Records',
            'width': 3,
            'height': 2,
            'query_config': {
                'aggregations': [{'type': 'count', 'name': 'val'}]
            },
            'viz_config': {
                'format': 'number'
            }
        })
        
        # Add date-based insights if available
        date_fields = [name for name, type in schema.items() if type in ['date', 'datetime']]
        if date_fields:
            insights.append({
                'type': 'line_chart',
                'title': f'Records Over Time',
                'width': 6,
                'height': 4,
                'query_config': {
                    'aggregations': [{
                        'type': 'count',
                        'group_by': date_fields[0],
                        'name': 'val'
                    }]
                },
                'viz_config': {
                    'x_axis': date_fields[0],
                    'y_axis': 'val'
                }
            })
        
        # Add categorical insights
        text_fields = [name for name, type in schema.items() if type == 'text']
        for field in text_fields[:2]:  # Limit to 2 categorical fields
            insights.append({
                'type': 'bar_chart',
                'title': f'Records by {field}',
                'width': 6,
                'height': 4,
                'query_config': {
                    'aggregations': [{
                        'type': 'count',
                        'group_by': field,
                        'name': 'val'
                    }]
                },
                'viz_config': {
                    'x_axis': field,
                    'y_axis': 'val'
                }
            })
        
        return insights
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