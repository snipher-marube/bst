import uuid
import json
from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.db.models import JSONField
from django.utils import timezone
from django.conf import settings

User = get_user_model()

class Workspace(models.Model):
    """
    Each user gets a workspace. For enterprise, multiple users can share a workspace.
    This is the top-level isolation boundary.
    """
    TIER_CHOICES = [
        ('free', 'Free'),
        ('starter', 'Starter'),
        ('professional', 'Professional'),
        ('enterprise', 'Enterprise'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_workspaces')
    members = models.ManyToManyField(
        User, 
        through='WorkspaceMembership', 
        through_fields=('workspace', 'user'),  # Specify the fields
        related_name='workspaces'
    )
    
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    # Limits based on tier - these will be enforced in business logic
    max_tables = models.IntegerField(default=5)  # Free tier: 5 tables
    max_records_per_table = models.IntegerField(default=1000)
    max_team_members = models.IntegerField(default=1)
    
    class Meta:
        indexes = [
            models.Index(fields=['owner', 'is_active']),
            models.Index(fields=['tier']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.owner.email})"
    
    def can_add_table(self):
        """Check if workspace can add another table"""
        current_tables = self.tables.count()
        return current_tables < self.max_tables
    
    def get_usage_stats(self):
        """Get current usage statistics"""
        from django.db.models import Sum, Count
        
        tables = self.tables.all()
        total_records = sum(table.records.count() for table in tables)
        total_storage = sum(table.estimated_storage_bytes for table in tables)
        
        return {
            'tables': tables.count(),
            'tables_limit': self.max_tables,
            'records': total_records,
            'records_limit': self.max_records_per_table * self.max_tables,
            'members': self.members.count(),
            'members_limit': self.max_team_members,
            'storage_bytes': total_storage,
        }

class WorkspaceMembership(models.Model):
    """
    Junction model for workspace members with roles
    """
    ROLE_CHOICES = [
        ('owner', 'Owner'),  # Full access
        ('admin', 'Admin'),  # Can manage workspace settings
        ('editor', 'Editor'),  # Can create/edit dashboards
        ('viewer', 'Viewer'),  # View only
    ]
    
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='invited_members')
    invited_at = models.DateTimeField(auto_now_add=True)
    joined_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['workspace', 'user']
        indexes = [
            models.Index(fields=['workspace', 'role']),
        ]
    
    def __str__(self):
        return f"{self.user.email} - {self.workspace.name} ({self.role})"


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
    # Example: [{"name": "Product", "type": "text", "required": true}, {"name": "Price", "type": "currency", "required": true}]
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
    record_count = models.IntegerField(default=0)  # Denormalized for performance
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['workspace', 'name']  # Unique table names per workspace
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
            schema_str = json.dumps(self.schema, sort_keys=True)
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
            
            if value is not None:
                if field_type == 'number' or field_type == 'currency' or field_type == 'percentage':
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
        avg_record_size = 1024  # Assume 1KB per record average
        return self.record_count * avg_record_size

    def generate_default_dashboard(self):
        """Automatically create a dashboard for this table"""
        from .models import Dashboard, Widget
        
        # Create a dashboard named after the table
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
        
        # Analyze schema to create appropriate widgets
        position_x = 0
        position_y = 0
        
        for field in self.schema:
            field_name = field['name']
            field_type = field['type']
            
            # Create different widgets based on field type
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
                                "name": f"total_{field_name}"
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
                
                # Create trend chart
                if position_x >= 12:
                    position_x = 0
                    position_y += 2
                
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
                                "group_by": "created_at_date"
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": "created_at_date",
                        "y_axis": f"sum_{field_name}",
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
                                "group_by": field_name
                            }
                        ]
                    },
                    viz_config={
                        "x_axis": field_name,
                        "y_axis": "count",
                        "show_legend": True
                    },
                    position={"x": position_x, "y": position_y, "w": 6, "h": 4}
                )
                position_x += 6
                
            # Reset position for next row
            if position_x >= 12:
                position_x = 0
                position_y += 4
        
        # Add a data table widget to see all records
        Widget.objects.create(
            dashboard=dashboard,
            widget_type='table',
            title=f"All {self.name} Records",
            table=self,
            query_config={},
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
    data = JSONField()
    
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
        # Note: We can't index JSON fields directly in all databases
        # Consider using PostgreSQL with GIN indexes for JSONB if needed
    
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
    # Example: {"columns": 12, "rowHeight": 100, "compact": true}
    layout_config = JSONField(default=dict)
    
    # For public sharing
    is_public = models.BooleanField(default=False)
    public_uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    is_active = models.BooleanField(default=True)
    
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
    # Example: {"aggregation": "sum", "field": "sales", "group_by": "month", "filters": [...]}
    query_config = JSONField(default=dict)
    
    # Visualization configuration
    # Example: {"colors": ["#03466e"], "show_legend": true, "x_axis_label": "Month"}
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
            return None
        
        from .services import QueryEngine
        engine = QueryEngine()
        return engine.execute_widget_query(self, limit=limit)


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
    content_type = models.CharField(max_length=50)  # Table, Record, Dashboard, etc.
    object_id = models.UUIDField()
    object_repr = models.CharField(max_length=200)
    changes = JSONField(default=dict)  # Store before/after values
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