"""
apps/insights/tests.py
======================
Tests for insights serializers and utilities.
"""
import uuid
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, DashboardFactory, WidgetFactory,
)
from apps.insights.serializers import WidgetSerializer, DashboardSerializer


class TestWidgetSerializer(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.dashboard = DashboardFactory(workspace=self.workspace)

    def test_widget_without_table_returns_message(self):
        widget = WidgetFactory(dashboard=self.dashboard, table=None)
        s = WidgetSerializer(widget)
        data = s.data
        self.assertEqual(data['widget_data'], {'message': 'No table selected'})

    def test_widget_get_table_name_none_when_no_table(self):
        widget = WidgetFactory(dashboard=self.dashboard, table=None)
        s = WidgetSerializer(widget)
        self.assertIsNone(s.data['table_name'])

    def test_to_representation_converts_uuids_to_strings(self):
        widget = WidgetFactory(dashboard=self.dashboard, table=None)
        s = WidgetSerializer(widget)
        data = s.data
        self.assertIsInstance(data['id'], str)
        self.assertIsInstance(data['dashboard'], str)

    def test_make_json_serializable_uuid(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        result = s._make_json_serializable(uuid.uuid4())
        self.assertIsInstance(result, str)

    def test_make_json_serializable_datetime(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        now = datetime(2025, 1, 1, 12, 0, 0)
        result = s._make_json_serializable(now)
        self.assertEqual(result, '2025-01-01T12:00:00')

    def test_make_json_serializable_date(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        d = date(2025, 6, 15)
        result = s._make_json_serializable(d)
        self.assertEqual(result, '2025-06-15')

    def test_make_json_serializable_decimal(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        result = s._make_json_serializable(Decimal('3.14'))
        self.assertAlmostEqual(result, 3.14)

    def test_make_json_serializable_dict(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        result = s._make_json_serializable({'key': Decimal('1.5')})
        self.assertEqual(result, {'key': 1.5})

    def test_make_json_serializable_list(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        result = s._make_json_serializable([Decimal('2.0'), 'hello'])
        self.assertEqual(result, [2.0, 'hello'])

    def test_make_json_serializable_plain_value(self):
        s = WidgetSerializer.__new__(WidgetSerializer)
        self.assertEqual(s._make_json_serializable('text'), 'text')
        self.assertEqual(s._make_json_serializable(42), 42)

    def test_widget_data_error_handling(self):
        """widget_data returns error dict when get_data raises"""
        from apps.dashboards.factories import DataTableFactory
        table = DataTableFactory(workspace=self.workspace)
        widget = WidgetFactory(dashboard=self.dashboard, table=table)
        widget.query_config = {}

        with patch.object(widget, 'get_data', side_effect=RuntimeError("DB error")):
            s = WidgetSerializer(widget)
            data = s.get_widget_data(widget)
        self.assertIn('error', data)


class TestDashboardSerializer(TestCase):

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.dashboard = DashboardFactory(workspace=self.workspace)

    def test_serializes_dashboard_fields(self):
        s = DashboardSerializer(self.dashboard)
        data = s.data
        self.assertEqual(data['name'], self.dashboard.name)
        self.assertIsInstance(data['id'], str)
        self.assertIsInstance(data['workspace'], str)

    def test_get_workspace_name(self):
        s = DashboardSerializer(self.dashboard)
        self.assertEqual(s.data['workspace_name'], self.workspace.name)

    def test_get_workspace_name_none_when_no_workspace(self):
        s = DashboardSerializer.__new__(DashboardSerializer)
        mock_obj = MagicMock()
        mock_obj.workspace = None
        self.assertIsNone(s.get_workspace_name(mock_obj))

    def test_get_widgets_returns_list(self):
        s = DashboardSerializer(self.dashboard)
        self.assertIsInstance(s.data['widgets'], list)

    def test_get_widgets_error_returns_empty_list(self):
        s = DashboardSerializer.__new__(DashboardSerializer)
        mock_obj = MagicMock()
        mock_obj.widgets.all.side_effect = RuntimeError("fail")
        mock_obj.id = uuid.uuid4()
        result = s.get_widgets(mock_obj)
        self.assertEqual(result, [])

    def test_to_representation_public_uuid_as_string(self):
        s = DashboardSerializer(self.dashboard)
        data = s.data
        if data.get('public_uuid'):
            self.assertIsInstance(data['public_uuid'], str)
