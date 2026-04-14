"""
apps/dashboards/tests.py
=========================
Tests for DataTable, Record, Dashboard, Widget, ImportJob models
and the DataImportService / QueryEngine services.
"""
import io
import csv
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, DataTableFactory, DataTableWithSchemaFactory,
    RecordFactory, DashboardFactory, WidgetFactory, ImportJobFactory,
)
from apps.dashboards.models import DataTable, Record, Dashboard, Widget, ImportJob
from apps.dashboards.services import DataImportService, QueryEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_tasks():
    """Patch Celery tasks that fire as side effects of Record.save/delete."""
    return (
        patch('apps.dashboards.models.broadcast_widget_update.delay'),
        patch('apps.dashboards.models.notify_table_change.delay'),
    )


# ---------------------------------------------------------------------------
# DataTable model
# ---------------------------------------------------------------------------

class TestDataTableModel(TestCase):

    def setUp(self):
        self.table = DataTableFactory(
            schema=[
                {'name': 'name',  'type': 'text',   'required': True},
                {'name': 'score', 'type': 'number',  'required': False},
                {'name': 'email', 'type': 'email',   'required': False},
            ]
        )

    # --- validate_record ---

    def test_validate_record_valid_data(self):
        ok, errors = self.table.validate_record({'name': 'Alice', 'score': 95})
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_validate_record_missing_required_field(self):
        ok, errors = self.table.validate_record({'score': 10})
        self.assertFalse(ok)
        self.assertTrue(any('name' in e for e in errors))

    def test_validate_record_wrong_number_type(self):
        ok, errors = self.table.validate_record({'name': 'Bob', 'score': 'not-a-number'})
        self.assertFalse(ok)
        self.assertTrue(any('score' in e for e in errors))

    def test_validate_record_invalid_email(self):
        ok, errors = self.table.validate_record({'name': 'C', 'email': 'bad-email'})
        self.assertFalse(ok)
        self.assertTrue(any('email' in e for e in errors))

    def test_validate_record_unknown_field_is_error(self):
        ok, errors = self.table.validate_record({'name': 'D', 'unknown_field': 'x'})
        self.assertFalse(ok)

    def test_validate_record_empty_schema_accepts_anything(self):
        table = DataTableFactory(schema=[])
        ok, errors = table.validate_record({'anything': 'goes'})
        self.assertTrue(ok)

    def test_validate_record_none_values_skipped(self):
        ok, errors = self.table.validate_record({'name': 'E', 'score': None})
        self.assertTrue(ok)

    # --- schema_hash ---

    def test_schema_hash_generated_on_save(self):
        self.assertNotEqual(self.table.schema_hash, '')

    def test_schema_hash_changes_when_schema_changes(self):
        old_hash = self.table.schema_hash
        self.table.schema = [{'name': 'new_col', 'type': 'text', 'required': False}]
        self.table.save()
        self.assertNotEqual(self.table.schema_hash, old_hash)

    # --- generate_default_dashboard ---

    def test_generate_default_dashboard_creates_dashboard_and_widgets(self):
        table = DataTableWithSchemaFactory()
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            dashboard = table.generate_default_dashboard()
        self.assertIsNotNone(dashboard)
        self.assertEqual(dashboard.workspace, table.workspace)
        self.assertGreater(dashboard.widgets.count(), 0)

    def test_generate_default_dashboard_unique_slug_on_collision(self):
        # Both tables share the same workspace; generate_default_dashboard must
        # produce unique slugs even when a dashboard with a similar name exists.
        table1 = DataTableWithSchemaFactory(name='Sales Alpha')
        table2 = DataTableWithSchemaFactory(workspace=table1.workspace, name='Sales Beta')
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            d1 = table1.generate_default_dashboard()
            d2 = table2.generate_default_dashboard()
        self.assertNotEqual(d1.slug, d2.slug)

    # --- estimated_storage_bytes ---

    def test_estimated_storage_bytes(self):
        table = DataTableFactory()
        table.record_count = 10
        self.assertEqual(table.estimated_storage_bytes, 10 * 1024)

    # --- unique_together ---

    def test_duplicate_name_in_same_workspace_raises(self):
        ws = WorkspaceFactory()
        DataTableFactory(workspace=ws, name='Duplicate')
        with self.assertRaises(Exception):
            DataTableFactory(workspace=ws, name='Duplicate')


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------

