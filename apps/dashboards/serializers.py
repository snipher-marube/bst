"""
apps/dashboards/serializers.py
================================
DRF serializers for DataTable, Record, Dashboard, Widget, and Workspace.
"""
from uuid import UUID
from datetime import datetime, date
from decimal import Decimal

from rest_framework import serializers
from django.contrib.auth import get_user_model

from apps.dashboards.models import DataTable, Record, Dashboard, Widget, DataAlert, WebhookEndpoint
from apps.workspaces.models import Workspace

import logging

logger = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_json_serializable(obj):
    """Recursively convert non-JSON-serializable objects to safe Python types."""
    if isinstance(obj, dict):
        return {str(k): _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class WorkspaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workspace
        fields = ['id', 'name', 'created_at']
        read_only_fields = ['id', 'created_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get('id'):
            data['id'] = str(data['id'])
        return data


# ---------------------------------------------------------------------------
# DataTable
# ---------------------------------------------------------------------------

class DataTableSerializer(serializers.ModelSerializer):
    workspace_name = serializers.SerializerMethodField()

    class Meta:
        model = DataTable
        fields = [
            'id', 'workspace', 'workspace_name', 'name', 'description',
            'schema', 'record_count', 'is_active',
            'created_at', 'updated_at',
        ]
        # workspace is injected by the view via serializer.save(workspace=ws)
        # — never sent in the request body.
        read_only_fields = ['id', 'workspace', 'record_count', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get('id'):
            data['id'] = str(data['id'])
        if data.get('workspace'):
            data['workspace'] = str(data['workspace'])
        return data

    def get_workspace_name(self, obj):
        return obj.workspace.name if obj.workspace else None


# ---------------------------------------------------------------------------
# DataAlert
# ---------------------------------------------------------------------------

class DataAlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataAlert
        fields = [
            'id', 'workspace', 'table', 'name', 'field_name',
            'aggregate', 'operator', 'threshold', 'is_active',
            'cooldown_minutes', 'last_triggered', 'last_value',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'workspace', 'last_triggered', 'last_value',
            'created_at', 'updated_at',
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for field in ('id', 'workspace', 'table'):
            if data.get(field):
                data[field] = str(data[field])
        return data


# ---------------------------------------------------------------------------
# WebhookEndpoint
# ---------------------------------------------------------------------------

class WebhookEndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookEndpoint
        fields = [
            'id', 'workspace', 'table', 'name', 'token', 'secret',
            'is_active', 'total_requests', 'last_request_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'workspace', 'token', 'secret',
            'total_requests', 'last_request_at',
            'created_at', 'updated_at',
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for field in ('id', 'workspace', 'table'):
            if data.get(field):
                data[field] = str(data[field])
        return data


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

class RecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = Record
        fields = ['id', 'table', 'data', 'version', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'version', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get('id'):
            data['id'] = str(data['id'])
        if data.get('table'):
            data['table'] = str(data['table'])
        return data


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class WidgetSerializer(serializers.ModelSerializer):
    widget_data = serializers.SerializerMethodField()
    table_name  = serializers.SerializerMethodField()

    class Meta:
        model = Widget
        fields = [
            'id', 'dashboard', 'widget_type', 'title', 'table',
            'table_name', 'query_config', 'viz_config', 'position',
            'widget_data', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for field in ('id', 'dashboard', 'table'):
            if data.get(field):
                data[field] = str(data[field])
        return data

    def get_widget_data(self, obj):
        try:
            if not obj.table:
                return {'message': 'No table selected'}
            limit = (obj.query_config or {}).get('limit', 100)
            data  = obj.get_data(limit=limit)
            return _make_json_serializable(data) if isinstance(data, dict) else data
        except Exception as e:
            logger.error(f"Error getting widget data for {obj.id}: {e}")
            return {'error': str(e)}

    def get_table_name(self, obj):
        return obj.table.name if obj.table else None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardSerializer(serializers.ModelSerializer):
    widgets        = serializers.SerializerMethodField()
    workspace_name = serializers.SerializerMethodField()

    class Meta:
        model = Dashboard
        fields = [
            'id', 'workspace', 'workspace_name', 'name', 'description', 'slug',
            'layout_config', 'is_public', 'public_uuid', 'widgets',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'workspace', 'public_uuid', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for field in ('id', 'workspace', 'public_uuid'):
            if data.get(field):
                data[field] = str(data[field])
        return data

    def get_widgets(self, obj):
        try:
            widgets = obj.widgets.all().order_by('created_at')
            return WidgetSerializer(widgets, many=True, context=self.context).data
        except Exception as e:
            logger.error(f"Error getting widgets for dashboard {obj.id}: {e}")
            return []

    def get_workspace_name(self, obj):
        return obj.workspace.name if obj.workspace else None
