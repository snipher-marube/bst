import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.db.models import JSONField
from apps.workspaces.models import Workspace

User = get_user_model()


class Insight(models.Model):
    """
    AI-generated insight for a workspace – a single finding with optional chart data.
    """
    INSIGHT_TYPES = [
        ('trend', 'Trend'),
        ('anomaly', 'Anomaly'),
        ('summary', 'Summary'),
        ('prediction', 'Prediction'),
        ('comparison', 'Comparison'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='insights')

    title = models.CharField(max_length=200)
    description = models.TextField()
    insight_type = models.CharField(max_length=20, choices=INSIGHT_TYPES, default='summary')

    # Optional chart data for rendering
    chart_data = JSONField(default=dict, blank=True)

    # Link back to the source table, if applicable
    source_table_name = models.CharField(max_length=100, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'insight_type']),
            models.Index(fields=['workspace', 'created_at']),
        ]

    def __str__(self):
        return f"{self.title} ({self.workspace.name})"
