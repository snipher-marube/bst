from rest_framework import serializers
from .models import Workspace, DataTable, Record, Dashboard, Widget
from django.core.exceptions import ValidationError
import logging

logger = logging.getLogger(__name__)

class WorkspaceSerializer(serializers.ModelSerializer):
    usage_stats = serializers.SerializerMethodField()
    
    class Meta:
        model = Workspace
        fields = ['id', 'name', 'tier', 'created_at', 'usage_stats', 'max_tables']
        read_only_fields = ['id', 'created_at', 'tier']
    
    def get_usage_stats(self, obj):
        return obj.get_usage_stats()
    
    def create(self, validated_data):
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)


class DataTableSerializer(serializers.ModelSerializer):
    record_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = DataTable
        fields = ['id', 'workspace', 'name', 'description', 'schema', 
                 'created_at', 'updated_at', 'record_count']
        read_only_fields = ['id', 'workspace', 'created_at', 'updated_at', 'record_count']
    
    def validate(self, data):
        # Check workspace limits (workspace passed via context by the view)
        workspace = self.context.get('workspace')
        if workspace and not self.instance and not workspace.can_add_table():
            raise serializers.ValidationError("Workspace has reached maximum table limit")
        return data


class RecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = Record
        fields = ['id', 'table', 'data', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def validate(self, data):
        # Validate against table schema
        table = data.get('table')
        if self.instance:
            table = self.instance.table
        
        if table:
            is_valid, errors = table.validate_record(data.get('data', {}))
            if not is_valid:
                raise ValidationError({'data': errors})
        
        return data


class DashboardSerializer(serializers.ModelSerializer):
    widgets = serializers.SerializerMethodField()
    
    class Meta:
        model = Dashboard
        fields = ['id', 'workspace', 'name', 'description', 'slug', 
                 'layout_config', 'is_public', 'public_uuid', 'widgets',
                 'created_at', 'updated_at']
        read_only_fields = ['id', 'workspace', 'public_uuid', 'created_at', 'updated_at']
    
    def get_widgets(self, obj):
        widgets = obj.widgets.all()
        return WidgetSerializer(widgets, many=True, context=self.context).data


class WidgetSerializer(serializers.ModelSerializer):
    widget_data = serializers.SerializerMethodField()
    table_id = serializers.UUIDField(source='table.id', read_only=True)
    table_name = serializers.CharField(source='table.name', read_only=True)
    
    class Meta:
        model = Widget
        fields = ['id', 'dashboard', 'widget_type', 'title', 'table', 'table_id', 'table_name',
                 'query_config', 'viz_config', 'position', 'widget_data', 'created_at', 'updated_at']
        read_only_fields = ['id', 'widget_data', 'table_id', 'table_name', 'created_at', 'updated_at']
    
    def get_widget_data(self, obj):
        """
        Fetch data for the widget with proper error handling and caching
        """
        try:
            # Check if we're in a request context
            request = self.context.get('request')
            
            # Try to get from cache
            from django.core.cache import cache
            cache_key = f"widget_data_{obj.id}"
            cached_data = cache.get(cache_key)
            
            if cached_data is not None:
                return cached_data
            
            # Get fresh data
            if obj.table:
                data = obj.get_data(limit=100)
                
                # Cache for 5 minutes
                cache.set(cache_key, data, 300)
                
                # Log the data for debugging
                logger.debug(f"Widget {obj.id} data: {data}")
                
                return data
            else:
                return {"error": "No table selected"}
                
        except Exception as e:
            logger.error(f"Error fetching widget {obj.id} data: {str(e)}", exc_info=True)
            return {"error": f"Failed to load data: {str(e)}"}