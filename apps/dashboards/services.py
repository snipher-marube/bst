import json
import pandas as pd
import numpy as np
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from typing import Dict, List, Any, Optional
import hashlib

class QueryEngine:
    """
    Execute queries against user data with caching and optimization
    """
    
    def __init__(self):
        self.cache_timeout = 300  # 5 minutes default cache
    
    def execute_widget_query(self, widget, limit=1000):
        """
        Execute a widget's query configuration and return formatted data
        """
        # Generate cache key based on widget config and table
        cache_key = self._generate_cache_key(widget)
        
        # Try to get from cache
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data
        
        # Execute query
        data = self._execute_query(
            table_id=widget.table_id,
            config=widget.query_config,
            limit=limit
        )
        
        # Cache result
        cache.set(cache_key, data, self.cache_timeout)
        
        return data
    
    def _generate_cache_key(self, widget):
        """Generate unique cache key for widget query"""
        from django.core.serializers.json import DjangoJSONEncoder
        key_data = {
            'table_id': str(widget.table_id),
            'query_config': widget.query_config,
            'widget_type': widget.widget_type,
        }
        key_str = json.dumps(key_data, sort_keys=True, cls=DjangoJSONEncoder)
        return f"widget_query:{hashlib.md5(key_str.encode()).hexdigest()}"
    
    def _execute_query(self, table_id, config, limit=1000):
        """
        Execute the actual query against records
        """
        from .models import Record
        
        # Start with base queryset
        queryset = Record.objects.filter(
            table_id=table_id,
            is_active=True
        ).select_related('table')
        
        # Apply filters
        if 'filters' in config:
            queryset = self._apply_filters(queryset, config['filters'])
        
        # Apply time range if specified
        if 'date_range' in config:
            queryset = self._apply_date_range(queryset, config['date_range'])
        
        # Get the data
        records = queryset.values('id', 'data', 'created_at')[:limit]
        
        # Convert to pandas DataFrame for analysis
        df = self._records_to_dataframe(records)
        
        # Apply aggregations
        if 'aggregations' in config:
            result = self._apply_aggregations(df, config['aggregations'])
        else:
            # Return raw data
            result = df.to_dict('records')
        
        return result
    
    def _apply_filters(self, queryset, filters):
        """Apply filters to queryset"""
        for filter_item in filters:
            field = filter_item.get('field')
            operator = filter_item.get('operator', 'eq')
            value = filter_item.get('value')
            
            if operator == 'eq':
                queryset = queryset.filter(**{f"data__{field}": value})
            elif operator == 'gt':
                queryset = queryset.filter(**{f"data__{field}__gt": value})
            elif operator == 'lt':
                queryset = queryset.filter(**{f"data__{field}__lt": value})
            elif operator == 'contains':
                queryset = queryset.filter(**{f"data__{field}__icontains": value})
            elif operator == 'in':
                queryset = queryset.filter(**{f"data__{field}__in": value})
        
        return queryset
    
    def _apply_date_range(self, queryset, date_range):
        """Apply date range filter"""
        field = date_range.get('field', 'created_at')
        start = date_range.get('start')
        end = date_range.get('end')
        
        if start:
            queryset = queryset.filter(**{f"{field}__gte": start})
        if end:
            queryset = queryset.filter(**{f"{field}__lte": end})
        
        return queryset
    
    def _records_to_dataframe(self, records):
        """Convert Django queryset to pandas DataFrame"""
        data_list = []
        for record in records:
            row = record['data'].copy()
            row['_record_id'] = record['id']
            row['_created_at'] = record['created_at']
            data_list.append(row)
        
        if data_list:
            return pd.DataFrame(data_list)
        return pd.DataFrame()
    
    def _apply_aggregations(self, df, aggregations):
        """Apply aggregations to DataFrame"""
        result = {}
        
        for agg in aggregations:
            agg_type = agg.get('type')
            field = agg.get('field')
            group_by = agg.get('group_by')
            
            if group_by:
                # Group by operation
                if agg_type == 'sum':
                    result[agg.get('name', 'sum')] = df.groupby(group_by)[field].sum().to_dict()
                elif agg_type == 'avg':
                    result[agg.get('name', 'avg')] = df.groupby(group_by)[field].mean().to_dict()
                elif agg_type == 'count':
                    result[agg.get('name', 'count')] = df.groupby(group_by).size().to_dict()
                elif agg_type == 'min':
                    result[agg.get('name', 'min')] = df.groupby(group_by)[field].min().to_dict()
                elif agg_type == 'max':
                    result[agg.get('name', 'max')] = df.groupby(group_by)[field].max().to_dict()
            else:
                # Simple aggregation
                if agg_type == 'sum':
                    result[agg.get('name', 'sum')] = df[field].sum()
                elif agg_type == 'avg':
                    result[agg.get('name', 'avg')] = df[field].mean()
                elif agg_type == 'count':
                    result[agg.get('name', 'count')] = len(df)
                elif agg_type == 'min':
                    result[agg.get('name', 'min')] = df[field].min()
                elif agg_type == 'max':
                    result[agg.get('name', 'max')] = df[field].max()
        
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
                        mapped_row = row
                    
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
            first_val = df[column].dropna().iloc[0] if not df[column].dropna().empty else ""

            # Improved detection logic
            field_type = 'text'

            if isinstance(first_val, (int, float, np.number)):
                field_type = 'number'
            elif isinstance(first_val, bool):
                field_type = 'boolean'
            elif self._is_date(str(first_val)):
                field_type = 'date'
            elif isinstance(first_val, str):
                val_lower = first_val.lower()
                if '@' in first_val and '.' in first_val:
                    field_type = 'email'
                elif val_lower.startswith(('http://', 'https://')):
                    field_type = 'url'
                elif val_lower in ['true', 'false', 'yes', 'no']:
                    field_type = 'boolean'
            
            schema.append({
                'name': str(column),
                'type': field_type,
                'required': False
            })
        
        return schema
    
    def _is_date(self, value):
        """Check if string is a date"""
        from django.utils.dateparse import parse_date
        return parse_date(str(value)) is not None