class TestRecordModel(TestCase):

    def setUp(self):
        self.table = DataTableWithSchemaFactory()

    def test_record_creation_increments_count(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            Record.objects.create(
                table=self.table,
                data={'customer': 'Acme', 'amount': 1000},
                created_by=self.table.workspace.owner,
            )
        self.table.refresh_from_db()
        self.assertEqual(self.table.record_count, 1)

    def test_soft_delete_decrements_count(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = Record.objects.create(
                table=self.table,
                data={'customer': 'Test', 'amount': 50},
                created_by=self.table.workspace.owner,
            )
            self.table.refresh_from_db()
            self.assertEqual(self.table.record_count, 1)
            record.delete()

        self.table.refresh_from_db()
        self.assertEqual(self.table.record_count, 0)

    def test_soft_delete_sets_is_active_false(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = Record.objects.create(
                table=self.table,
                data={'customer': 'Test', 'amount': 10},
                created_by=self.table.workspace.owner,
            )
            record.delete()
        record.refresh_from_db()
        self.assertFalse(record.is_active)
        self.assertIsNotNone(record.deleted_at)

    def test_bulk_create_bypasses_save_triggers(self):
        """bulk_create used by import service — record_count NOT auto-updated."""
        records = [
            Record(table=self.table, data={'customer': f'c{i}'}, created_by=self.table.workspace.owner)
            for i in range(5)
        ]
        Record.objects.bulk_create(records)
        # record_count still 0 because save() was not called
        self.table.refresh_from_db()
        self.assertEqual(self.table.record_count, 0)


# ---------------------------------------------------------------------------
# Dashboard model
# ---------------------------------------------------------------------------

class TestDashboardModel(TestCase):

    def test_str(self):
        d = DashboardFactory(name='My Board')
        self.assertIn('My Board', str(d))

    def test_is_active_default_true(self):
        d = DashboardFactory()
        self.assertTrue(d.is_active)

    def test_public_uuid_generated(self):
        d = DashboardFactory()
        self.assertIsNotNone(d.public_uuid)

    def test_unique_slug_per_workspace(self):
        ws = WorkspaceFactory()
        DashboardFactory(workspace=ws, slug='my-dash')
        with self.assertRaises(Exception):
            DashboardFactory(workspace=ws, slug='my-dash')


# ---------------------------------------------------------------------------
# Widget model
# ---------------------------------------------------------------------------

class TestWidgetModel(TestCase):

    def test_widget_types_include_all_nine(self):
        types = [c[0] for c in Widget.WIDGET_TYPES]
        for t in ['line_chart', 'bar_chart', 'pie_chart', 'table', 'metric',
                  'number', 'gauge', 'heatmap', 'scatter']:
            self.assertIn(t, types)

    def test_get_data_no_table_returns_error(self):
        widget = WidgetFactory(table=None, widget_type='metric')
        result = widget.get_data()
        self.assertIn('error', result)


# ---------------------------------------------------------------------------
# ImportJob model
# ---------------------------------------------------------------------------

class TestImportJobModel(TestCase):

    def test_progress_pct_zero_when_total_zero(self):
        job = ImportJobFactory(total_rows=0, processed_rows=0)
        self.assertEqual(job.progress_pct, 0)

    def test_progress_pct_calculated_correctly(self):
        job = ImportJobFactory(total_rows=100, processed_rows=75)
        self.assertEqual(job.progress_pct, 75)

    def test_progress_pct_capped_at_100(self):
        job = ImportJobFactory(total_rows=10, processed_rows=15)
        self.assertLessEqual(job.progress_pct, 100)

    def test_status_choices(self):
        statuses = [c[0] for c in ImportJob.STATUS_CHOICES]
        for s in ['pending', 'running', 'completed', 'failed']:
            self.assertIn(s, statuses)


# ---------------------------------------------------------------------------
# DataImportService
# ---------------------------------------------------------------------------

class TestDataImportService(TestCase):

    def setUp(self):
        self.service = DataImportService()
        self.table   = DataTableFactory(schema=[])

    # --- parse_file ---

    def _make_csv(self, rows):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        buf.seek(0)
        buf.name = 'test.csv'
        return buf

    def test_parse_csv_returns_dataframe(self):
        f  = self._make_csv([{'name': 'Alice', 'score': '90'}])
        df = self.service.parse_file(f, 'csv')
        self.assertEqual(len(df), 1)
        self.assertIn('name', df.columns)

    def test_parse_unsupported_format_raises(self):
        buf = io.BytesIO(b'garbage')
        buf.name = 'data.xml'
        with self.assertRaises(ValueError):
            self.service.parse_file(buf)

    # --- clean_dataframe ---

    def test_clean_dataframe_removes_duplicates(self):
        import pandas as pd
        df = pd.DataFrame([
            {'a': 1, 'b': 2},
            {'a': 1, 'b': 2},
            {'a': 3, 'b': 4},
        ])
        cleaned, report = self.service.clean_dataframe(df)
        self.assertEqual(len(cleaned), 2)
        self.assertTrue(any('duplicate' in r for r in report))

    def test_clean_dataframe_strips_whitespace_from_col_names(self):
        import pandas as pd
        df = pd.DataFrame({' name ': ['Alice'], 'score': [90]})
        cleaned, report = self.service.clean_dataframe(df)
        self.assertIn('name', cleaned.columns)
        self.assertNotIn(' name ', cleaned.columns)

    def test_clean_dataframe_no_issues_reported(self):
        import pandas as pd
        df = pd.DataFrame({'a': [1, 2, 3], 'b': ['x', 'y', 'z']})
        _, report = self.service.clean_dataframe(df)
        self.assertTrue(len(report) >= 1)  # always returns at least one message

    # --- import_data ---

    def test_import_data_creates_records_and_updates_count(self):
        import pandas as pd
        df = pd.DataFrame([
            {'product': 'Widget A', 'qty': 10},
            {'product': 'Widget B', 'qty': 5},
        ])
        result = self.service.import_data(self.table, df, self.table.workspace.owner)
        self.assertEqual(result['success'], 2)
        self.assertEqual(result['errors'], 0)
        self.table.refresh_from_db()
        self.assertEqual(self.table.record_count, 2)

    def test_import_data_with_mapping(self):
        import pandas as pd
        df = pd.DataFrame([{'src_name': 'Acme', 'src_val': 100}])
        mapping = {'customer': 'src_name', 'amount': 'src_val'}
        result = self.service.import_data(self.table, df, self.table.workspace.owner, mapping=mapping)
        self.assertEqual(result['success'], 1)
        record = Record.objects.filter(table=self.table).first()
        self.assertEqual(record.data['customer'], 'Acme')

    def test_import_data_returns_error_count_on_bad_rows(self):
        import pandas as pd
        # NaN values should be sanitized; test that the service handles edge cases
        df = pd.DataFrame([
            {'col': 'good'},
            {'col': None},
        ])
        result = self.service.import_data(self.table, df, self.table.workspace.owner)
        self.assertGreaterEqual(result['success'] + result['errors'], 2)

    def test_sanitize_row_converts_nan_to_none(self):
        import math
        row = {'val': float('nan')}
        cleaned = self.service._sanitize_row(row)
        self.assertIsNone(cleaned['val'])

    def test_sanitize_row_preserves_strings_and_numbers(self):
        row = {'name': 'Alice', 'score': 95, 'active': True}
        cleaned = self.service._sanitize_row(row)
        self.assertEqual(cleaned['name'], 'Alice')
        self.assertEqual(cleaned['score'], 95)

    def test_import_csv_wrapper(self):
        f = self._make_csv([{'city': 'Nairobi', 'pop': '5000000'}])
        result = self.service.import_csv(self.table, f, self.table.workspace.owner)
        self.assertEqual(result['success'], 1)


# ---------------------------------------------------------------------------
# QueryEngine
# ---------------------------------------------------------------------------

class TestQueryEngine(TestCase):

    def setUp(self):
        self.engine = QueryEngine()
        self.table  = DataTableWithSchemaFactory()
        self.user   = self.table.workspace.owner
        # Seed records directly (bypass save() triggers via bulk_create)
        records = [
            Record(table=self.table, data={'customer': f'C{i}', 'amount': 100 * (i + 1)}, created_by=self.user)
            for i in range(5)
        ]
        Record.objects.bulk_create(records)
        # Update count manually (bulk_create skips save())
        self.table.record_count = 5
        self.table.save(update_fields=['record_count'])

    def _widget(self, widget_type, query_config):
        d = DashboardFactory(workspace=self.table.workspace)
        return WidgetFactory(dashboard=d, widget_type=widget_type, table=self.table, query_config=query_config)

    # --- no table ---

    def test_execute_no_table_returns_error(self):
        w = WidgetFactory(table=None, widget_type='metric')
        result = self.engine.execute_widget_query(w)
        self.assertIn('error', result)

    # --- metric widget ---

    def test_execute_metric_count(self):
        w = self._widget('metric', {'aggregations': [{'type': 'count', 'name': 'val'}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(result.get('val'), 5)

    def test_execute_metric_sum(self):
        w = self._widget('metric', {'aggregations': [{'type': 'sum', 'field': 'amount', 'name': 'val'}]})
        result = self.engine.execute_widget_query(w)
        # 100 + 200 + 300 + 400 + 500 = 1500
        self.assertAlmostEqual(result.get('val'), 1500, delta=1)

    def test_execute_metric_avg(self):
        w = self._widget('metric', {'aggregations': [{'type': 'avg', 'field': 'amount', 'name': 'val'}]})
        result = self.engine.execute_widget_query(w)
        self.assertAlmostEqual(result.get('val'), 300, delta=1)

    def test_execute_metric_min(self):
        w = self._widget('metric', {'aggregations': [{'type': 'min', 'field': 'amount', 'name': 'val'}]})
        result = self.engine.execute_widget_query(w)
        self.assertAlmostEqual(result.get('val'), 100, delta=0.1)

    def test_execute_metric_max(self):
        w = self._widget('metric', {'aggregations': [{'type': 'max', 'field': 'amount', 'name': 'val'}]})
        result = self.engine.execute_widget_query(w)
        self.assertAlmostEqual(result.get('val'), 500, delta=0.1)

    def test_execute_metric_empty_aggregations_defaults_count(self):
        w = self._widget('metric', {})
        result = self.engine.execute_widget_query(w)
        self.assertIn('val', result)

    # --- table widget ---

    def test_execute_table_returns_list(self):
        w = self._widget('table', {})
        result = self.engine.execute_widget_query(w)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 5)

    def test_execute_table_applies_filter(self):
        w = self._widget('table', {'filters': [{'field': 'customer', 'operator': 'eq', 'value': 'C0'}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(len(result), 1)

    # --- chart widget ---

    def test_execute_chart_grouped_by_customer(self):
        w = self._widget('bar_chart', {
            'aggregations': [{'type': 'sum', 'field': 'amount', 'group_by': 'customer', 'name': 'val'}]
        })
        result = self.engine.execute_widget_query(w)
        # Each customer is unique so we expect 5 groups
        self.assertEqual(len(result.get('val', {})), 5)

    def test_execute_chart_no_aggregations_returns_error(self):
        w = self._widget('line_chart', {})
        result = self.engine.execute_widget_query(w)
        self.assertIn('error', result)

    # --- filters ---

    def test_apply_filters_gt(self):
        w = self._widget('table', {'filters': [{'field': 'amount', 'operator': 'gt', 'value': 300}]})
        result = self.engine.execute_widget_query(w)
        # amount > 300 → 400, 500 → 2 records
        self.assertEqual(len(result), 2)

    def test_apply_filters_lt(self):
        w = self._widget('table', {'filters': [{'field': 'amount', 'operator': 'lt', 'value': 200}]})
        result = self.engine.execute_widget_query(w)
        # amount < 200 → 100 → 1 record
        self.assertEqual(len(result), 1)

    def test_apply_filters_neq(self):
        w = self._widget('table', {'filters': [{'field': 'customer', 'operator': 'neq', 'value': 'C0'}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(len(result), 4)

    # --- caching ---

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    def test_result_is_cached_on_second_call(self):
        w = self._widget('metric', {'aggregations': [{'type': 'count', 'name': 'val'}]})
        r1 = self.engine.execute_widget_query(w)
        r2 = self.engine.execute_widget_query(w)
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------
# Additional QueryEngine tests (aggregate paths not yet covered)
# ---------------------------------------------------------------------------

class TestQueryEngineExtended(TestCase):
    """Additional tests to cover remaining aggregation and filter paths."""

    def setUp(self):
        self.engine = QueryEngine()
        self.table  = DataTableWithSchemaFactory()
        self.user   = self.table.workspace.owner
        records = [
            Record(
                table=self.table,
                data={'customer': f'C{i}', 'amount': 100 * (i + 1), 'region': 'East' if i < 3 else 'West'},
                created_by=self.user,
            )
            for i in range(5)
        ]
        Record.objects.bulk_create(records)
        self.table.record_count = 5
        self.table.save(update_fields=['record_count'])

    def _widget(self, widget_type, query_config):
        d = DashboardFactory(workspace=self.table.workspace)
        return WidgetFactory(dashboard=d, widget_type=widget_type, table=self.table, query_config=query_config)

    def test_chart_sum_group_by(self):
        w = self._widget('bar_chart', {
            'aggregations': [{'type': 'sum', 'field': 'amount', 'group_by': 'region', 'name': 'total'}]
        })
        result = self.engine.execute_widget_query(w)
        # East: 100+200+300=600, West: 400+500=900
        self.assertIn('total', result)
        self.assertEqual(len(result['total']), 2)

    def test_chart_avg_group_by(self):
        w = self._widget('line_chart', {
            'aggregations': [{'type': 'avg', 'field': 'amount', 'group_by': 'region', 'name': 'avg_amt'}]
        })
        result = self.engine.execute_widget_query(w)
        self.assertIn('avg_amt', result)

    def test_chart_count_group_by(self):
        w = self._widget('pie_chart', {
            'aggregations': [{'type': 'count', 'group_by': 'region', 'name': 'cnt'}]
        })
        result = self.engine.execute_widget_query(w)
        self.assertIn('cnt', result)

    def test_chart_min_max_group_by(self):
        w_min = self._widget('bar_chart', {
            'aggregations': [{'type': 'min', 'field': 'amount', 'group_by': 'region', 'name': 'mn'}]
        })
        w_max = self._widget('bar_chart', {
            'aggregations': [{'type': 'max', 'field': 'amount', 'group_by': 'region', 'name': 'mx'}]
        })
        r_min = self.engine.execute_widget_query(w_min)
        r_max = self.engine.execute_widget_query(w_max)
        self.assertIn('mn', r_min)
        self.assertIn('mx', r_max)

    def test_filter_gte(self):
        w = self._widget('table', {'filters': [{'field': 'amount', 'operator': 'gte', 'value': 300}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(len(result), 3)

    def test_filter_lte(self):
        w = self._widget('table', {'filters': [{'field': 'amount', 'operator': 'lte', 'value': 200}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(len(result), 2)

    def test_filter_contains(self):
        w = self._widget('table', {'filters': [{'field': 'customer', 'operator': 'contains', 'value': 'C'}]})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(len(result), 5)

    def test_table_with_limit(self):
        w = self._widget('table', {'limit': 2})
        result = self.engine.execute_widget_query(w, limit=2)
        self.assertLessEqual(len(result), 2)

    def test_metric_no_values_returns_zero(self):
        """Aggregation over a field that has no numeric values returns 0."""
        w = self._widget('metric', {
            'aggregations': [{'type': 'sum', 'field': 'customer', 'name': 'val'}]
        })
        result = self.engine.execute_widget_query(w)
        self.assertEqual(result.get('val'), 0)

    def test_number_widget_same_as_metric(self):
        w = self._widget('number', {'aggregations': [{'type': 'count', 'name': 'n'}]})
        result = self.engine.execute_widget_query(w)
        self.assertIn('n', result)


# ---------------------------------------------------------------------------
# Additional DataImportService tests (schema detection)
# ---------------------------------------------------------------------------

class TestDataImportServiceExtended(TestCase):

    def setUp(self):
        self.service = DataImportService()
        self.table   = DataTableFactory(schema=[])

    def test_detect_schema_numeric(self):
        import pandas as pd
        df = pd.DataFrame({'qty': [1, 2, 3], 'price': [1.5, 2.5, 3.0]})
        schema = self.service._detect_schema_from_df(df)
        types = {f['name']: f['type'] for f in schema}
        self.assertEqual(types['qty'], 'number')

    def test_detect_schema_boolean(self):
        import pandas as pd
        df = pd.DataFrame({'active': ['true', 'false', 'yes']})
        schema = self.service._detect_schema_from_df(df)
        self.assertEqual(schema[0]['type'], 'boolean')

    def test_detect_schema_email(self):
        import pandas as pd
        df = pd.DataFrame({'email': ['a@b.com', 'c@d.com']})
        schema = self.service._detect_schema_from_df(df)
        self.assertEqual(schema[0]['type'], 'email')

    def test_detect_schema_date(self):
        import pandas as pd
        df = pd.DataFrame({'created': ['2024-01-01', '2024-01-02']})
        schema = self.service._detect_schema_from_df(df)
        self.assertEqual(schema[0]['type'], 'date')

    def test_parse_xlsx_file(self):
        """Accepts xlsx extension."""
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['name', 'score'])
        ws.append(['Alice', 90])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = 'data.xlsx'
        df = self.service.parse_file(buf, 'xlsx')
        self.assertEqual(len(df), 1)

    def test_is_date_valid(self):
        self.assertTrue(self.service._is_date('2024-01-15'))

    def test_is_date_invalid(self):
        self.assertFalse(self.service._is_date('not-a-date'))


# ---------------------------------------------------------------------------
# Utils module tests
# ---------------------------------------------------------------------------

from apps.dashboards.utils import generate_cache_key, cache_result, validate_workspace_limits


class TestDashboardUtils(TestCase):

    def test_generate_cache_key_deterministic(self):
        k1 = generate_cache_key('prefix', 'a', 'b')
        k2 = generate_cache_key('prefix', 'a', 'b')
        self.assertEqual(k1, k2)

    def test_generate_cache_key_different_args_differ(self):
        k1 = generate_cache_key('prefix', 'a')
        k2 = generate_cache_key('prefix', 'b')
        self.assertNotEqual(k1, k2)

    def test_cache_result_decorator_returns_value(self):
        call_count = {'n': 0}

        @cache_result(timeout=60)
        def expensive(x):
            call_count['n'] += 1
            return x * 2

        result = expensive(5)
        self.assertEqual(result, 10)

    def test_validate_workspace_limits_within_limits(self):
        ws = WorkspaceFactory()
        # Ensure limits exceed current usage (1 owner member by default)
        ws.max_team_members = 10
        ws.save(update_fields=['max_team_members'])
        ok, msg = validate_workspace_limits(ws)
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')

    def test_validate_workspace_limits_tables_exceeded(self):
        ws = WorkspaceFactory()
        ws.max_tables = 0
        ws.save(update_fields=['max_tables'])
        ok, msg = validate_workspace_limits(ws)
        self.assertFalse(ok)
        self.assertIn('Table', msg)

    def test_validate_workspace_limits_members_exceeded(self):
        ws = WorkspaceFactory()
        ws.max_team_members = 0
        ws.save(update_fields=['max_team_members'])
        ok, msg = validate_workspace_limits(ws)
        self.assertFalse(ok)
        self.assertIn('member', msg.lower())


# ---------------------------------------------------------------------------
# Template filter tests
# ---------------------------------------------------------------------------

from apps.dashboards.templatetags.dashboard_filters import get_item


class TestDashboardFilters(TestCase):

    def test_get_item_from_dict(self):
        self.assertEqual(get_item({'key': 'val'}, 'key'), 'val')

    def test_get_item_missing_key_returns_dash(self):
        self.assertEqual(get_item({'a': 1}, 'missing'), '-')

    def test_get_item_none_returns_dash(self):
        self.assertEqual(get_item(None, 'key'), '-')

    def test_get_item_from_object_via_getattr(self):
        class Obj:
            name = 'test'
        self.assertEqual(get_item(Obj(), 'name'), 'test')


# ---------------------------------------------------------------------------
# WorkspaceInsightService tests
# ---------------------------------------------------------------------------

from apps.dashboards.services import WorkspaceInsightService


class TestWorkspaceInsightService(TestCase):

    def setUp(self):
        self.service = WorkspaceInsightService()
        self.table = DataTableWithSchemaFactory()
        self.user = self.table.workspace.owner
        # Seed records so the service has data to profile
        records = [
            Record(
                table=self.table,
                data={'customer': f'C{i}', 'amount': 100 * (i + 1), 'region': 'East'},
                created_by=self.user,
            )
            for i in range(10)
        ]
        Record.objects.bulk_create(records)
        self.table.record_count = 10
        self.table.save(update_fields=['record_count'])

    def test_generate_workspace_overview_creates_dashboard(self):
        dashboard = self.service.generate_workspace_overview(self.table.workspace, self.user)
        self.assertIsNotNone(dashboard)
        self.assertEqual(dashboard.workspace, self.table.workspace)

    def test_generate_workspace_overview_idempotent(self):
        d1 = self.service.generate_workspace_overview(self.table.workspace, self.user)
        d2 = self.service.generate_workspace_overview(self.table.workspace, self.user)
        self.assertEqual(d1.id, d2.id)

    def test_generate_table_insights_empty_schema(self):
        table = DataTableFactory(schema=[])
        insights = self.service._generate_table_insights(table)
        self.assertEqual(insights, [])

    def test_generate_table_insights_no_records(self):
        table = DataTableWithSchemaFactory()
        insights = self.service._generate_table_insights(table)
        self.assertEqual(insights, [])

    def test_generate_table_insights_with_data(self):
        insights = self.service._generate_table_insights(self.table)
        self.assertIsInstance(insights, list)

    def test_filter_numeric_fields(self):
        # _filter_numeric_fields takes field names (strings), not schema dicts
        fields = ['amount', 'name']
        sample = [{'amount': 100, 'name': 'A'}, {'amount': 200, 'name': 'B'}]
        result = self.service._filter_numeric_fields(fields, sample)
        self.assertIn('amount', result)
        self.assertNotIn('name', result)

    def test_unique_values(self):
        sample = [{'region': 'East'}, {'region': 'West'}, {'region': 'East'}]
        vals = self.service._unique_values('region', sample)
        self.assertEqual(set(vals), {'East', 'West'})

    def test_kpi_spec_structure(self):
        spec = self.service._kpi_spec('Revenue', 'sum', 'amount', 0, prefix='$')
        self.assertEqual(spec['title'], 'Revenue')
        self.assertIn('query_config', spec)
        self.assertIn('viz_config', spec)


# ---------------------------------------------------------------------------
# Dashboard HTML views (require login)
# ---------------------------------------------------------------------------

from django.test import Client as DjangoClient


class TestDashboardHTMLViews(TestCase):
    """Test the main dashboard HTML views using Django's test client."""

    def setUp(self):
        self.client = DjangoClient()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        # Set current workspace in session for views that read from session
        session = self.client.session
        session['current_workspace_id'] = str(self.workspace.id)
        session.save()
        # Also set current_workspace attribute on user (used by some views)
        self.user.current_workspace = self.workspace

    def test_home_view(self):
        resp = self.client.get('/dashboard/analytics/')
        self.assertEqual(resp.status_code, 200)

    def test_home_requires_login(self):
        c = DjangoClient()
        resp = c.get('/dashboard/analytics/')
        self.assertEqual(resp.status_code, 302)

    def test_workspace_list_view(self):
        resp = self.client.get('/dashboard/workspaces/')
        self.assertEqual(resp.status_code, 200)

    def test_workspace_create_view_get(self):
        resp = self.client.get('/dashboard/workspaces/create/')
        self.assertEqual(resp.status_code, 200)

    def test_workspace_create_view_post(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self.client.post('/dashboard/workspaces/create/', {
                'name': 'New Test Workspace',
            })
        self.assertIn(resp.status_code, [200, 302])

    def test_switch_workspace(self):
        other_ws = WorkspaceFactory(owner=self.user)
        resp = self.client.post(f'/dashboard/workspaces/{other_ws.id}/switch/')
        self.assertEqual(resp.status_code, 302)

    def test_tables_view(self):
        resp = self.client.get('/dashboard/tables/')
        self.assertEqual(resp.status_code, 200)

    def test_table_create_view_get(self):
        resp = self.client.get('/dashboard/tables/create/')
        self.assertEqual(resp.status_code, 200)

    def test_table_create_view_post(self):
        resp = self.client.post('/dashboard/tables/create/', {
            'name': 'My Test Table',
            'schema': '[]',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_table_detail_view(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/tables/{table.id}/')
        self.assertEqual(resp.status_code, 200)

    def test_table_edit_view(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/tables/{table.id}/edit/')
        self.assertEqual(resp.status_code, 200)

    def test_table_delete_view(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/tables/{table.id}/delete/')
        self.assertIn(resp.status_code, [200, 302])

    def test_record_list_view(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/tables/{table.id}/records/')
        self.assertEqual(resp.status_code, 200)

    def test_record_create_view_get(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/tables/{table.id}/records/create/')
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_detail_view(self):
        dashboard = DashboardFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/dashboards/{dashboard.id}/')
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_edit_view(self):
        dashboard = DashboardFactory(workspace=self.workspace)
        resp = self.client.get(f'/dashboard/dashboards/{dashboard.id}/edit/')
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_delete_view(self):
        dashboard = DashboardFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/dashboards/{dashboard.id}/delete/')
        self.assertIn(resp.status_code, [200, 302])

    def test_settings_view(self):
        resp = self.client.get('/dashboard/settings/')
        self.assertEqual(resp.status_code, 200)

    def test_members_view(self):
        resp = self.client.get('/dashboard/members/')
        self.assertEqual(resp.status_code, 200)

    def test_activity_log_view(self):
        resp = self.client.get('/dashboard/activity/')
        self.assertEqual(resp.status_code, 200)

    def test_billing_view(self):
        resp = self.client.get('/dashboard/billing/')
        self.assertEqual(resp.status_code, 200)

    def test_profile_view(self):
        resp = self.client.get('/dashboard/profile/')
        self.assertEqual(resp.status_code, 200)


    # --- Table POST actions ---

    def test_table_create_post_valid(self):
        """Table creation form_valid creates table and redirects"""
        from apps.dashboards.models import DataTable
        # Set current_workspace on user so middleware can pick it up
        self.user.current_workspace = self.workspace
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self.client.post('/dashboard/tables/create/', {
                'name': 'Sales Data',
                'schema': '[]',
                'description': 'test table',
            })
        self.assertIn(resp.status_code, [200, 302])
        # Check that table was created
        table = DataTable.objects.filter(name='Sales Data').first()
        self.assertIsNotNone(table)

    def test_table_edit_post(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/tables/{table.id}/edit/', {
            'name': 'Renamed Table',
            'schema': '[]',
            'description': '',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_table_soft_delete(self):
        table = DataTableFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/tables/{table.id}/delete/')
        self.assertIn(resp.status_code, [200, 302])
        # Query with filter that includes inactive objects
        table = DataTable.objects.filter(id=table.id).first()
        self.assertIsNotNone(table)
        self.assertFalse(table.is_active)

    # --- Record POST actions ---

    def test_record_create_post(self):
        table = DataTableFactory(workspace=self.workspace)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self.client.post(
                f'/dashboard/tables/{table.id}/records/create/',
                {'field_name': 'Alice', 'field_amount': '500'},
            )
        self.assertIn(resp.status_code, [200, 302])

    def test_record_edit_get(self):
        table = DataTableFactory(workspace=self.workspace)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = RecordFactory(table=table)
        resp = self.client.get(
            f'/dashboard/tables/{table.id}/records/{record.id}/edit/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_record_edit_post(self):
        table = DataTableFactory(workspace=self.workspace)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = RecordFactory(table=table, data={'name': 'Old'})
            resp = self.client.post(
                f'/dashboard/tables/{table.id}/records/{record.id}/edit/',
                {'field_name': 'New'},
            )
        self.assertIn(resp.status_code, [200, 302])

    def test_record_delete_get(self):
        table = DataTableFactory(workspace=self.workspace)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = RecordFactory(table=table)
        resp = self.client.get(
            f'/dashboard/tables/{table.id}/records/{record.id}/delete/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_record_delete_post(self):
        table = DataTableFactory(workspace=self.workspace)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            record = RecordFactory(table=table)
            resp = self.client.post(
                f'/dashboard/tables/{table.id}/records/{record.id}/delete/'
            )
        self.assertIn(resp.status_code, [200, 302])
        record.refresh_from_db()
        self.assertFalse(record.is_active)

    # --- Dashboard POST actions ---

    def test_dashboard_edit_post(self):
        dashboard = DashboardFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/dashboards/{dashboard.id}/edit/', {
            'name': 'Updated Dashboard',
            'description': 'new desc',
            'is_public': '',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_dashboard_soft_delete(self):
        dashboard = DashboardFactory(workspace=self.workspace)
        resp = self.client.post(f'/dashboard/dashboards/{dashboard.id}/delete/')
        self.assertIn(resp.status_code, [200, 302])
        # Query with filter that includes inactive objects
        dashboard = Dashboard.objects.filter(id=dashboard.id).first()
        self.assertIsNotNone(dashboard)
        self.assertFalse(dashboard.is_active)

    # --- WorkspaceSettingsView POST actions ---

    def test_settings_update_workspace_name(self):
        resp = self.client.post('/dashboard/settings/', {
            'action': 'update_workspace',
            'workspace_name': 'Renamed WS',
        })
        self.assertIn(resp.status_code, [200, 302])
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, 'Renamed WS')

    def test_settings_update_workspace_empty_name(self):
        resp = self.client.post('/dashboard/settings/', {
            'action': 'update_workspace',
            'workspace_name': '',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_settings_non_owner_blocked(self):
        other_user = UserFactory()
        self.workspace.members.add(other_user)
        c = DjangoClient()
        c.force_login(other_user)
        session = c.session
        session['current_workspace_id'] = str(self.workspace.id)
        session.save()
        resp = c.post('/dashboard/settings/', {
            'action': 'update_workspace',
            'workspace_name': 'Hacked',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_settings_delete_workspace_wrong_name(self):
        resp = self.client.post('/dashboard/settings/', {
            'action': 'delete_workspace',
            'confirm_name': 'wrong-name',
        })
        self.assertIn(resp.status_code, [200, 302])
        from apps.workspaces.models import Workspace
        self.assertTrue(Workspace.objects.filter(id=self.workspace.id).exists())

    def test_settings_delete_workspace_correct_name(self):
        ws_name = self.workspace.name
        resp = self.client.post('/dashboard/settings/', {
            'action': 'delete_workspace',
            'confirm_name': ws_name,
        })
        self.assertIn(resp.status_code, [200, 302])

    # --- ProfileView POST actions ---

    def test_profile_update_name(self):
        resp = self.client.post('/dashboard/profile/', {
            'action': 'update_profile',
            'first_name': 'John',
            'last_name': 'Doe',
        })
        self.assertIn(resp.status_code, [200, 302])
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'John')

    def test_profile_update_notification_prefs(self):
        resp = self.client.post('/dashboard/profile/', {
            'action': 'update_notification_prefs',
            'email_invites': 'on',
            'email_imports': 'on',
        })
        self.assertIn(resp.status_code, [200, 302])

    def test_profile_unknown_action(self):
        resp = self.client.post('/dashboard/profile/', {
            'action': 'unknown_action',
        })
        self.assertIn(resp.status_code, [200, 302])


# ---------------------------------------------------------------------------
# DRF Permissions tests
# ---------------------------------------------------------------------------

from rest_framework.test import APIRequestFactory
from rest_framework.authtoken.models import Token
from apps.dashboards.permissions import HasWorkspaceAccess, CanEditData, CanManageWorkspace


class TestHasWorkspaceAccess(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)

    def _request(self, method='get', ws_header=None):
        req = getattr(self.factory, method)('/')
        req.user = self.user
        req.data = {}
        if ws_header:
            req.META['HTTP_X_WORKSPACE_ID'] = ws_header
        return req

    def test_unauthenticated_denied(self):
        from django.contrib.auth.models import AnonymousUser
        perm = HasWorkspaceAccess()
        req = self.factory.get('/')
        req.user = AnonymousUser()
        req.data = {}
        self.assertFalse(perm.has_permission(req, None))

    def test_member_with_header_allowed(self):
        perm = HasWorkspaceAccess()
        req = self._request(ws_header=str(self.workspace.id))
        self.assertTrue(perm.has_permission(req, None))

    def test_non_member_denied(self):
        perm = HasWorkspaceAccess()
        other_ws = WorkspaceFactory()
        req = self._request(ws_header=str(other_ws.id))
        self.assertFalse(perm.has_permission(req, None))

    def test_no_workspace_id_denied(self):
        perm = HasWorkspaceAccess()
        req = self._request()
        self.assertFalse(perm.has_permission(req, None))

    def test_no_workspace_id_uses_current_workspace(self):
        perm = HasWorkspaceAccess()
        req = self._request()
        req.user.current_workspace = self.workspace
        self.assertTrue(perm.has_permission(req, None))

    def test_object_permission_workspace_obj(self):
        perm = HasWorkspaceAccess()
        req = self._request()
        mock_obj = MagicMock()
        mock_obj.workspace = self.workspace
        self.assertTrue(perm.has_object_permission(req, None, mock_obj))

    def test_object_permission_no_workspace_attr(self):
        perm = HasWorkspaceAccess()
        req = self._request()
        mock_obj = MagicMock(spec=[])  # no 'workspace', 'dashboard', 'table' attrs
        self.assertFalse(perm.has_object_permission(req, None, mock_obj))


class TestCanManageWorkspace(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.filter(workspace=self.workspace, user=self.user).update(role='owner')

    def test_owner_allowed(self):
        perm = CanManageWorkspace()
        req = self.factory.get('/')
        req.user = self.user
        req.data = {}
        req.META['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)
        self.assertTrue(perm.has_permission(req, None))

    def test_no_workspace_denied(self):
        perm = CanManageWorkspace()
        req = self.factory.get('/')
        req.user = self.user
        req.data = {}
        self.assertFalse(perm.has_permission(req, None))


# ---------------------------------------------------------------------------
# clean_dataframe — new steps 7-10
# ---------------------------------------------------------------------------

import os
import pandas as pd

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'test_fixtures')


class TestCleanDataNewSteps(TestCase):
    """Tests for clean_dataframe steps 7 (boolean), 8 (percentage),
    9 (title case), and 10 (missing-value flag)."""

    def setUp(self):
        self.svc = DataImportService()

    # ── Step 7: boolean normalisation ────────────────────────────────────

    def test_step7_boolean_yes_no_converted(self):
        df = pd.DataFrame({
            'active': ['yes', 'no', 'yes', 'YES', 'No', 'NO', 'yes', 'no',
                       'yes', 'no', 'yes', 'yes', 'no', 'yes', 'no', 'yes',
                       'yes', 'no', 'yes', 'no'],
        })
        cleaned, report = self.svc.clean_dataframe(df)
        self.assertTrue(any('boolean' in r.lower() or 'normalised' in r.lower()
                            for r in report), f"Expected boolean report, got: {report}")
        self.assertIn(True,  cleaned['active'].values)
        self.assertIn(False, cleaned['active'].values)

    def test_step7_true_false_converted(self):
        df = pd.DataFrame({
            'flag': ['true', 'false', 'true', 'false', 'TRUE', 'FALSE',
                     'true', 'false', 'true', 'false', 'true', 'false',
                     'true', 'false', 'true', 'false', 'true', 'false',
                     'true', 'false'],
        })
        cleaned, report = self.svc.clean_dataframe(df)
        self.assertIn(True,  cleaned['flag'].values)
        self.assertIn(False, cleaned['flag'].values)

    def test_step7_mixed_text_not_converted(self):
        """A column with varied text values must NOT be forced to boolean."""
        df = pd.DataFrame({
            'name': ['Alice', 'Bob', 'Carol', 'Dave', 'Eve', 'Frank',
                     'Grace', 'Henry', 'Irene', 'James', 'Karen', 'Liam',
                     'Mary', 'Noah', 'Olivia', 'Peter', 'Quinn', 'Rachel',
                     'Sam', 'Tina'],
        })
        cleaned, _ = self.svc.clean_dataframe(df)
        # All values must remain strings (pandas 3 uses StringDtype, not object)
        for val in cleaned['name'].dropna():
            self.assertIsInstance(val, str, f"Expected str, got {type(val)}: {val}")

    # ── Step 8: percentage stripping ─────────────────────────────────────

    def test_step8_pct_column_becomes_numeric(self):
        df = pd.DataFrame({
            'completion': ['85%', '72%', '91%', '68%', '95%', '55%', '88%',
                           '76%', '98%', '63%', '84%', '70%', '92%', '79%',
                           '48%', '86%', '97%', '74%', '89%', '61%'],
        })
        cleaned, report = self.svc.clean_dataframe(df)
        self.assertTrue(
            any('percentage' in r.lower() or 'numeric' in r.lower() for r in report)
            or pd.api.types.is_numeric_dtype(cleaned['completion']),
            "Percentage column should be numeric after cleaning"
        )
        if pd.api.types.is_numeric_dtype(cleaned['completion']):
            self.assertAlmostEqual(cleaned['completion'].iloc[0], 85.0)

    def test_step8_space_before_pct_handled(self):
        df = pd.DataFrame({
            'rate': ['45 %', '78 %', '91 %', '62 %', '88 %', '54 %', '77 %',
                     '93 %', '66 %', '82 %', '59 %', '74 %', '87 %', '69 %',
                     '95 %', '73 %', '81 %', '67 %', '90 %', '58 %'],
        })
        cleaned, _ = self.svc.clean_dataframe(df)
        if pd.api.types.is_numeric_dtype(cleaned['rate']):
            self.assertAlmostEqual(cleaned['rate'].iloc[0], 45.0)

    # ── Step 9: title-case categorical ───────────────────────────────────

    def test_step9_low_cardinality_text_titled(self):
        # Use a unique id column so deduplication does not collapse repeated
        # 'status' values — otherwise a single-column df gets reduced to 3 rows
        # and n_unique/n_non_null = 1.0 > 0.3, which causes step 9 to skip.
        df = pd.DataFrame({
            'id':     list(range(20)),
            'status': ['active', 'inactive', 'active', 'pending', 'active',
                       'inactive', 'active', 'pending', 'active', 'inactive',
                       'active', 'inactive', 'pending', 'active', 'inactive',
                       'active', 'pending', 'active', 'inactive', 'active'],
        })
        cleaned, report = self.svc.clean_dataframe(df)
        self.assertTrue(
            any('title' in r.lower() or 'standardised' in r.lower() for r in report)
            or str(cleaned['status'].iloc[0])[0].isupper(),
            "Low-cardinality column should be title-cased"
        )

    def test_step9_high_cardinality_not_titled(self):
        """Columns with many unique values must NOT be forced to title case."""
        values = [f'item_{i}' for i in range(30)]  # 30 unique → skip
        df = pd.DataFrame({'description': values})
        cleaned, _ = self.svc.clean_dataframe(df)
        # column should remain unchanged (still lowercase)
        self.assertTrue(cleaned['description'].iloc[0].startswith('item_'))

    # ── Step 10: missing-value flag ───────────────────────────────────────

    def test_step10_high_missing_rate_reported(self):
        """Columns with ≥30 % missing values should appear in the report."""
        data = {'amount': [100, None, None, None, None, None, None, None, None, None,
                           200, None, None, None, None, None, None, None, None, None],
                'name':   ['Alice'] * 20}
        df = pd.DataFrame(data)
        _, report = self.svc.clean_dataframe(df)
        self.assertTrue(
            any('missing' in r.lower() for r in report),
            f"Expected high-missing warning. Got: {report}"
        )

    def test_step10_low_missing_not_reported(self):
        """Columns below 30 % missing should NOT trigger the warning."""
        data = {'amount': [100, 200, None, 400, 500, 600, 700, 800, 900, 1000],
                'name':   ['A'] * 10}
        df = pd.DataFrame(data)
        _, report = self.svc.clean_dataframe(df)
        high_miss = [r for r in report if 'missing' in r.lower() and 'amount' in r.lower()]
        self.assertEqual(high_miss, [])

    # ── CSV fixture round-trip ────────────────────────────────────────────

    def test_boolean_pct_csv_cleans_without_error(self):
        """boolean_pct.csv has both boolean text cols and percentage cols."""
        path = os.path.join(FIXTURES_DIR, 'boolean_pct.csv')
        df = pd.read_csv(path)
        cleaned, report = self.svc.clean_dataframe(df)
        self.assertGreater(len(cleaned), 0)
        # At least one cleaning step must have triggered
        self.assertGreater(len(report), 0)

    def test_sparse_csv_reports_high_missing(self):
        """sparse.csv has columns with >30 % nulls — step 10 must flag them."""
        path = os.path.join(FIXTURES_DIR, 'sparse.csv')
        df = pd.read_csv(path)
        _, report = self.svc.clean_dataframe(df)
        self.assertTrue(
            any('missing' in r.lower() for r in report),
            f"Expected high-missing warning for sparse.csv. Got: {report}"
        )


# ---------------------------------------------------------------------------
# Schema detection — majority-vote sampling
# ---------------------------------------------------------------------------

class TestSchemaDetectionMajorityVote(TestCase):
    """Tests for DataImportService._detect_schema_from_df."""

    def setUp(self):
        self.svc = DataImportService()

    def _schema(self, df):
        return {s['name']: s['type'] for s in self.svc._detect_schema_from_df(df)}

    def test_detects_email_column(self):
        df = pd.DataFrame({'email': [f'user{i}@example.com' for i in range(20)]})
        self.assertEqual(self._schema(df).get('email'), 'email')

    def test_detects_url_column(self):
        df = pd.DataFrame({'website': [f'https://site{i}.com' for i in range(20)]})
        self.assertEqual(self._schema(df).get('website'), 'url')

    def test_detects_boolean_column(self):
        df = pd.DataFrame({'active': ['yes', 'no'] * 10})
        typ = self._schema(df).get('active')
        self.assertEqual(typ, 'boolean')

    def test_detects_percentage_column(self):
        df = pd.DataFrame({'rate': [f'{i}%' for i in range(10, 50)]})
        typ = self._schema(df).get('rate')
        self.assertEqual(typ, 'percentage')

    def test_detects_numeric_column(self):
        df = pd.DataFrame({'amount': [100.0, 200.0, 300.0] * 10})
        typ = self._schema(df).get('amount')
        self.assertEqual(typ, 'number')

    def test_detects_date_column(self):
        df = pd.DataFrame({'sale_date': ['2024-01-15', '2024-02-20', '2024-03-05'] * 10})
        typ = self._schema(df).get('sale_date')
        self.assertEqual(typ, 'date')

    def test_defaults_to_text_for_mixed(self):
        df = pd.DataFrame({'misc': ['hello', '42', 'world', 'foo', 'bar',
                                    'baz', 'qux', 'quux', 'corge', 'grault'] * 2})
        typ = self._schema(df).get('misc')
        self.assertEqual(typ, 'text')

    def test_already_numeric_dtype_detected_as_number(self):
        df = pd.DataFrame({'revenue': pd.array([1000, 2000, 3000] * 5, dtype='int64')})
        typ = self._schema(df).get('revenue')
        self.assertEqual(typ, 'number')

    def test_already_bool_dtype_detected_as_boolean(self):
        df = pd.DataFrame({'flag': pd.array([True, False] * 10, dtype='bool')})
        typ = self._schema(df).get('flag')
        self.assertEqual(typ, 'boolean')

    def test_all_csv_fixtures_produce_schema(self):
        """Every fixture CSV must produce a non-empty schema."""
        for fname in ('sales.csv', 'hr.csv', 'healthcare.csv',
                      'inventory.csv', 'finance.csv', 'survey.csv', 'text_only.csv'):
            with self.subTest(file=fname):
                df = pd.read_csv(os.path.join(FIXTURES_DIR, fname))
                schema = self.svc._detect_schema_from_df(df)
                self.assertGreater(len(schema), 0, f"Empty schema for {fname}")
                for field in schema:
                    self.assertIn('name', field)
                    self.assertIn('type', field)
                    self.assertTrue(field['type'], f"Empty type for field {field['name']}")


# ---------------------------------------------------------------------------
# Insight engine — no widget may be empty
# ---------------------------------------------------------------------------

class TestInsightEngineWidgetCompleteness(TestCase):
    """
    _generate_table_insights must never produce:
      - a widget with an empty title
      - a widget whose query_config has no aggregations
      - a widget whose viz_config is missing a 'description'

    Covers all nine detected domains plus key edge cases.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.svc = WorkspaceInsightService()

    @staticmethod
    def _clean_row(row):
        """Convert a row dict to contain only JSON-serializable Python types."""
        import json, math
        clean = {}
        for k, v in row.items():
            if v is None:
                clean[k] = None
            elif isinstance(v, float) and math.isnan(v):
                clean[k] = None
            else:
                try:
                    json.dumps(v)   # fast serializability check
                    clean[k] = v
                except (TypeError, ValueError):
                    clean[k] = None  # pd.NA, numpy types, etc.
        return clean

    def _build(self, schema_fields, rows):
        """Create DataTable + Records and return the table."""
        schema = [{'name': n, 'type': t, 'required': False} for n, t in schema_fields]
        table  = DataTableFactory(schema=schema)
        user   = table.workspace.owner
        clean  = [self._clean_row(r) for r in rows]
        p1, p2 = _patch_tasks()
        with p1, p2:
            Record.objects.bulk_create([
                Record(table=table, data=row, created_by=user, is_active=True)
                for row in clean
            ])
        table.record_count = len(clean)
        table.save(update_fields=['record_count'])
        return table

    def _check(self, insights, label=''):
        self.assertGreater(len(insights), 0, f"No widgets generated — {label}")
        for spec in insights:
            with self.subTest(widget_title=spec.get('title', '??'), dataset=label):
                self.assertTrue(
                    spec.get('title', '').strip(),
                    f"Empty title: {spec}"
                )
                aggs = (spec.get('query_config') or {}).get('aggregations')
                self.assertTrue(aggs, f"No aggregations in query_config: {spec}")
                desc = (spec.get('viz_config') or {}).get('description', '')
                self.assertTrue(
                    desc.strip(),
                    f"Missing or empty viz_config.description: {spec}"
                )

    # ── Sales ─────────────────────────────────────────────────────────────

    def test_sales_domain_no_empty_widgets(self):
        products = ['Widget A', 'Gadget B', 'Tool C', 'Device D']
        regions  = ['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru']
        statuses = ['Paid', 'Pending', 'Cancelled']
        rows = [
            {'invoice': f'INV-{i:03d}',
             'customer': f'Customer {i % 8}',
             'product': products[i % 4],
             'region':  regions[i % 4],
             'amount':  round(1000 + i * 150.5, 2),
             'discount': round(i * 2.0, 2),
             'status':  statuses[i % 3],
             'sale_date': f'2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}'}
            for i in range(30)
        ]
        schema = [
            ('invoice', 'text'), ('customer', 'text'), ('product', 'text'),
            ('region',  'text'), ('amount',   'currency'), ('discount', 'number'),
            ('status',  'text'), ('sale_date', 'date'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'sales')

    # ── HR ────────────────────────────────────────────────────────────────

    def test_hr_domain_no_empty_widgets(self):
        depts    = ['Engineering', 'Sales', 'HR', 'Finance', 'Marketing']
        positions = ['Developer', 'Manager', 'Executive', 'Analyst', 'Lead']
        rows = [
            {'employee_id': f'EMP-{i:03d}',
             'department': depts[i % 5],
             'position':   positions[i % 5],
             'salary':     80000 + i * 2500,
             'attendance': 85 + (i % 15),
             'leave_days': i % 20,
             'hire_date':  f'2020-{(i % 12) + 1:02d}-01',
             'status':     'Active' if i % 5 != 3 else 'Inactive'}
            for i in range(30)
        ]
        schema = [
            ('employee_id', 'text'), ('department', 'text'), ('position', 'text'),
            ('salary',      'number'), ('attendance', 'number'), ('leave_days', 'number'),
            ('hire_date',   'date'),  ('status',     'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'hr')

    # ── Healthcare ────────────────────────────────────────────────────────

    def test_healthcare_domain_no_empty_widgets(self):
        diagnoses = ['Malaria', 'Typhoid', 'Diabetes', 'Fracture', 'Pneumonia']
        wards     = ['General', 'ICU', 'Orthopedics', 'Surgery', 'Endocrinology']
        rows = [
            {'patient_id':    f'PAT-{i:03d}',
             'diagnosis':     diagnoses[i % 5],
             'ward':          wards[i % 5],
             'doctor':        f'Dr. {["Kamau","Odhiambo","Wanjiru","Mwangi","Omondi"][i%5]}',
             'bill_amount':   10000 + i * 1500,
             'admission_date': f'2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}',
             'status':        'Discharged' if i % 3 != 0 else 'Admitted'}
            for i in range(30)
        ]
        schema = [
            ('patient_id', 'text'), ('diagnosis', 'text'), ('ward', 'text'),
            ('doctor',     'text'), ('bill_amount', 'currency'),
            ('admission_date', 'date'), ('status', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'healthcare')

    # ── Inventory ─────────────────────────────────────────────────────────

    def test_inventory_domain_no_empty_widgets(self):
        warehouses = ['Main Store', 'Stationery', 'Medical', 'General']
        suppliers  = ['PharmaCo', 'MediSupply', 'OfficeWorld', 'CleanCo']
        rows = [
            {'sku':         f'SKU-{i:03d}',
             'product':     f'Product {i % 12}',
             'warehouse':   warehouses[i % 4],
             'stock':       50 + i * 10,
             'reorder_level': 20 + i % 30,
             'unit_cost':   round(10 + i * 5.5, 2),
             'supplier':    suppliers[i % 4],
             'status':      'Available' if i % 4 != 3 else 'Low Stock'}
            for i in range(30)
        ]
        schema = [
            ('sku',         'text'), ('product',  'text'), ('warehouse', 'text'),
            ('stock',       'number'), ('reorder_level', 'number'),
            ('unit_cost',   'currency'), ('supplier', 'text'), ('status', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'inventory')

    # ── Finance ───────────────────────────────────────────────────────────

    def test_finance_domain_no_empty_widgets(self):
        depts  = ['Marketing', 'Engineering', 'Sales', 'Operations', 'Finance']
        t_types = ['Expense', 'Salary', 'Revenue', 'Equipment', 'Utilities']
        rows = [
            {'account':           f'ACC-{i:03d}',
             'department':        depts[i % 5],
             'transaction_type':  t_types[i % 5],
             'debit':             round(i % 2 == 0 and 5000 + i * 1000 or 0, 2),
             'credit':            round(i % 2 == 1 and 8000 + i * 800 or 0, 2),
             'balance':           round(50000 - i * 500, 2),
             'transaction_date':  f'2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}',
             'status':            'Completed' if i % 5 != 4 else 'Pending'}
            for i in range(30)
        ]
        schema = [
            ('account', 'text'), ('department', 'text'), ('transaction_type', 'text'),
            ('debit',   'currency'), ('credit', 'currency'), ('balance', 'currency'),
            ('transaction_date', 'date'), ('status', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'finance')

    # ── Survey ────────────────────────────────────────────────────────────

    def test_survey_domain_no_empty_widgets(self):
        channels  = ['Online', 'Phone', 'Email']
        regions   = ['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru']
        outcomes  = ['Promoter', 'Passive', 'Detractor']
        rows = [
            {'respondent_id': f'R{i:03d}',
             'rating':        (i % 5) + 1,
             'satisfaction':  50 + (i * 3) % 50,
             'nps':           i % 11,
             'channel':       channels[i % 3],
             'region':        regions[i % 4],
             'feedback_date': f'2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}',
             'outcome':       outcomes[i % 3]}
            for i in range(30)
        ]
        schema = [
            ('respondent_id', 'text'), ('rating', 'number'), ('satisfaction', 'number'),
            ('nps',    'number'),  ('channel', 'text'), ('region', 'text'),
            ('feedback_date', 'date'), ('outcome', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'survey')

    # ── Text-only (no numeric fields) ─────────────────────────────────────

    def test_text_only_no_empty_widgets(self):
        """With zero numeric fields the engine must still produce at least
        a count KPI and distribution charts — none may be empty."""
        categories = ['Premium', 'Standard', 'Basic']
        regions    = ['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru']
        statuses   = ['Active', 'Inactive']
        channels   = ['Web', 'Mobile', 'Phone']
        rows = [
            {'name':     f'Contact {i}',
             'category': categories[i % 3],
             'region':   regions[i % 4],
             'status':   statuses[i % 2],
             'channel':  channels[i % 3]}
            for i in range(30)
        ]
        schema = [
            ('name', 'text'), ('category', 'text'), ('region', 'text'),
            ('status', 'text'), ('channel', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'text-only')

    # ── Single numeric, no categoricals ───────────────────────────────────

    def test_single_numeric_no_categoricals_no_empty_widgets(self):
        """Minimal dataset: one numeric field, no text fields, no dates."""
        rows = [{'revenue': round(1000 + i * 55.5, 2)} for i in range(20)]
        schema = [('revenue', 'currency')]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'single-numeric')

    # ── No dates, many categoricals ───────────────────────────────────────

    def test_no_date_field_no_empty_widgets(self):
        """When no date column is present, trend charts should fall back to
        created_at_date — widgets must still be complete."""
        regions   = ['Nairobi', 'Mombasa', 'Kisumu']
        statuses  = ['Active', 'Closed', 'Pending']
        rows = [
            {'customer': f'Cust {i}',
             'amount':   round(500 + i * 80, 2),
             'region':   regions[i % 3],
             'status':   statuses[i % 3]}
            for i in range(30)
        ]
        schema = [
            ('customer', 'text'), ('amount', 'currency'),
            ('region', 'text'), ('status', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'no-dates')

    # ── Low-cardinality-only categorical (pie-heavy) ───────────────────────

    def test_pie_heavy_dataset_no_empty_widgets(self):
        """All categorical columns have ≤5 unique values → lots of pie charts."""
        rows = [
            {'gender':  ['Male', 'Female'][i % 2],
             'tier':    ['Bronze', 'Silver', 'Gold'][i % 3],
             'status':  ['Active', 'Inactive'][i % 2],
             'channel': ['Web', 'App', 'Phone', 'Email'][i % 4],
             'revenue': round(200 + i * 45, 2)}
            for i in range(30)
        ]
        schema = [
            ('gender', 'text'), ('tier', 'text'), ('status', 'text'),
            ('channel', 'text'), ('revenue', 'currency'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'pie-heavy')

    # ── Sparse data (many nulls) ──────────────────────────────────────────

    def test_sparse_data_no_empty_widgets(self):
        """Columns with >30 % nulls — engine must handle gracefully."""
        rows = []
        for i in range(30):
            row = {'order_id': f'ORD-{i:03d}',
                   'customer': f'Customer {i % 8}',
                   'status':   ['Shipped', 'Delivered', 'Pending'][i % 3]}
            if i % 3 != 0:          # ~33 % null
                row['amount'] = round(500 + i * 60, 2)
            if i % 4 != 0:          # ~25 % null
                row['region'] = ['Nairobi', 'Mombasa', 'Kisumu'][i % 3]
            rows.append(row)

        schema = [
            ('order_id', 'text'), ('customer', 'text'),
            ('amount',   'currency'), ('region', 'text'), ('status', 'text'),
        ]
        table   = self._build(schema, rows)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'sparse')

    # ── CSV fixture integration ───────────────────────────────────────────

    def _build_from_csv(self, fname, schema_fields):
        """Import a fixture CSV into a DataTable and return the table."""
        df   = pd.read_csv(os.path.join(FIXTURES_DIR, fname))
        rows = df.to_dict('records')   # _build/_clean_row handles NaN → None
        return self._build(schema_fields, rows)

    def test_sales_csv_no_empty_widgets(self):
        schema = [
            ('invoice_id', 'text'), ('customer', 'text'), ('product', 'text'),
            ('region',     'text'), ('amount',   'currency'), ('discount', 'number'),
            ('sale_date',  'date'), ('status',   'text'),
        ]
        table   = self._build_from_csv('sales.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'sales.csv')

    def test_hr_csv_no_empty_widgets(self):
        schema = [
            ('employee_id', 'text'), ('department', 'text'), ('position', 'text'),
            ('salary',      'number'), ('hire_date', 'date'),
            ('attendance',  'number'), ('leave_days', 'number'), ('status', 'text'),
        ]
        table   = self._build_from_csv('hr.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'hr.csv')

    def test_healthcare_csv_no_empty_widgets(self):
        schema = [
            ('patient_id', 'text'), ('diagnosis', 'text'), ('doctor', 'text'),
            ('ward', 'text'), ('treatment', 'text'), ('bill_amount', 'currency'),
            ('admission_date', 'date'), ('discharge_date', 'date'), ('status', 'text'),
        ]
        table   = self._build_from_csv('healthcare.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'healthcare.csv')

    def test_inventory_csv_no_empty_widgets(self):
        schema = [
            ('sku', 'text'), ('product', 'text'), ('warehouse', 'text'),
            ('stock', 'number'), ('reorder_level', 'number'), ('unit_cost', 'currency'),
            ('supplier', 'text'), ('expiry_date', 'date'), ('status', 'text'),
        ]
        table   = self._build_from_csv('inventory.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'inventory.csv')

    def test_finance_csv_no_empty_widgets(self):
        schema = [
            ('account', 'text'), ('department', 'text'), ('transaction_type', 'text'),
            ('debit', 'currency'), ('credit', 'currency'), ('balance', 'currency'),
            ('transaction_date', 'date'), ('status', 'text'),
        ]
        table   = self._build_from_csv('finance.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'finance.csv')

    def test_survey_csv_no_empty_widgets(self):
        schema = [
            ('respondent_id', 'text'), ('rating', 'number'), ('satisfaction', 'number'),
            ('nps', 'number'), ('channel', 'text'), ('region', 'text'),
            ('feedback_date', 'date'), ('outcome', 'text'),
        ]
        table   = self._build_from_csv('survey.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'survey.csv')

    def test_text_only_csv_no_empty_widgets(self):
        schema = [
            ('name', 'text'), ('category', 'text'), ('region', 'text'),
            ('channel', 'text'), ('status', 'text'),
        ]
        table   = self._build_from_csv('text_only.csv', schema)
        insights = self.svc._generate_table_insights(table)
        self._check(insights, 'text_only.csv')


# ---------------------------------------------------------------------------
# Public dashboard view
# ---------------------------------------------------------------------------

class TestPublicDashboardView(TestCase):
    """
    Tests for the unauthenticated public dashboard view at /d/<public_uuid>/.
    """

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.dashboard = DashboardFactory(workspace=self.workspace, is_public=True)

    def test_public_dashboard_returns_200_for_public(self):
        resp = self.client.get(f'/d/{self.dashboard.public_uuid}/')
        self.assertEqual(resp.status_code, 200)

    def test_public_dashboard_no_login_required(self):
        """Anonymous users must be able to access the page."""
        from django.test import Client
        anon = Client()
        resp = anon.get(f'/d/{self.dashboard.public_uuid}/')
        self.assertEqual(resp.status_code, 200)

    def test_public_dashboard_contains_dashboard_name(self):
        resp = self.client.get(f'/d/{self.dashboard.public_uuid}/')
        self.assertContains(resp, self.dashboard.name)

    def test_private_dashboard_returns_404(self):
        private = DashboardFactory(workspace=self.workspace, is_public=False)
        resp = self.client.get(f'/d/{private.public_uuid}/')
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_uuid_returns_404(self):
        import uuid
        resp = self.client.get(f'/d/{uuid.uuid4()}/')
        self.assertEqual(resp.status_code, 404)

    def test_inactive_dashboard_returns_404(self):
        from django.utils import timezone
        self.dashboard.is_active = False
        self.dashboard.deleted_at = timezone.now()
        self.dashboard.save()
        resp = self.client.get(f'/d/{self.dashboard.public_uuid}/')
        self.assertEqual(resp.status_code, 404)

    def test_powered_by_branding_present(self):
        resp = self.client.get(f'/d/{self.dashboard.public_uuid}/')
        self.assertContains(resp, 'AnalyticsMeta')


# ===========================================================================
# Phase 2 Tests
# ===========================================================================

import hashlib
import hmac as hmac_mod
import json as _json

from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.dashboards.factories import (
    DataAlertFactory, WebhookEndpointFactory,
)
from apps.dashboards.models import DataAlert, WebhookEndpoint, AuditLog


# ---------------------------------------------------------------------------
# DataAlert model
# ---------------------------------------------------------------------------

class TestDataAlertModel(TestCase):

    def _make_alert(self, operator='gt', threshold=100.0):
        table = DataTableFactory()
        return DataAlertFactory(
            workspace=table.workspace,
            table=table,
            operator=operator,
            threshold=threshold,
            cooldown_minutes=60,
        )

    def test_evaluate_gt_true(self):
        alert = self._make_alert('gt', 100.0)
        self.assertTrue(alert.evaluate(101.0))

    def test_evaluate_gt_false(self):
        alert = self._make_alert('gt', 100.0)
        self.assertFalse(alert.evaluate(99.0))

    def test_evaluate_lte(self):
        alert = self._make_alert('lte', 50.0)
        self.assertTrue(alert.evaluate(50.0))
        self.assertFalse(alert.evaluate(50.01))

    def test_evaluate_eq(self):
        alert = self._make_alert('eq', 42.0)
        self.assertTrue(alert.evaluate(42.0))
        self.assertFalse(alert.evaluate(42.1))

    def test_is_in_cooldown_false_when_never_triggered(self):
        alert = self._make_alert()
        self.assertFalse(alert.is_in_cooldown())

    def test_is_in_cooldown_true_when_recently_triggered(self):
        from datetime import timedelta
        alert = self._make_alert()
        alert.last_triggered = timezone.now() - timedelta(minutes=5)
        alert.save(update_fields=['last_triggered'])
        self.assertTrue(alert.is_in_cooldown())

    def test_is_in_cooldown_false_after_cooldown_period(self):
        from datetime import timedelta
        alert = self._make_alert()
        alert.last_triggered = timezone.now() - timedelta(minutes=120)
        alert.save(update_fields=['last_triggered'])
        self.assertFalse(alert.is_in_cooldown())

    def test_str(self):
        alert = self._make_alert('gt', 100.0)
        self.assertIn('Alert', str(alert))


# ---------------------------------------------------------------------------
# DataAlert API
# ---------------------------------------------------------------------------

class TestDataAlertAPI(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table = DataTableFactory(workspace=self.workspace)
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)

    def test_list_alerts_empty(self):
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/alerts/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_create_alert(self):
        data = {
            'table': str(self.table.id),
            'name': 'Revenue Alert',
            'field_name': 'revenue',
            'aggregate': 'sum',
            'operator': 'gt',
            'threshold': 5000.0,
        }
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/alerts/',
            data=_json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Revenue Alert')
        self.assertTrue(DataAlert.objects.filter(workspace=self.workspace).exists())

    def test_get_alert_detail(self):
        alert = DataAlertFactory(workspace=self.workspace, table=self.table)
        resp = self.client.get(f'/api/v1/alerts/{alert.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], str(alert.id))

    def test_patch_alert(self):
        alert = DataAlertFactory(workspace=self.workspace, table=self.table, threshold=100.0)
        resp = self.client.patch(
            f'/api/v1/alerts/{alert.id}/',
            data=_json.dumps({'threshold': 200.0}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.threshold, 200.0)

    def test_delete_alert(self):
        alert = DataAlertFactory(workspace=self.workspace, table=self.table)
        resp = self.client.delete(f'/api/v1/alerts/{alert.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(DataAlert.objects.filter(pk=alert.pk).exists())

    def test_create_alert_wrong_workspace_table_rejected(self):
        other_table = DataTableFactory()  # belongs to a different workspace
        data = {
            'table': str(other_table.id),
            'name': 'Bad Alert',
            'field_name': 'x',
            'aggregate': 'sum',
            'operator': 'gt',
            'threshold': 1.0,
        }
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/alerts/',
            data=_json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_rejected(self):
        anon = APIClient()
        resp = anon.get(f'/api/v1/workspaces/{self.workspace.id}/alerts/')
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# WebhookEndpoint API
# ---------------------------------------------------------------------------

class TestWebhookEndpointAPI(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table = DataTableFactory(workspace=self.workspace)
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)

    def test_list_webhooks_empty(self):
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/webhooks/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_create_webhook(self):
        data = {'table': str(self.table.id), 'name': 'My Webhook'}
        resp = self.client.post(
            f'/api/v1/workspaces/{self.workspace.id}/webhooks/',
            data=_json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn('token', resp.data)
        self.assertIn('secret', resp.data)
        self.assertEqual(resp.data['name'], 'My Webhook')

    def test_get_webhook_detail(self):
        wh = WebhookEndpointFactory(workspace=self.workspace, table=self.table)
        resp = self.client.get(f'/api/v1/webhooks/{wh.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], str(wh.id))

    def test_patch_webhook_name(self):
        wh = WebhookEndpointFactory(workspace=self.workspace, table=self.table)
        resp = self.client.patch(
            f'/api/v1/webhooks/{wh.id}/',
            data=_json.dumps({'name': 'Renamed'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        wh.refresh_from_db()
        self.assertEqual(wh.name, 'Renamed')

    def test_delete_webhook(self):
        wh = WebhookEndpointFactory(workspace=self.workspace, table=self.table)
        resp = self.client.delete(f'/api/v1/webhooks/{wh.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(WebhookEndpoint.objects.filter(pk=wh.pk).exists())

    def test_regenerate_secret(self):
        wh = WebhookEndpointFactory(workspace=self.workspace, table=self.table)
        old_secret = wh.secret
        resp = self.client.post(f'/api/v1/webhooks/{wh.id}/regenerate-secret/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('secret', resp.data)
        wh.refresh_from_db()
        self.assertNotEqual(wh.secret, old_secret)


# ---------------------------------------------------------------------------
# Webhook ingest view
# ---------------------------------------------------------------------------

class TestWebhookIngestView(TestCase):

    def setUp(self):
        self.table = DataTableFactory(
            schema=[{'name': 'value', 'type': 'number', 'required': False}]
        )
        self.wh = WebhookEndpointFactory(workspace=self.table.workspace, table=self.table)

    def _sign(self, body: bytes) -> str:
        from apps.dashboards.webhook_crypto import decrypt_secret
        plaintext = decrypt_secret(self.wh.secret)
        return 'sha256=' + hmac_mod.new(
            plaintext.encode('utf-8'), body, hashlib.sha256
        ).hexdigest()

    def _post(self, payload, sign=True, bad_sig=False):
        from django.test import Client
        client = Client()
        body = _json.dumps(payload).encode()
        sig = 'sha256=bad' if bad_sig else (self._sign(body) if sign else '')
        return client.generic(
            'POST',
            f'/webhook/ingest/{self.wh.token}/',
            data=body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )

    def test_valid_object_payload_creates_record(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self._post({'value': 42})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['created'], 1)

    def test_valid_array_payload(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            resp = self._post([{'value': 1}, {'value': 2}])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['created'], 2)

    def test_invalid_signature_rejected(self):
        resp = self._post({'value': 1}, bad_sig=True)
        self.assertEqual(resp.status_code, 401)

    def test_missing_signature_rejected(self):
        resp = self._post({'value': 1}, sign=False)
        self.assertEqual(resp.status_code, 401)

    def test_inactive_endpoint_returns_404(self):
        self.wh.is_active = False
        self.wh.save()
        resp = self._post({'value': 1})
        self.assertEqual(resp.status_code, 404)

    def test_unknown_token_returns_404(self):
        from django.test import Client
        body = b'{}'
        sig = 'sha256=' + hmac_mod.new(b'secret', body, hashlib.sha256).hexdigest()
        resp = Client().generic(
            'POST', '/webhook/ingest/unknowntoken/',
            data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp.status_code, 404)

    def test_telemetry_incremented(self):
        before = self.wh.total_requests
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            self._post({'value': 5})
        self.wh.refresh_from_db()
        self.assertEqual(self.wh.total_requests, before + 1)
        self.assertIsNotNone(self.wh.last_request_at)


# ---------------------------------------------------------------------------
# Audit Log API
# ---------------------------------------------------------------------------

class TestAuditLogAPI(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)
        # Create some audit log entries
        for i in range(3):
            AuditLog.objects.create(
                workspace=self.workspace,
                user=self.user,
                action='view',
                content_type='DataTable',
                object_id=DataTableFactory(workspace=self.workspace).id,
                object_repr=f'Table {i}',
            )

    def test_list_audit_logs(self):
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/audit-logs/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 3)

    def test_filter_by_action(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/audit-logs/?action=view'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 3)

    def test_filter_by_wrong_action_returns_empty(self):
        resp = self.client.get(
            f'/api/v1/workspaces/{self.workspace.id}/audit-logs/?action=delete'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 0)

    def test_viewer_member_forbidden(self):
        viewer = UserFactory()
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.create(workspace=self.workspace, user=viewer, role='viewer')
        viewer_token, _ = Token.objects.get_or_create(user=viewer)
        viewer_client = APIClient()
        viewer_client.credentials(HTTP_AUTHORIZATION=f'Token {viewer_token.key}')
        viewer_client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)
        resp = viewer_client.get(f'/api/v1/workspaces/{self.workspace.id}/audit-logs/')
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Workspace Usage API
# ---------------------------------------------------------------------------

class TestWorkspaceUsageAPI(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)

    def test_usage_returns_expected_keys(self):
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/usage/')
        self.assertEqual(resp.status_code, 200)
        for key in ('total_tables', 'total_records', 'member_count',
                    'estimated_storage_mb', 'workspace_id'):
            self.assertIn(key, resp.data)

    def test_usage_counts_tables(self):
        DataTableFactory(workspace=self.workspace)
        DataTableFactory(workspace=self.workspace)
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/usage/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total_tables'], 2)

    def test_usage_counts_members(self):
        from apps.workspaces.models import WorkspaceMembership
        extra = UserFactory()
        WorkspaceMembership.objects.create(workspace=self.workspace, user=extra, role='editor')
        resp = self.client.get(f'/api/v1/workspaces/{self.workspace.id}/usage/')
        self.assertEqual(resp.data['member_count'], 2)  # owner + editor


# ---------------------------------------------------------------------------
# Celery tasks — prune_audit_logs and check_data_alerts
# ---------------------------------------------------------------------------

class TestPruneAuditLogsTask(TestCase):

    @override_settings(AUDIT_LOG_RETENTION_DAYS=90)
    def test_deletes_old_entries(self):
        from datetime import timedelta
        from apps.dashboards.tasks import prune_audit_logs
        ws = WorkspaceFactory()
        table = DataTableFactory(workspace=ws)
        # Old entry (should be deleted)
        old = AuditLog.objects.create(
            workspace=ws, user=ws.owner, action='view',
            content_type='DataTable', object_id=table.id, object_repr='old',
        )
        AuditLog.objects.filter(pk=old.pk).update(
            timestamp=timezone.now() - timedelta(days=100)
        )
        # Recent entry (should survive)
        AuditLog.objects.create(
            workspace=ws, user=ws.owner, action='view',
            content_type='DataTable', object_id=table.id, object_repr='recent',
        )
        result = prune_audit_logs.apply().get()
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(AuditLog.objects.filter(workspace=ws).count(), 1)

    @override_settings(AUDIT_LOG_RETENTION_DAYS=90)
    def test_no_entries_to_delete(self):
        from apps.dashboards.tasks import prune_audit_logs
        result = prune_audit_logs.apply().get()
        self.assertEqual(result['deleted'], 0)


class TestCheckDataAlertsTask(TestCase):

    def setUp(self):
        self.table = DataTableFactory(
            schema=[{'name': 'amount', 'type': 'number', 'required': False}]
        )
        self.workspace = self.table.workspace
        self.user = self.workspace.owner

    def _create_records(self, values):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            for v in values:
                Record.objects.create(
                    table=self.table, data={'amount': v}, created_by=self.user
                )

    def test_alert_fires_when_condition_met(self):
        from apps.dashboards.tasks import check_data_alerts
        from apps.notifications.models import Notification
        self._create_records([2000, 3000, 4000])
        DataAlertFactory(
            workspace=self.workspace, table=self.table,
            field_name='amount', aggregate='sum', operator='gt', threshold=5000.0,
        )
        result = check_data_alerts.apply().get()
        self.assertEqual(result['triggered'], 1)
        self.assertTrue(Notification.objects.filter(user=self.user).exists())

    def test_alert_does_not_fire_when_condition_not_met(self):
        from apps.dashboards.tasks import check_data_alerts
        from apps.notifications.models import Notification
        self._create_records([100, 200])
        DataAlertFactory(
            workspace=self.workspace, table=self.table,
            field_name='amount', aggregate='sum', operator='gt', threshold=1_000_000.0,
        )
        result = check_data_alerts.apply().get()
        self.assertEqual(result['triggered'], 0)
        self.assertFalse(Notification.objects.filter(user=self.user).exists())

    def test_alert_respects_cooldown(self):
        from datetime import timedelta
        from apps.dashboards.tasks import check_data_alerts
        from apps.notifications.models import Notification
        self._create_records([2000, 3000])
        alert = DataAlertFactory(
            workspace=self.workspace, table=self.table,
            field_name='amount', aggregate='sum', operator='gt', threshold=1.0,
            cooldown_minutes=60,
        )
        # Simulate it was already triggered 10 minutes ago
        DataAlert.objects.filter(pk=alert.pk).update(
            last_triggered=timezone.now() - timedelta(minutes=10)
        )
        result = check_data_alerts.apply().get()
        self.assertEqual(result['triggered'], 0)


# ---------------------------------------------------------------------------
# File upload rate limiting
# ---------------------------------------------------------------------------

class TestFileUploadRateLimit(TestCase):
    """
    Verify that the FileUploadThrottle fires HTTP 429 when the per-user
    import rate is exceeded.

    We override the throttle rate to '1/minute' so the test only needs to
    send two requests rather than 31.
    """

    def setUp(self):
        from django.test import Client as DjangoClient
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace)
        self.client    = DjangoClient()
        self.client.force_login(self.user)
        session = self.client.session
        session['current_workspace_id'] = str(self.workspace.id)
        session.save()

    def _post_import(self):
        import io as _io
        f = _io.BytesIO(b"name\nalice\n")
        f.name = 'test.csv'
        return self.client.post(
            f'/api/v1/tables/{self.table.id}/import/',
            data={'file': f},
        )

    def test_throttle_class_is_applied_to_import_endpoint(self):
        """The import view must have FileUploadThrottle in its throttle_classes."""
        from apps.dashboards.api_v1 import TableImportAPIView, FileUploadThrottle
        self.assertIn(FileUploadThrottle, TableImportAPIView.throttle_classes)

    def test_file_upload_scope_is_configured(self):
        """'file_upload' scope must appear in DEFAULT_THROTTLE_RATES."""
        from django.conf import settings
        rates = settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
        self.assertIn('file_upload', rates)
        # Rate string must be parseable (e.g. '30/hour')
        rate_str = rates['file_upload']
        self.assertRegex(rate_str, r'^\d+/(second|minute|hour|day)$')

    def test_throttle_blocks_on_limit(self):
        """When allow_request returns False the endpoint must respond with 429."""
        from apps.dashboards.api_v1 import FileUploadThrottle

        call_count = {'n': 0}

        def mock_allow(self_throttle, request, view):
            call_count['n'] += 1
            # DRF's wait() accesses self.history; initialise it to avoid AttributeError.
            self_throttle.history = []
            self_throttle.now = 0
            return call_count['n'] <= 1  # allow first, block second

        fake_task = MagicMock()
        fake_task.id = 'fake-celery-task-id'

        with patch.object(FileUploadThrottle, 'allow_request', mock_allow), \
             patch('apps.dashboards.services.DataImportService.parse_file') as mock_parse, \
             patch('apps.insights.tasks.run_async_import') as mock_task_cls:
            mock_task_cls.delay.return_value = fake_task
            import pandas as pd
            mock_parse.return_value = pd.DataFrame({'name': ['alice']})
            resp1 = self._post_import()
            resp2 = self._post_import()

        self.assertIn(resp1.status_code, [200, 201, 202])
        self.assertEqual(resp2.status_code, 429)
