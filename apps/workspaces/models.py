import uuid
from django.db import models
from django.contrib.auth import get_user_model
import logging


logger = logging.getLogger(__name__)

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
        through_fields=('workspace', 'user'),
        related_name='workspaces'
    )
    
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    # Limits based on tier - these will be enforced in business logic
    max_tables = models.IntegerField(default=5)
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
            'dashboards': self.dashboards.count(),
        }


class WorkspaceMembership(models.Model):
    """
    Junction model for workspace members with roles
    """
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('admin', 'Admin'),
        ('editor', 'Editor'),
        ('viewer', 'Viewer'),
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

