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
        key_data = {
            'table_id': str(widget.table_id),
            'query_config': widget.query_config,
            'widget_type': widget.widget_type,
        }
        key_str = json.dumps(key_data, sort_keys=True)
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
        records = queryset.values('data', 'created_at')[:limit]
        
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
    
    def import_csv(self, table, file_obj, user, mapping=None):
        """
        Import CSV data into a table
        """
        import csv
        import io
        from django.db import transaction
        
        # Read CSV
        decoded_file = file_obj.read().decode('utf-8')
        io_string = io.StringIO(decoded_file)
        reader = csv.DictReader(io_string)
        
        # Auto-detect schema if not provided
        if not table.schema and not mapping:
            schema = self._detect_schema_from_csv(reader.fieldnames, next(reader))
            table.schema = schema
            table.save()
        
        # Import rows
        success_count = 0
        error_count = 0
        errors = []
        
        with transaction.atomic():
            for row_num, row in enumerate(reader, start=1):
                try:
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
            'error_details': errors[:10]  # First 10 errors
        }
    
    def _detect_schema_from_csv(self, headers, first_row):
        """
        Automatically detect field types from CSV data
        """
        schema = []
        
        for header in headers:
            value = first_row.get(header, '')
            
            # Detect type
            if value.replace('.', '').replace('-', '').isdigit():
                field_type = 'number'
            elif self._is_date(value):
                field_type = 'date'
            elif value.lower() in ['true', 'false', 'yes', 'no']:
                field_type = 'boolean'
            elif '@' in value and '.' in value:
                field_type = 'email'
            else:
                field_type = 'text'
            
            schema.append({
                'name': header,
                'type': field_type,
                'required': False
            })
        
        return schema
    
    def _is_date(self, value):
        """Check if string is a date"""
        from django.utils.dateparse import parse_date
        return parse_date(str(value)) is not None


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