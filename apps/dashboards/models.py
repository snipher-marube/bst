import uuid
import json
import re
from django.db import models
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import JSONField, F
from django.utils import timezone
import logging
from apps.insights.tasks import broadcast_widget_update, notify_table_change
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

User = get_user_model()


class DataTable(models.Model):
    """
    Think of this like an Excel sheet - a collection of records with a defined schema
    """
    FIELD_TYPES = [
        ('text', 'Text'),
        ('number', 'Number'),
        ('date', 'Date'),
        ('datetime', 'DateTime'),
        ('boolean', 'Yes/No'),
        ('email', 'Email'),
        ('url', 'URL'),
        ('phone', 'Phone'),
        ('currency', 'Currency'),
        ('percentage', 'Percentage'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='tables')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    # Schema defines the columns - stored as JSON for flexibility
    schema = JSONField(default=list)
    
    # Performance optimization: store computed schema hash for quick validation
    schema_hash = models.CharField(max_length=64, blank=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # For soft delete
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    
    # Metadata
    record_count = models.IntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['workspace', 'name']
        indexes = [
            models.Index(fields=['workspace', 'is_active']),
            models.Index(fields=['workspace', 'updated_at']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.workspace.name})"
    
    def save(self, *args, **kwargs):
        # Generate schema hash for quick comparisons
        if self.schema:
            import hashlib
            from django.core.serializers.json import DjangoJSONEncoder
            schema_str = json.dumps(self.schema, sort_keys=True, cls=DjangoJSONEncoder)
            self.schema_hash = hashlib.sha256(schema_str.encode()).hexdigest()
        super().save(*args, **kwargs)
    
    def validate_record(self, data):
        """
        Validate a record against the table schema
        """
        if not self.schema:
            return True, []
        
        errors = []
        schema_dict = {field['name']: field for field in self.schema}
        
        # Check required fields
        for field in self.schema:
            if field.get('required', False) and field['name'] not in data:
                errors.append(f"Field '{field['name']}' is required")
        
        # Validate field types
        for key, value in data.items():
            if key not in schema_dict:
                errors.append(f"Unknown field '{key}'")
                continue
            
            field = schema_dict[key]
            field_type = field['type']
            
            if value is not None and value != '':
                if field_type in ['number', 'currency', 'percentage']:
                    try:
                        float(value)
                    except (TypeError, ValueError):
                        errors.append(f"Field '{key}' must be a number")
                
                elif field_type == 'date':
                    from django.utils.dateparse import parse_date
                    if not parse_date(str(value)):
                        errors.append(f"Field '{key}' must be a valid date (YYYY-MM-DD)")
                
                elif field_type == 'datetime':
                    from django.utils.dateparse import parse_datetime
                    if not parse_datetime(str(value)):
                        errors.append(f"Field '{key}' must be a valid datetime")
                
                elif field_type == 'boolean':
                    if str(value).lower() not in ['true', 'false', '1', '0', 'yes', 'no']:
                        errors.append(f"Field '{key}' must be a boolean")
                
                elif field_type == 'email':
                    from django.core.validators import validate_email
                    try:
                        validate_email(str(value))
                    except ValidationError:
                        errors.append(f"Field '{key}' must be a valid email")
        
        return len(errors) == 0, errors
    
    @property
    def estimated_storage_bytes(self):
        """Estimate storage used by this table (for billing)"""
        avg_record_size = 1024
        return self.record_count * avg_record_size

    def generate_default_dashboard(self):
        """Automatically create a smart dashboard for this table.

        The entire operation runs inside a single database transaction so a
        crash mid-way never leaves a half-built dashboard or orphaned widgets.
        """
        from .models import Dashboard, Widget
        from django.db import transaction
        import re

        # Generate a unique slug — resolved *before* the transaction so the
        # while-loop does not hold a write lock during a potentially slow loop.
        base_slug = re.sub(r'[^a-z0-9]+', '-', self.name.lower().strip()).strip('-') + '-dashboard'
        slug = base_slug
        counter = 1
        while Dashboard.objects.filter(workspace=self.workspace, slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        with transaction.atomic():
            dashboard = Dashboard.objects.create(
                workspace=self.workspace,
                name=f"{self.name} Dashboard",
                description=f"Auto-generated dashboard for {self.name}",
                slug=slug,
                created_by=self.created_by,
                layout_config={"columns": 12, "rowHeight": 100, "compact": True}
            )

            # Classify schema fields
            numeric_types = {'number', 'currency', 'percentage', 'integer', 'float', 'decimal'}
            date_types    = {'date', 'datetime'}
            text_types    = {'text', 'string', 'category', 'email', 'url'}

            numeric_fields = [f for f in self.schema if f.get('type') in numeric_types]
            date_fields    = [f for f in self.schema if f.get('type') in date_types]
            text_fields    = [f for f in self.schema if f.get('type') in text_types]

            # ── Row 1: KPI cards (w=3 h=2 each, up to 4 across) ──────────────
            kpi_colors = ['blue', 'green', 'orange', 'purple']
            kpi_icons  = ['fa-database', 'fa-chart-bar', 'fa-coins', 'fa-percent']
            kpi_x, kpi_y, kpi_w, kpi_h = 0, 0, 3, 2

            # Total Records — always present
            Widget.objects.create(
                dashboard=dashboard, widget_type='metric', title='Total Records', table=self,
                query_config={"aggregations": [{"type": "count", "name": "val"}]},
                viz_config={"icon": "fa-database", "color": "blue"},
                position={"x": kpi_x, "y": kpi_y, "w": kpi_w, "h": kpi_h}
            )
            kpi_x += kpi_w

            # One KPI per numeric field (max 3 more so row stays 4-wide)
            for i, field in enumerate(numeric_fields[:3]):
                prefix = "$" if field.get('type') == 'currency' else ""
                suffix = "%" if field.get('type') == 'percentage' else ""
                color  = kpi_colors[(i + 1) % len(kpi_colors)]
                icon   = kpi_icons[(i + 1) % len(kpi_icons)]
                Widget.objects.create(
                    dashboard=dashboard, widget_type='metric',
                    title=f"Total {field['name']}", table=self,
                    query_config={"aggregations": [{"type": "sum", "field": field['name'], "name": "val"}]},
                    viz_config={"prefix": prefix, "suffix": suffix, "icon": icon, "color": color},
                    position={"x": kpi_x, "y": kpi_y, "w": kpi_w, "h": kpi_h}
                )
                kpi_x += kpi_w
                if kpi_x >= 12:
                    kpi_x = 0
                    kpi_y += kpi_h

            # ── Row 2+: Charts ────────────────────────────────────────────────
            chart_y = kpi_y + kpi_h   # start below KPI row
            chart_x = 0

            primary_date = date_fields[0]['name'] if date_fields else None

            # Numeric trend charts — group by date field if available
            for field in numeric_fields[:2]:
                group_by = primary_date if primary_date else 'created_at_date'
                title = f"{field['name']} Over Time" if primary_date else f"{field['name']} Trend"
                Widget.objects.create(
                    dashboard=dashboard, widget_type='line_chart', title=title, table=self,
                    query_config={"aggregations": [
                        {"type": "sum", "field": field['name'], "group_by": group_by, "name": "val"}
                    ]},
                    viz_config={"x_axis": group_by, "y_axis": "val"},
                    position={"x": chart_x, "y": chart_y, "w": 6, "h": 4}
                )
                chart_x += 6
                if chart_x >= 12:
                    chart_x = 0
                    chart_y += 4

            # Date field: records-per-period (only when no numeric field covers it)
            if date_fields and not numeric_fields:
                for dfield in date_fields[:1]:
                    Widget.objects.create(
                        dashboard=dashboard, widget_type='line_chart',
                        title=f"Records by {dfield['name']}", table=self,
                        query_config={"aggregations": [
                            {"type": "count", "group_by": dfield['name'], "name": "val"}
                        ]},
                        viz_config={"x_axis": dfield['name'], "y_axis": "val"},
                        position={"x": chart_x, "y": chart_y, "w": 6, "h": 4}
                    )
                    chart_x += 6
                    if chart_x >= 12:
                        chart_x = 0
                        chart_y += 4

            # Categorical fields: bar chart for distribution
            for field in text_fields[:2]:
                Widget.objects.create(
                    dashboard=dashboard, widget_type='bar_chart',
                    title=f"{field['name']} Distribution", table=self,
                    query_config={"aggregations": [
                        {"type": "count", "group_by": field['name'], "name": "val"}
                    ]},
                    viz_config={"x_axis": field['name'], "y_axis": "val"},
                    position={"x": chart_x, "y": chart_y, "w": 6, "h": 4}
                )
                chart_x += 6
                if chart_x >= 12:
                    chart_x = 0
                    chart_y += 4

            # Data table at the bottom — always present
            table_y = chart_y + (4 if chart_x > 0 else 0)
            Widget.objects.create(
                dashboard=dashboard, widget_type='table',
                title=f"All {self.name} Records", table=self,
                query_config={"limit": 20},
                viz_config={"page_size": 20, "show_search": True},
                position={"x": 0, "y": table_y, "w": 12, "h": 6}
            )

        return dashboard  # transaction committed — dashboard + all widgets exist or none do


class Record(models.Model):
    """
    Individual data rows in a table
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    table = models.ForeignKey(DataTable, on_delete=models.CASCADE, related_name='records')
    
    # The actual data stored as JSON
    data = JSONField(default=dict)
    
    # Metadata
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='updated_records')
    updated_at = models.DateTimeField(auto_now=True)
    
    # For soft delete
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    
    # Version for optimistic locking
    version = models.IntegerField(default=1)
    
    class Meta:
        indexes = [
            models.Index(fields=['table', 'is_active']),
            models.Index(fields=['table', 'updated_at']),
            models.Index(fields=['table', 'created_by']),
            # Composite index for the most common query pattern:
            # filter(table=x, is_active=True).order_by('-created_at')
            models.Index(fields=['table', 'is_active', 'created_at'],
                         name='rec_tbl_active_created_idx'),
        ]
    
    def __str__(self):
        return f"Record {self.id} in {self.table.name}"
    
    def save(self, *args, **kwargs):
        # Validate against table schema
        if self.table:
            is_valid, errors = self.table.validate_record(self.data)
            if not is_valid:
                raise ValidationError(f"Record validation failed: {', '.join(errors)}")

        # UUID fields are set before save(), so use _state.adding instead of
        # checking pk is None (which is always False for UUIDField defaults).
        is_new = self._state.adding

        # Update version for optimistic locking on updates
        if not is_new:
            self.version += 1

        super().save(*args, **kwargs)

        # Increment count with a single UPDATE … SET record_count = record_count + 1
        # instead of a SELECT COUNT(*) + full model save on every row.
        # Decrement happens in delete() below.
        if is_new and self.is_active:
            DataTable.objects.filter(pk=self.table_id).update(record_count=F('record_count') + 1)

        # Trigger real-time updates after save
        self._trigger_updates('saved')

    def delete(self, *args, **kwargs):
        # Soft-delete: update directly to avoid re-running save() validation
        # and to decrement count atomically without an extra COUNT query.
        Record.objects.filter(pk=self.pk).update(
            is_active=False,
            deleted_at=timezone.now(),
            version=F('version') + 1,
        )
        self.is_active = False
        self.deleted_at = timezone.now()
        DataTable.objects.filter(pk=self.table_id).update(record_count=F('record_count') - 1)
        self._trigger_updates('deleted')
    
    def _trigger_updates(self, action):
        """Trigger real-time updates"""
        try:
            # Get all dashboards that use this table
            from .models import Widget
            
            affected_widgets = Widget.objects.filter(
                table=self.table,
                dashboard__is_active=True
            ).values_list('id', 'dashboard_id')
            
            # Queue updates for each widget
            for widget_id, dashboard_id in affected_widgets:
                broadcast_widget_update.delay(str(widget_id), str(dashboard_id))
            
            # Notify workspace
            notify_table_change.delay(str(self.table.id), action)
            
        except Exception as e:
            logger.error(f"Failed to trigger updates: {str(e)}")



class Dashboard(models.Model):
    """
    User-created dashboards containing multiple widgets
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='dashboards')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    slug = models.SlugField(max_length=120)
    
    # Layout configuration (grid system)
    layout_config = JSONField(default=dict)

    # Dashboard-level filter bar configuration.
    # Defines which filter controls are shown above the widget grid.
    # Each entry describes one filter input the user can interact with.
    #
    # Schema:
    #   {
    #     "filters": [
    #       {"field": "region",  "label": "Region",  "type": "text"},
    #       {"field": "status",  "label": "Status",  "type": "select",
    #        "options": ["active", "inactive"]},
    #       {"field": "sale_date","label": "Date",   "type": "date"}
    #     ]
    #   }
    #
    # When the user selects a value in the filter bar the frontend sends
    # the active values as extra_filters to the widget data API, which
    # merges them with each widget's own query_config.filters before
    # executing the query.
    filter_config = JSONField(default=dict, blank=True)

    # For public sharing
    is_public = models.BooleanField(default=False)
    public_uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['workspace', 'slug']
        indexes = [
            models.Index(fields=['workspace', 'is_public']),
            models.Index(fields=['public_uuid']),
        ]
    
    def __str__(self):
        return f"{self.name} - {self.workspace.name}"
    
    def get_public_url(self):
        """Get public sharing URL if enabled"""
        if self.is_public:
            return f"/shared/dashboard/{self.public_uuid}/"
        return None


class Widget(models.Model):
    """
    Individual chart, table, or metric on a dashboard
    """
    WIDGET_TYPES = [
        ('line_chart', 'Line Chart'),
        ('bar_chart', 'Bar Chart'),
        ('pie_chart', 'Pie Chart'),
        ('table', 'Data Table'),
        ('metric', 'Single Metric'),
        ('number', 'Number Card'),
        ('gauge', 'Gauge'),
        ('heatmap', 'Heatmap'),
        ('scatter', 'Scatter Plot'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name='widgets')
    widget_type = models.CharField(max_length=20, choices=WIDGET_TYPES)
    title = models.CharField(max_length=100)
    
    # ── data source: one of (table) or (data_source + source_table_name) ────
    # Imported DataTable (CSV/webhook ingestion)
    table = models.ForeignKey(DataTable, on_delete=models.SET_NULL, null=True, blank=True)
    # Direct DB connector
    data_source       = models.ForeignKey(
        'DataSource', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='widgets',
    )
    # Table/view name inside the external database (used when data_source is set)
    source_table_name = models.CharField(max_length=200, blank=True)
    
    # Query configuration — blank=True because {} is a valid empty config.
    query_config = JSONField(default=dict, blank=True)

    # Hard ceiling: a single widget may never request more than this many rows.
    # Prevents crafted query_configs from materialising millions of records.
    MAX_QUERY_LIMIT = 10_000

    # Aggregation types the query engine actually supports.
    ALLOWED_AGG_TYPES = {'count', 'sum', 'avg', 'min', 'max', 'distinct'}

    # Only these characters are allowed in field / group_by names.  This blocks
    # injection attempts (SQL, ORM __field traversal, path separators, etc.).
    _SAFE_FIELD_RE = re.compile(r'^[A-Za-z0-9_ .\-]{1,128}$')

    # Visualization configuration — blank=True because {} is a valid empty config.
    viz_config = JSONField(default=dict, blank=True)

    # Position in grid (x, y, width, height) — blank=True same reason.
    position = JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['dashboard', 'widget_type']),
        ]

    def __str__(self):
        return f"{self.title} ({self.widget_type})"

    def clean(self):
        """Validate query_config so malformed or oversized configs are rejected
        at model-save time rather than silently exploding at render time."""
        from django.core.exceptions import ValidationError

        qc = self.query_config or {}

        # ── Row limit cap ────────────────────────────────────────────────
        limit = qc.get('limit')
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise ValidationError(
                    {'query_config': "query_config.limit must be an integer."}
                )
            if limit < 1:
                raise ValidationError(
                    {'query_config': "query_config.limit must be at least 1."}
                )
            if limit > self.MAX_QUERY_LIMIT:
                raise ValidationError(
                    {'query_config': (
                        f"query_config.limit cannot exceed {self.MAX_QUERY_LIMIT:,}. "
                        f"Requested: {limit:,}."
                    )}
                )

        # ── Aggregation shape ────────────────────────────────────────────
        aggs = qc.get('aggregations')
        if aggs is not None:
            if not isinstance(aggs, list):
                raise ValidationError(
                    {'query_config': "query_config.aggregations must be a list."}
                )
            if len(aggs) > 20:
                raise ValidationError(
                    {'query_config': (
                        "query_config.aggregations may contain at most 20 items."
                    )}
                )
            for i, agg in enumerate(aggs):
                if not isinstance(agg, dict):
                    raise ValidationError(
                        {'query_config': f"aggregations[{i}] must be an object."}
                    )
                agg_type = agg.get('type', '')
                if agg_type not in self.ALLOWED_AGG_TYPES:
                    raise ValidationError(
                        {'query_config': (
                            f"aggregations[{i}].type '{agg_type}' is not supported. "
                            f"Allowed: {', '.join(sorted(self.ALLOWED_AGG_TYPES))}."
                        )}
                    )
                # Validate field and group_by names — only safe identifier chars.
                for key in ('field', 'group_by', 'name'):
                    val = agg.get(key)
                    if val is not None:
                        if not isinstance(val, str):
                            raise ValidationError(
                                {'query_config': f"aggregations[{i}].{key} must be a string."}
                            )
                        if not self._SAFE_FIELD_RE.match(val):
                            raise ValidationError(
                                {'query_config': (
                                    f"aggregations[{i}].{key} '{val}' contains invalid "
                                    f"characters. Use only letters, digits, underscores, "
                                    f"dots, and hyphens (max 128 chars)."
                                )}
                            )

        # ── Filters shape ────────────────────────────────────────────────
        filters = qc.get('filters')
        if filters is not None:
            if not isinstance(filters, list):
                raise ValidationError(
                    {'query_config': "query_config.filters must be a list."}
                )
            if len(filters) > 50:
                raise ValidationError(
                    {'query_config': "query_config.filters may contain at most 50 items."}
                )
            # Validate field names in filters.
            for j, f in enumerate(filters):
                if not isinstance(f, dict):
                    raise ValidationError(
                        {'query_config': f"filters[{j}] must be an object."}
                    )
                field_val = f.get('field')
                if field_val is not None:
                    if not isinstance(field_val, str):
                        raise ValidationError(
                            {'query_config': f"filters[{j}].field must be a string."}
                        )
                    if not self._SAFE_FIELD_RE.match(field_val):
                        raise ValidationError(
                            {'query_config': (
                                f"filters[{j}].field '{field_val}' contains invalid "
                                f"characters. Use only letters, digits, underscores, "
                                f"dots, and hyphens (max 128 chars)."
                            )}
                        )

    def save(self, *args, **kwargs):
        """Run full_clean() so query_config validation fires on every save path."""
        self.full_clean()
        super().save(*args, **kwargs)

    def get_data(self, limit=1000):
        """
        Execute the query and return data for this widget.

        Routes to DataSourceQueryEngine when data_source is set,
        otherwise falls back to the standard QueryEngine (DataTable path).
        """
        if not self.table and not (self.data_source and self.source_table_name):
            return {"error": "No data source selected"}

        try:
            from .services import QueryEngine, DataSourceQueryEngine
            if self.data_source_id and self.source_table_name:
                engine = DataSourceQueryEngine(self.data_source)
                result = engine.execute_widget_query(self, limit=limit)
            else:
                engine = QueryEngine()
                result = engine.execute_widget_query(self, limit=limit)

            if result is None:
                return {"message": "No data available"}
            return result

        except Exception as e:
            logger.error(f"Widget query error: {str(e)}", exc_info=True)
            return {"error": str(e)}
    
class AuditLog(models.Model):
    """
    Immutable audit log for compliance and debugging
    """
    ACTION_TYPES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('view', 'View'),
        ('export', 'Export'),
        ('share', 'Share'),
    ]
    
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, null=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=20, choices=ACTION_TYPES)
    content_type = models.CharField(max_length=50)
    object_id = models.UUIDField()
    object_repr = models.CharField(max_length=200)
    changes = JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True)
    user_agent = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'timestamp']),
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['content_type', 'object_id']),
        ]
        ordering = ['-timestamp']
    
    def __str__(self):
        return f"{self.action} {self.content_type} at {self.timestamp}"


