import json
import pandas as pd
import numpy as np
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from typing import Dict, List, Any, Optional
import hashlib
import logging

logger = logging.getLogger(__name__)

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
        try:
            # Generate cache key based on widget config and table
            cache_key = self._generate_cache_key(widget)

            # Try to get from cache
            cached_data = cache.get(cache_key)
            if cached_data:
                logger.debug(f"Cache hit for widget {widget.id}")
                return cached_data

            # Use limit from config if provided
            query_limit = widget.query_config.get('limit', limit)

            # Execute query
            data = self._execute_query(
                table_id=widget.table_id,
                config=widget.query_config,
                limit=query_limit
            )

            # Cache result
            cache.set(cache_key, data, self.cache_timeout)

            return data
        except Exception as e:
            logger.error(f"Error executing widget query: {str(e)}", exc_info=True)
            return {"error": str(e)}
    
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
        
        try:
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
            records = list(queryset.values('id', 'data', 'created_at')[:limit])
            
            if not records:
                logger.debug(f"No records found for table {table_id}")
                return {"message": "No data available"}
            
            # Convert to pandas DataFrame for analysis
            df = self._records_to_dataframe(records)
            
            # Apply aggregations
            if 'aggregations' in config:
                result = self._apply_aggregations(df, config['aggregations'])
            else:
                # Return raw data
                result = df.to_dict('records')
            
            return result
            
        except Exception as e:
            logger.error(f"Query execution error: {str(e)}", exc_info=True)
            return {"error": f"Failed to execute query: {str(e)}"}
    
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
            row = record['data'].copy() if record['data'] else {}
            row['_record_id'] = str(record['id'])
            row['_created_at'] = record['created_at']
            data_list.append(row)
    
        if data_list:
            df = pd.json_normalize(data_list)
            # Convert _created_at to datetime
            if '_created_at' in df.columns:
                df['_created_at'] = pd.to_datetime(df['_created_at'])
            return df
        return pd.DataFrame()
    
    def _apply_aggregations(self, df, aggregations):
        """Apply aggregations to DataFrame"""
        result = {}
    
        for agg in aggregations:
            agg_type = agg.get('type')
            field = agg.get('field')
            group_by = agg.get('group_by')
            agg_name = agg.get('name', 'val')
        
            # Handle empty data
            if df.empty:
                if agg_type == 'count':
                    result[agg_name] = 0
                else:
                    result[agg_name] = None
                continue

            try:
                if group_by:
                    # Handle special case for created_at_date
                    if group_by == 'created_at_date' and '_created_at' in df.columns:
                        # Extract date from _created_at
                        df['created_at_date'] = pd.to_datetime(df['_created_at']).dt.date
                        group_by = 'created_at_date'
                    # Check if group_by field exists
                    elif group_by not in df.columns:
                        # For date fields, try to find any date column
                        date_cols = [col for col in df.columns if 'date' in col.lower() or 'time' in col.lower() or col == '_created_at']
                        if date_cols:
                            # Use the first date column
                            date_col = date_cols[0]
                            if date_col == '_created_at':
                                df['created_at_date'] = pd.to_datetime(df['_created_at']).dt.date
                                group_by = 'created_at_date'
                            else:
                                group_by = date_col
                        else:
                            # If no date field, don't group - return count
                            if agg_type == 'count':
                                result[agg_name] = len(df)
                            elif field in df.columns:
                                if agg_type == 'sum':
                                    result[agg_name] = float(df[field].sum())
                                elif agg_type == 'avg':
                                    result[agg_name] = float(df[field].mean())
                            else:
                                result[agg_name] = len(df)
                            continue
                
                    # Group by operation
                    if agg_type == 'sum':
                        if field in df.columns:
                            grouped = df.groupby(group_by)[field].sum()
                        else:
                            grouped = df.groupby(group_by).size()
                    elif agg_type == 'avg':
                        if field in df.columns:
                            grouped = df.groupby(group_by)[field].mean()
                        else:
                            grouped = df.groupby(group_by).size()
                    elif agg_type == 'count':
                        grouped = df.groupby(group_by).size()
                    elif agg_type == 'min':
                        if field in df.columns:
                            grouped = df.groupby(group_by)[field].min()
                        else:
                            grouped = df.groupby(group_by).size()
                    elif agg_type == 'max':
                        if field in df.columns:
                            grouped = df.groupby(group_by)[field].max()
                        else:
                            grouped = df.groupby(group_by).size()
                    else:
                        grouped = df.groupby(group_by).size()
                
                    # Convert to dict with proper handling of non-string keys
                    # Convert dates to strings for JSON serialization
                    result[agg_name] = {str(k): float(v) if isinstance(v, (np.integer, np.floating)) else v 
                                   for k, v in grouped.to_dict().items()}
                else:
                    # Simple aggregation
                    if agg_type == 'sum':
                        result[agg_name] = float(df[field].sum()) if field in df.columns and not df[field].empty else 0
                    elif agg_type == 'avg':
                        result[agg_name] = float(df[field].mean()) if field in df.columns and not df[field].empty else 0
                    elif agg_type == 'count':
                        result[agg_name] = len(df)
                    elif agg_type == 'min':
                        result[agg_name] = float(df[field].min()) if field in df.columns and not df[field].empty else 0
                    elif agg_type == 'max':
                        result[agg_name] = float(df[field].max()) if field in df.columns and not df[field].empty else 0
                    else:
                        result[agg_name] = len(df)
            except Exception as e:
                print(f"Aggregation error: {str(e)}")
                import traceback
                traceback.print_exc()
                result[agg_name] = {"error": str(e)}
    
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
    Automatically generate insights and dashboards from workspace tables
    """

    def generate_workspace_overview(self, workspace, user):
        """
        Scan all tables in the workspace and create an insights dashboard
        """
        from .models import Dashboard, DataTable, Widget
        import logging
        logger = logging.getLogger(__name__)
        
        logger.info(f"Generating insights for workspace {workspace.id}")

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
        
        logger.info(f"Dashboard {'created' if created else 'exists'}: {dashboard.id}")

        # Always clear to regenerate the best insights
        dashboard.widgets.all().delete()
        logger.info("Cleared existing widgets")

        # 2. Analyze all active tables
        tables = workspace.tables.filter(is_active=True)
        logger.info(f"Found {tables.count()} tables")
        
        pos_x, pos_y = 0, 0
        widget_count = 0

        for table in tables:
            # Skip tables with no data
            record_count = table.records.filter(is_active=True).count()
            if record_count == 0:
                logger.info(f"Skipping table {table.name} - no records")
                continue
            
            logger.info(f"Processing table {table.name} with {record_count} records")
            
            schema = table.schema or []
            numeric_fields = [f for f in schema if f['type'] in ['number', 'currency', 'percentage']]
            date_fields = [f for f in schema if f['type'] in ['date', 'datetime']]
            
            logger.info(f"Table {table.name}: {len(numeric_fields)} numeric fields, {len(date_fields)} date fields")

            # GUARANTEE: Record Count Metric
            Widget.objects.create(
                dashboard=dashboard,
                widget_type='metric',
                title=f"Total Records ({table.name})",
                table=table,
                query_config={
                    "aggregations": [
                        {"type": "count", "field": "id", "name": "val"}
                    ]
                },
                viz_config={
                    "format": "number",
                    "prefix": "",
                    "suffix": ""
                },
                position={"x": pos_x, "y": pos_y, "w": 3, "h": 2}
            )
            widget_count += 1
            pos_x += 3
            if pos_x >= 12: 
                pos_x = 0
                pos_y += 2

            # 1. KPI Metrics for Numeric Fields
            for field in numeric_fields[:2]:  # Limit to first 2 numeric fields
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='metric',
                    title=f"Total {field['name']} ({table.name})",
                    table=table,
                    query_config={
                        "aggregations": [
                            {"type": "sum", "field": field['name'], "name": "val"}
                        ]
                    },
                    viz_config={
                        "format": "number",
                        "prefix": "$" if field['type'] == 'currency' else "",
                        "suffix": "%" if field['type'] == 'percentage' else ""
                    },
                    position={"x": pos_x, "y": pos_y, "w": 3, "h": 2}
                )
                widget_count += 1
                pos_x += 3
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 2

            # 2. Categorical Discovery
            cat_field = self._sample_and_detect_categorical(table)
            if cat_field:
                logger.info(f"Found categorical field: {cat_field}")
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='bar_chart',
                    title=f"Records by {cat_field} ({table.name})",
                    table=table,
                    query_config={
                        "aggregations": [
                            {
                                "type": "count", 
                                "field": "id", 
                                "group_by": cat_field, 
                                "name": "val"
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": cat_field,
                        "y_axis": "val",
                        "show_legend": False
                    },
                    position={"x": pos_x, "y": pos_y, "w": 6, "h": 4}
                )
                widget_count += 1
                pos_x += 6
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 4

            # 3. Temporal Discovery (Trends)
            if date_fields:
                date_field = date_fields[0]['name']
                # If numeric exists, show trend of first numeric field, otherwise count
                if numeric_fields:
                    target_field = numeric_fields[0]['name']
                    agg_type = "sum"
                else:
                    target_field = "id"
                    agg_type = "count"

                logger.info(f"Creating trend chart: {agg_type} of {target_field} over {date_field}")
                
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='line_chart',
                    title=f"{target_field} over Time ({table.name})",
                    table=table,
                    query_config={
                        "aggregations": [
                            {
                                "type": agg_type, 
                                "field": target_field, 
                                "group_by": date_field, 
                                "name": "val"
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": date_field,
                        "y_axis": "val",
                        "show_legend": True
                    },
                    position={"x": pos_x, "y": pos_y, "w": 6, "h": 4}
                )
                widget_count += 1
                pos_x += 6
                if pos_x >= 12:
                    pos_x = 0
                    pos_y += 4

            # 4. Sample Data Table
            Widget.objects.create(
                dashboard=dashboard,
                widget_type='table',
                title=f"Sample: {table.name}",
                table=table,
                query_config={"limit": 10},
                viz_config={},
                position={"x": 0, "y": pos_y, "w": 12, "h": 4}
            )
            widget_count += 1
            pos_y += 4
            pos_x = 0

            # Stop if we have too many widgets
            if widget_count > 20:
                logger.info("Reached maximum widget count (20)")
                break
        
        logger.info(f"Generated {widget_count} widgets for dashboard {dashboard.id}")
        return dashboard

    def _sample_and_detect_categorical(self, table):
        """
        Sample table records to find the best categorical field
        """
        from .models import Record
        import pandas as pd
        
        records = Record.objects.filter(table=table, is_active=True).values('data')[:100]
        if not records:
            return None

        # Extract data and convert to DataFrame
        data_list = [r['data'] for r in records if r['data']]
        if not data_list:
            return None
            
        df = pd.DataFrame(data_list)

        # Look for text columns with low cardinality (unique values < 20% of sample)
        best_col = None
        for col in df.columns:
            if df[col].dtype == 'object':
                unique_count = df[col].nunique()
                if 1 < unique_count < 15:
                    # Exclude common non-categorical fields
                    exclude_list = ['id', 'email', 'name', 'first_name', 'last_name', 'phone', 
                                  'address', 'url', 'website', 'image', 'photo']
                    if col.lower() not in exclude_list:
                        best_col = col
                        break
        
        # Fallback: any column with few unique values
        if not best_col:
            for col in df.columns:
                if df[col].dtype == 'object' and 1 < df[col].nunique() < 20:
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