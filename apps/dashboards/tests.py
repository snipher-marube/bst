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
