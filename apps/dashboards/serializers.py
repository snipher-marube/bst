from rest_framework import serializers
from .models import Workspace, DataTable, Record, Dashboard, Widget
from django.core.exceptions import ValidationError

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
        # Check workspace limits
        workspace = self.context['request'].user.current_workspace
        if not workspace.can_add_table() and not self.instance:
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
        return WidgetSerializer(widgets, many=True).data


class WidgetSerializer(serializers.ModelSerializer):
    widget_data = serializers.SerializerMethodField()
    
    class Meta:
        model = Widget
        fields = ['id', 'dashboard', 'widget_type', 'title', 'table',
                 'query_config', 'viz_config', 'position', 'widget_data']
        read_only_fields = ['id', 'widget_data']
    
    def get_widget_data(self, obj):
        # Only fetch data for GET requests, not for POST/PUT
        if self.context['request'].method == 'GET':
            return obj.get_data(limit=100)
        return None