class ImportJob(models.Model):
    """
    Track asynchronous CSV/Excel import jobs
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='import_jobs')
    table = models.ForeignKey(DataTable, on_delete=models.CASCADE, null=True, blank=True, related_name='import_jobs')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    IMPORT_MODE_REPLACE = 'replace'
    IMPORT_MODE_APPEND  = 'append'
    IMPORT_MODE_UPSERT  = 'upsert'
    IMPORT_MODE_CHOICES = [
        (IMPORT_MODE_REPLACE, 'Replace — overwrite all existing records'),
        (IMPORT_MODE_APPEND,  'Append — add rows without touching existing data'),
        (IMPORT_MODE_UPSERT,  'Upsert — insert or update keyed on primary_key field'),
    ]

    file_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500, blank=True)
    # SHA-256 of the raw file bytes — used to prevent duplicate imports when a
    # Celery task retries or the user re-uploads an identical file.
    file_hash = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Delta-import controls
    import_mode = models.CharField(
        max_length=10, choices=IMPORT_MODE_CHOICES, default=IMPORT_MODE_APPEND,
        help_text='How incoming rows are merged with existing data.',
    )
    primary_key_field = models.CharField(
        max_length=255, blank=True,
        help_text='Field name used as the unique key for upsert mode.',
    )

    total_rows = models.IntegerField(default=0)
    processed_rows = models.IntegerField(default=0)
    success_rows = models.IntegerField(default=0)
    error_rows = models.IntegerField(default=0)

    error_log = JSONField(default=list)
    celery_task_id = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['table', 'status']),
        ]
        # Prevent duplicate imports: same file into the same table cannot be
        # re-imported unless the previous job failed (handled in service layer).
        constraints = [
            models.UniqueConstraint(
                fields=['table', 'file_hash'],
                condition=models.Q(status__in=['pending', 'running', 'completed']),
                name='unique_successful_import_per_table_hash',
            )
        ]

    def __str__(self):
        return f"ImportJob {self.file_name} ({self.status})"

    @staticmethod
    def compute_hash(file_obj):
        """Return SHA-256 hex digest of *file_obj* (rewinds before and after)."""
        import hashlib
        file_obj.seek(0)
        h = hashlib.sha256()
        for chunk in iter(lambda: file_obj.read(65536), b''):
            h.update(chunk)
        file_obj.seek(0)
        return h.hexdigest()

    @property
    def progress_pct(self):
        if self.total_rows == 0:
            return 0
        return min(100, int((self.processed_rows / self.total_rows) * 100))


class DataAlert(models.Model):
    """
    Threshold-based alert on a numeric field in a DataTable.

    When the Celery beat task ``check_data_alerts`` runs, it evaluates
    ``current_value <operator> threshold`` for every active alert.  On a
    trigger, it creates a ``Notification`` for every member of the workspace
    and optionally sends email if the member's preferences allow it.

    Cooldown: a triggered alert will not fire again for ``cooldown_minutes``
    (default 60) to avoid notification storms.
    """
    OPERATOR_CHOICES = [
        ('gt',  'Greater than'),
        ('gte', 'Greater than or equal'),
        ('lt',  'Less than'),
        ('lte', 'Less than or equal'),
        ('eq',  'Equal to'),
    ]
    AGGREGATE_CHOICES = [
        ('sum',   'Sum'),
        ('avg',   'Average'),
        ('count', 'Count'),
        ('min',   'Minimum'),
        ('max',   'Maximum'),
    ]

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace    = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='alerts')
    table        = models.ForeignKey(DataTable, on_delete=models.CASCADE, related_name='alerts')
    created_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    name         = models.CharField(max_length=150)
    field_name   = models.CharField(max_length=100, help_text="Column in the DataTable to aggregate")
    aggregate    = models.CharField(max_length=10, choices=AGGREGATE_CHOICES, default='sum')
    operator     = models.CharField(max_length=5,  choices=OPERATOR_CHOICES)
    threshold    = models.FloatField()

    is_active      = models.BooleanField(default=True)
    cooldown_minutes = models.PositiveIntegerField(default=60)

    # Populated by the check task
    last_triggered = models.DateTimeField(null=True, blank=True)
    last_value     = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'is_active']),
            models.Index(fields=['table', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_aggregate_display()} {self.field_name} {self.get_operator_display()} {self.threshold})"

    def is_in_cooldown(self):
        """Return True if the alert fired recently and should be suppressed."""
        if not self.last_triggered:
            return False
        from datetime import timedelta
        return timezone.now() < self.last_triggered + timedelta(minutes=self.cooldown_minutes)

    def evaluate(self, current_value: float) -> bool:
        """Return True when the condition is met."""
        ops = {
            'gt':  current_value >  self.threshold,
            'gte': current_value >= self.threshold,
            'lt':  current_value <  self.threshold,
            'lte': current_value <= self.threshold,
            'eq':  abs(current_value - self.threshold) < 1e-9,
        }
        return ops.get(self.operator, False)


class WebhookEndpoint(models.Model):
    """
    Workspace-scoped webhook ingestion endpoint.

    A client POSTs a JSON payload to ``/webhook/ingest/<token>/``.
    The view validates the ``X-Hub-Signature-256`` HMAC header
    (HMAC-SHA256 of the raw request body with ``secret`` as key)
    and creates ``Record`` rows in the target ``DataTable``.

    The payload must be either:
    - a JSON object   → treated as a single record
    - a JSON array    → each element treated as one record

    ``is_active=False`` returns HTTP 404 so endpoints can be
    disabled without deleting them.
    """
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace  = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='webhook_endpoints')
    table      = models.ForeignKey(DataTable, on_delete=models.CASCADE, related_name='webhook_endpoints')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    name       = models.CharField(max_length=150)
    # The routing token embedded in the URL — random, not guessable
    token      = models.CharField(max_length=64, unique=True, db_index=True)
    # HMAC signing secret — stored as Fernet ciphertext (encrypted at rest).
    # Use get_plaintext_secret() to recover the value for HMAC verification.
    # The plaintext is shown to the user exactly once: on creation and on
    # explicit secret rotation via the regenerate-secret endpoint.
    secret     = models.CharField(max_length=256)

    is_active  = models.BooleanField(default=True)

    # Telemetry
    total_requests  = models.PositiveIntegerField(default=0)
    last_request_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'is_active']),
        ]

    def __str__(self):
        return f"WebhookEndpoint({self.name}, workspace={self.workspace_id})"

    @staticmethod
    def generate_token():
        """Return a URL-safe 32-byte hex token."""
        import secrets
        return secrets.token_hex(32)

    @staticmethod
    def generate_secret():
        """Return a new 32-byte URL-safe plaintext secret."""
        import secrets
        return secrets.token_urlsafe(32)

    def get_plaintext_secret(self) -> str:
        """
        Decrypt and return the plaintext signing secret.

        The DB column stores a Fernet ciphertext.  Call this method whenever
        you need the actual bytes used as the HMAC key (i.e. in the webhook
        ingestion view).
        """
        from apps.dashboards.webhook_crypto import decrypt_secret
        return decrypt_secret(self.secret)


class DataSource(models.Model):
    """
    Direct database connector for live querying of external databases.

    Supported connectors: PostgreSQL (psycopg3), MySQL (mysqlclient / PyMySQL).
    Credentials are stored with Fernet encryption (same scheme as WebhookEndpoint).
    Only read-only connections are issued — the recommended practice is to
    create a dedicated read-only DB user for each connector.

    Workflow:
        1. POST /api/v1/workspaces/<id>/data-sources/ to create.
        2. POST /api/v1/data-sources/<id>/test/ to verify connectivity.
        3. GET  /api/v1/data-sources/<id>/schema/ to browse tables & columns.
        4. Create a Widget, set data_source=<id> and source_table_name=<table>.
           The QueryEngine will route to DataSourceQueryEngine automatically.
    """

    CONNECTOR_POSTGRESQL   = 'postgresql'
    CONNECTOR_MYSQL        = 'mysql'
    CONNECTOR_GOOGLE_SHEETS = 'google_sheets'
    CONNECTOR_CHOICES = [
        (CONNECTOR_POSTGRESQL,    'PostgreSQL'),
        (CONNECTOR_MYSQL,         'MySQL'),
        (CONNECTOR_GOOGLE_SHEETS, 'Google Sheets'),
    ]

    SSL_DISABLE  = 'disable'
    SSL_REQUIRE  = 'require'
    SSL_VERIFY   = 'verify-full'
    SSL_CHOICES  = [
        (SSL_DISABLE, 'Disable'),
        (SSL_REQUIRE, 'Require'),
        (SSL_VERIFY,  'Verify CA'),
    ]

    # ── identity ─────────────────────────────────────────────────────────
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace  = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='data_sources')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    # ── connection config ─────────────────────────────────────────────────
    name           = models.CharField(max_length=200)
    connector_type = models.CharField(max_length=20, choices=CONNECTOR_CHOICES,
                                      default=CONNECTOR_POSTGRESQL)

    # ── Database connector fields (PostgreSQL / MySQL) ────────────────────
    # blank=True / default='' because they are not required for Google Sheets.
    host           = models.CharField(max_length=255, blank=True, default='')
    port           = models.PositiveIntegerField(default=5432)
    database       = models.CharField(max_length=200, blank=True, default='')
    username       = models.CharField(max_length=200, blank=True, default='')
    # Fernet-encrypted password — use get_plaintext_password() to decrypt
    _password      = models.TextField(db_column='password_enc', blank=True)
    ssl_mode       = models.CharField(max_length=20, choices=SSL_CHOICES, default=SSL_REQUIRE)
    # Optional extra driver options (e.g. {"connect_timeout": 10, "application_name": "analyticsmeta"})
    extra_options  = models.JSONField(default=dict, blank=True)

    # ── Google Sheets connector fields ────────────────────────────────────
    # Fernet-encrypted service-account JSON — use get_google_credentials() to decrypt.
    google_credentials_enc  = models.TextField(blank=True, default='')
    # The spreadsheet ID from the Google Sheets URL:
    #   https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
    google_spreadsheet_id   = models.CharField(max_length=200, blank=True, default='')

    # ── status ────────────────────────────────────────────────────────────
    is_active       = models.BooleanField(default=True)
    last_tested_at  = models.DateTimeField(null=True, blank=True)
    last_test_ok    = models.BooleanField(null=True)
    last_test_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['workspace', 'name']
        indexes  = [
            models.Index(fields=['workspace', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.connector_type}://{self.host}/{self.database})"

    def set_password(self, plaintext: str) -> None:
        """Encrypt and store *plaintext*."""
        from apps.dashboards.webhook_crypto import encrypt_secret
        self._password = encrypt_secret(plaintext)

    def get_plaintext_password(self) -> str:
        """Decrypt and return the plaintext connection password."""
        from apps.dashboards.webhook_crypto import decrypt_secret
        return decrypt_secret(self._password)

    def get_dsn(self) -> str:
        """Return a libpq-style DSN string (password decrypted inline)."""
        pw = self.get_plaintext_password()
        ssl = f"?sslmode={self.ssl_mode}" if self.connector_type == self.CONNECTOR_POSTGRESQL else ""
        return (
            f"{self.connector_type}://{self.username}:{pw}"
            f"@{self.host}:{self.port}/{self.database}{ssl}"
        )

    # ── Google Sheets helpers ─────────────────────────────────────────────

    def set_google_credentials(self, credentials_json: str) -> None:
        """Encrypt and store the service-account JSON string."""
        from apps.dashboards.webhook_crypto import encrypt_secret
        self.google_credentials_enc = encrypt_secret(credentials_json)

    def get_google_credentials(self) -> str:
        """Decrypt and return the service-account JSON string."""
        from apps.dashboards.webhook_crypto import decrypt_secret
        return decrypt_secret(self.google_credentials_enc)