import uuid
import json
from django.db import models
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import JSONField
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
        """Automatically create a dashboard for this table"""
        from .models import Dashboard, Widget
        
        dashboard = Dashboard.objects.create(
            workspace=self.workspace,
            name=f"{self.name} Dashboard",
            description=f"Auto-generated dashboard for {self.name}",
            slug=f"{self.name.lower().replace(' ', '-')}-dashboard",
            created_by=self.created_by,
            layout_config={
                "columns": 12,
                "rowHeight": 100,
                "compact": True
            }
        )
        
        position_x = 0
        position_y = 0
        
        for field in self.schema:
            field_name = field['name']
            field_type = field['type']
            
            if field_type in ['number', 'currency', 'percentage']:
                # Create summary widget for numeric fields
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='metric',
                    title=f"Total {field_name}",
                    table=self,
                    query_config={
                        "aggregations": [
                            {
                                "type": "sum",
                                "field": field_name,
                                "name": "val"
                            }
                        ]
                    },
                    viz_config={
                        "format": "number",
                        "prefix": "$" if field_type == 'currency' else "",
                        "suffix": "%" if field_type == 'percentage' else ""
                    },
                    position={"x": position_x, "y": position_y, "w": 3, "h": 2}
                )
                position_x += 3
                
                if position_x >= 12:
                    position_x = 0
                    position_y += 2
                
                # Create trend chart
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='line_chart',
                    title=f"{field_name} Over Time",
                    table=self,
                    query_config={
                        "aggregations": [
                            {
                                "type": "sum",
                                "field": field_name,
                                "group_by": "created_at_date",
                                "name": "val"
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": "created_at_date",
                        "y_axis": "val",
                        "show_legend": True
                    },
                    position={"x": position_x, "y": position_y, "w": 6, "h": 4}
                )
                position_x += 6
                
            elif field_type in ['date', 'datetime']:
                # Create timeline widget
                Widget.objects.create(
                    dashboard=dashboard,
                    widget_type='line_chart',
                    title=f"Records by {field_name}",
                    table=self,
                    query_config={
                        "aggregations": [
                            {
                                "type": "count",
                                "field": "id",
                                "group_by": field_name,
                                "name": "val"
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": field_name,
                        "y_axis": "val",
                        "show_legend": True
                    },
                    position={"x": position_x, "y": position_y, "w": 6, "h": 4}
                )
                position_x += 6
                
            if position_x >= 12:
                position_x = 0
                position_y += 4
        
        # Add a data table widget to see all records
        Widget.objects.create(
            dashboard=dashboard,
            widget_type='table',
            title=f"All {self.name} Records",
            table=self,
            query_config={"limit": 10},
            viz_config={
                "page_size": 10,
                "show_search": True
            },
            position={"x": 0, "y": position_y + 2, "w": 12, "h": 6}
        )
        
        return dashboard


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
        ]
    
    def __str__(self):
        return f"Record {self.id} in {self.table.name}"
    
    def save(self, *args, **kwargs):
        # Validate against table schema
        if self.table:
            is_valid, errors = self.table.validate_record(self.data)
            if not is_valid:
                raise ValidationError(f"Record validation failed: {', '.join(errors)}")
        
        # Update version for optimistic locking
        if self.pk:
            self.version += 1
        
        super().save(*args, **kwargs)
        
        # Update table record count
        self.table.record_count = self.table.records.filter(is_active=True).count()
        self.table.save(update_fields=['record_count'])
        
        # Trigger real-time updates after save
        self._trigger_updates('saved')

    def delete(self, *args, **kwargs):
        # Soft delete
        self.is_active = False
        self.deleted_at = timezone.now()
        self.save()
        
        # Trigger updates
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
    
    # Data source configuration
    table = models.ForeignKey(DataTable, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Query configuration
    query_config = JSONField(default=dict)
    
    # Visualization configuration
    viz_config = JSONField(default=dict)
    
    # Position in grid (x, y, width, height)
    position = JSONField(default=dict)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['dashboard', 'widget_type']),
        ]
    
    def __str__(self):
        return f"{self.title} ({self.widget_type})"
    
    def get_data(self, limit=1000):
        """
        Execute the query and return data for this widget
        """
        if not self.table:
            return {"error": "No table selected"}
    
        # Check if table has records
        if self.table.record_count == 0:
            return {"message": "No data available in this table"}
    
        try:
            from .services import QueryEngine
            engine = QueryEngine()
            result = engine.execute_widget_query(self, limit=limit)
        
            # Ensure we always return a dict
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

    file_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

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

    def __str__(self):
        return f"ImportJob {self.file_name} ({self.status})"

    @property
    def progress_pct(self):
        if self.total_rows == 0:
            return 0
        return int((self.processed_rows / self.total_rows) * 100)