class WorkspaceInsightService:
    """
    Automatically generate insights and dashboards from workspace tables
    """

    def generate_workspace_overview(self, workspace, user):
        """
        Scan all tables in the workspace and create an insights dashboard
        """
        from .models import Dashboard, DataTable, Widget

        # 1. Create or get the "Workspace Overview" dashboard
        dashboard, created = Dashboard.objects.get_or_create(
            workspace=workspace,
            slug='workspace-overview',
            defaults={
                'name': 'Workspace Insights Overview',
                'description': 'Automatically generated insights from all your data tables.',
                'created_by': user,
                'layout_config': {"columns": 12, "rowHeight": 100, "compact": True}
            }
        )

        if not created:
            # Clear existing auto-generated widgets to refresh insights
            dashboard.widgets.all().delete()

        # 2. Analyze all active tables
        tables = workspace.tables.filter(is_active=True)
        pos_x, pos_y = 0, 0

        for table in tables:
            # Skip tables with no data
            if table.record_count == 0:
                continue

            # Identify fields for insights
            schema = table.schema or []
            numeric_fields = [f for f in schema if f['type'] in ['number', 'currency', 'percentage']]
            date_fields = [f for f in schema if f['type'] in ['date', 'datetime']]
            text_fields = [f for f in schema if f['type'] in ['text', 'category']]

            # 1. Metric Cards (KPIs)
            for field in numeric_fields[:3]:
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='metric',
                    title=f"Total {field['name']} ({table.name})",
                    table=table,
                    query_config={"aggregations": [{"type": "sum", "field": field['name'], "name": "val"}]},
                    viz_config={"format": "number", "prefix": "$" if field['type'] == 'currency' else "", "suffix": "%" if field['type'] == 'percentage' else ""},
                    position={"x": pos_x, "y": pos_y, "w": 3, "h": 2}
                )
                pos_x += 3
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 2

            # 2. Time Series (Trends)
            if date_fields and numeric_fields:
                date_field = date_fields[0]['name']
                num_field = numeric_fields[0]['name']

                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='line_chart',
                    title=f"{num_field} over Time",
                    table=table,
                    query_config={"aggregations": [{"type": "sum", "field": num_field, "group_by": date_field}]},
                    viz_config={"x_axis": date_field, "y_axis": "sum", "show_legend": True},
                    position={"x": pos_x, "y": pos_y, "w": 6, "h": 4}
                )
                pos_x += 6
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 4

            # 3. Categorical Insights (Pie/Bar)
            cat_field = self._sample_and_detect_categorical(table)

            if cat_field and numeric_fields:
                num_field = numeric_fields[0]['name']

                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='pie_chart',
                    title=f"{num_field} by {cat_field}",
                    table=table,
                    query_config={"aggregations": [{"type": "sum", "field": num_field, "group_by": cat_field, "name": "val"}]},
                    viz_config={"x_axis": cat_field, "y_axis": "val", "show_legend": True},
                    position={"x": pos_x, "y": pos_y, "w": 6, "h": 4}
                )
                pos_x += 6
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 4

            # Reset positions for next table to avoid too much verticality if many tables
            if pos_y > 20:
                pos_x = 0
                pos_y = 0

        return dashboard

    def _sample_and_detect_categorical(self, table):
        """
        Sample table records to find the best categorical field
        """
        from .models import Record
        records = Record.objects.filter(table=table, is_active=True).values('data')[:100]
        if not records:
            return None

        df = pd.DataFrame([r['data'] for r in records])

        # Look for text columns with low cardinality (unique values < 20% of sample)
        best_col = None
        for col in df.columns:
            if df[col].dtype == 'object':
                unique_count = df[col].nunique()
                if 1 < unique_count < 15:
                    # Exclude common non-categorical fields
                    if col.lower() not in ['id', 'email', 'name', 'first_name', 'last_name', 'phone']:
                        best_col = col
                        break
        return best_col


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