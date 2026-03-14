# apps/insights/serializers.py
from rest_framework import serializers
from apps.dashboards.models import Dashboard, Widget, DataTable
from .utils import InsightJSONEncoder
import json
import logging
from uuid import UUID
from datetime import datetime, date
from decimal import Decimal

logger = logging.getLogger(__name__)

class WidgetSerializer(serializers.ModelSerializer):
    """Serializer for widgets with data"""
    widget_data = serializers.SerializerMethodField()
    table_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Widget
        fields = ['id', 'dashboard', 'widget_type', 'title', 'table',
                 'table_name', 'query_config', 'viz_config', 'position', 
                 'widget_data', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def to_representation(self, instance):
        """Override to ensure all UUIDs are converted to strings"""
        data = super().to_representation(instance)
        # Convert UUID fields to strings
        if 'id' in data and data['id']:
            data['id'] = str(data['id'])
        if 'dashboard' in data and data['dashboard']:
            data['dashboard'] = str(data['dashboard'])
        if 'table' in data and data['table']:
            data['table'] = str(data['table'])
        return data
    
    def get_widget_data(self, obj):
        """Get widget data with error handling and proper encoding"""
        try:
            if not obj.table:
                return {"message": "No table selected"}
            
            # Use limit from query_config or default to 100
            limit = obj.query_config.get('limit', 100) if obj.query_config else 100
            data = obj.get_data(limit=limit)
            
            # Ensure data is JSON serializable
            if isinstance(data, dict):
                # Recursively convert any UUIDs in the data
                return self._make_json_serializable(data)
            return data
        except Exception as e:
            logger.error(f"Error getting widget data for {obj.id}: {e}")
            return {"error": str(e)}
    
    def get_table_name(self, obj):
        """Get table name for display"""
        return obj.table.name if obj.table else None
    
    def _make_json_serializable(self, obj):
        """Recursively convert non-serializable objects to serializable format"""
        if isinstance(obj, dict):
            return {str(k): self._make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, UUID):
            return str(obj)
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return float(obj)
        else:
            return obj

class DashboardSerializer(serializers.ModelSerializer):
    """Serializer for dashboards with widgets"""
    widgets = serializers.SerializerMethodField()
    workspace_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Dashboard
        fields = ['id', 'workspace', 'workspace_name', 'name', 'description', 'slug',
                 'layout_config', 'is_public', 'public_uuid', 'widgets',
                 'created_at', 'updated_at']
        read_only_fields = ['id', 'workspace', 'public_uuid', 'created_at', 'updated_at']
    
    def to_representation(self, instance):
        """Override to ensure all UUIDs are converted to strings"""
        data = super().to_representation(instance)
        # Convert UUID fields to strings
        if 'id' in data and data['id']:
            data['id'] = str(data['id'])
        if 'workspace' in data and data['workspace']:
            data['workspace'] = str(data['workspace'])
        if 'public_uuid' in data and data['public_uuid']:
            data['public_uuid'] = str(data['public_uuid'])
        return data
    
    def get_widgets(self, obj):
        """Get all widgets for this dashboard"""
        try:
            widgets = obj.widgets.all().order_by('created_at')
            serializer = WidgetSerializer(widgets, many=True, context=self.context)
            return serializer.data
        except Exception as e:
            logger.error(f"Error getting widgets for dashboard {obj.id}: {e}")
            return []
    
    def get_workspace_name(self, obj):
        """Get workspace name"""
        return obj.workspace.name if obj.workspace else None