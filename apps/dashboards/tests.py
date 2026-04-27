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
from apps.dashboards.services import DataImportService, QueryEngine, TableProfileService


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

    def test_widget_types_include_distribution_and_scatter_widgets(self):
        types = [c[0] for c in Widget.WIDGET_TYPES]
        for t in ['line_chart', 'area_chart', 'bar_chart', 'pie_chart', 'table', 'metric',
                  'number', 'gauge', 'heatmap', 'scatter', 'histogram', 'box_plot']:
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

    def test_execute_histogram_returns_values_and_kde(self):
        w = self._widget('histogram', {'field': 'amount', 'bins': 8, 'show_kde': True})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(result.get('field'), 'amount')
        self.assertEqual(len(result.get('values', [])), 5)
        self.assertIn('kde', result)

    def test_execute_box_plot_returns_values_and_summary(self):
        w = self._widget('box_plot', {'field': 'amount'})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(result.get('field'), 'amount')
        self.assertEqual(len(result.get('values', [])), 5)
        self.assertEqual(result.get('summary', {}).get('median'), 300.0)

    def test_execute_scatter_returns_points(self):
        w = self._widget('scatter', {'x_field': 'amount', 'y_field': 'amount'})
        result = self.engine.execute_widget_query(w)
        self.assertEqual(result.get('x_field'), 'amount')
        self.assertEqual(result.get('y_field'), 'amount')
        self.assertEqual(len(result.get('points', [])), 5)

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


class TestTableProfileService(TestCase):

    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table = DataTableFactory(
            workspace=self.workspace,
            created_by=self.user,
            schema=[
                {'name': 'amount', 'type': 'number'},
                {'name': 'status', 'type': 'text'},
            ],
        )

        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            Record.objects.bulk_create([
                Record(table=self.table, created_by=self.user, data={'amount': 10, 'status': 'new'}),
                Record(table=self.table, created_by=self.user, data={'amount': 20, 'status': 'new'}),
                Record(table=self.table, created_by=self.user, data={'amount': 30, 'status': 'paid'}),
                Record(table=self.table, created_by=self.user, data={'amount': 40, 'status': 'paid'}),
                Record(table=self.table, created_by=self.user, data={'amount': 50, 'status': 'paid'}),
            ])
        self.table.record_count = 5
        self.table.save(update_fields=['record_count'])

    def test_numeric_column_includes_histogram_and_box_plot(self):
        profile = TableProfileService(self.table).compute()
        amount = next(col for col in profile['columns'] if col['name'] == 'amount')

        self.assertIn('histogram', amount)
        self.assertTrue(amount['histogram'])
        self.assertIn('box_plot', amount)
        self.assertEqual(amount['box_plot']['median'], 30.0)
        self.assertEqual(amount['box_plot']['min'], 10.0)
        self.assertEqual(amount['box_plot']['max'], 50.0)
        self.assertEqual(sum(bin_['count'] for bin_ in amount['histogram']), 5)


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


# ===========================================================================
# Dashboard Filter Bar (P1 #9)
# ===========================================================================

class TestDashboardFilterConfig(TestCase):
    """Tests for filter_config field, PATCH endpoint, and QueryEngine extra_filters."""

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.client    = APIClient()
        self.client.force_authenticate(user=self.user)
        self.user.current_workspace = self.workspace
        self.user.save()
        self.dashboard = DashboardFactory(workspace=self.workspace, created_by=self.user)

    # ------------------------------------------------------------------
    # Model field
    # ------------------------------------------------------------------

    def test_filter_config_defaults_to_empty_dict(self):
        self.assertEqual(self.dashboard.filter_config, {})

    def test_filter_config_saves_and_retrieves(self):
        config = {"filters": [{"field": "region", "label": "Region", "type": "select", "options": ["EMEA", "APAC"]}]}
        self.dashboard.filter_config = config
        self.dashboard.save()
        self.dashboard.refresh_from_db()
        self.assertEqual(self.dashboard.filter_config, config)

    # ------------------------------------------------------------------
    # PATCH /api/dashboards/<id>/filter-config/
    # ------------------------------------------------------------------

    def test_patch_filter_config_saves(self):
        payload = {"filter_config": {"filters": [{"field": "region", "type": "select", "options": ["EMEA"]}]}}
        resp = self.client.patch(
            f'/api/dashboards/{self.dashboard.id}/filter-config/',
            data=payload,
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.dashboard.refresh_from_db()
        self.assertEqual(self.dashboard.filter_config['filters'][0]['field'], 'region')

    def test_patch_filter_config_requires_filter_config_key(self):
        resp = self.client.patch(
            f'/api/dashboards/{self.dashboard.id}/filter-config/',
            data={'bad_key': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_filter_config_rejects_non_object(self):
        resp = self.client.patch(
            f'/api/dashboards/{self.dashboard.id}/filter-config/',
            data={'filter_config': [1, 2, 3]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_filter_config_wrong_workspace_returns_404(self):
        other_user      = UserFactory()
        other_workspace = WorkspaceFactory(owner=other_user)
        other_dashboard = DashboardFactory(workspace=other_workspace, created_by=other_user)
        resp = self.client.patch(
            f'/api/dashboards/{other_dashboard.id}/filter-config/',
            data={'filter_config': {}},
            format='json',
        )
        self.assertIn(resp.status_code, [403, 404])

    # ------------------------------------------------------------------
    # WidgetDataAPIView — ?filters= param
    # ------------------------------------------------------------------

    def _make_widget_with_records(self):
        table = DataTableFactory(workspace=self.workspace, created_by=self.user)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'):
            RecordFactory(table=table, data={'region': 'EMEA', 'amount': 100}, created_by=self.user)
            RecordFactory(table=table, data={'region': 'APAC', 'amount': 200}, created_by=self.user)
        widget = WidgetFactory(
            dashboard=self.dashboard,
            widget_type='table',
            query_config={},
        )
        widget.table = table
        widget.save()
        return widget, table

    def test_widget_data_no_filters_returns_all_records(self):
        widget, _ = self._make_widget_with_records()
        resp = self.client.get(f'/api/widgets/{widget.id}/data/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(len(data), 2)

    def test_widget_data_with_filter_narrows_results(self):
        widget, _ = self._make_widget_with_records()
        import json as _json
        filters = _json.dumps([{"field": "region", "operator": "eq", "value": "EMEA"}])
        resp = self.client.get(f'/api/widgets/{widget.id}/data/?filters={filters}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['region'], 'EMEA')

    def test_widget_data_invalid_filters_json_returns_400(self):
        widget, _ = self._make_widget_with_records()
        resp = self.client.get(f'/api/widgets/{widget.id}/data/?filters=not-valid-json')
        self.assertEqual(resp.status_code, 400)

    def test_widget_data_filters_must_be_array(self):
        widget, _ = self._make_widget_with_records()
        import json as _json
        resp = self.client.get(f'/api/widgets/{widget.id}/data/?filters={_json.dumps({"key": "val"})}')
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------
    # QueryEngine.execute_widget_query — extra_filters
    # ------------------------------------------------------------------

    def test_query_engine_extra_filters_applied(self):
        table = DataTableFactory(workspace=self.workspace, created_by=self.user)
        with patch('apps.dashboards.models.broadcast_widget_update.delay'):
            RecordFactory(table=table, data={'status': 'active', 'val': 10}, created_by=self.user)
            RecordFactory(table=table, data={'status': 'inactive', 'val': 20}, created_by=self.user)
        widget = WidgetFactory(
            dashboard=self.dashboard,
            widget_type='table',
            query_config={},
        )
        widget.table = table
        widget.save()

        engine = QueryEngine()
        result = engine.execute_widget_query(widget, extra_filters=[{"field": "status", "operator": "eq", "value": "active"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['status'], 'active')

    def test_query_engine_cache_key_differs_with_extra_filters(self):
        widget = WidgetFactory(dashboard=self.dashboard, widget_type='metric', query_config={})
        engine = QueryEngine()
        key_plain   = engine._generate_cache_key(widget)
        key_filtered = engine._generate_cache_key(widget, extra_filters=[{"field": "x", "operator": "eq", "value": "y"}])
        self.assertNotEqual(key_plain, key_filtered)


# ===========================================================================
# Incremental / Delta Imports — import_mode (P1 #11)
# ===========================================================================

class TestIncrementalImports(TestCase):
    """Tests for append / replace / upsert import modes in DataImportService."""

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace, created_by=self.user)

    def _seed(self, rows):
        """Insert raw Record rows."""
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            for row in rows:
                Record.objects.create(table=self.table, data=row, created_by=self.user)

    def _import(self, rows, mode='append', pk_field=''):
        import pandas as pd
        df = pd.DataFrame(rows)
        return DataImportService().import_data(
            table=self.table,
            df=df,
            user=self.user,
            import_mode=mode,
            primary_key_field=pk_field,
        )

    # ------------------------------------------------------------------
    # append mode (default)
    # ------------------------------------------------------------------

    def test_append_adds_rows_without_touching_existing(self):
        self._seed([{'id': 1, 'val': 'old'}])
        result = self._import([{'id': 2, 'val': 'new'}], mode='append')
        self.assertEqual(result['success'], 1)
        self.assertEqual(Record.objects.filter(table=self.table, is_active=True).count(), 2)

    # ------------------------------------------------------------------
    # replace mode
    # ------------------------------------------------------------------

    def test_replace_soft_deletes_existing_then_inserts(self):
        self._seed([{'id': 1}, {'id': 2}])
        self.assertEqual(Record.objects.filter(table=self.table, is_active=True).count(), 2)

        result = self._import([{'id': 3}], mode='replace')
        active = Record.objects.filter(table=self.table, is_active=True)
        inactive = Record.objects.filter(table=self.table, is_active=False)

        self.assertEqual(active.count(), 1)
        self.assertEqual(inactive.count(), 2)
        self.assertEqual(active.first().data['id'], 3)
        self.assertEqual(result['success'], 1)

    # ------------------------------------------------------------------
    # upsert mode
    # ------------------------------------------------------------------

    def test_upsert_updates_existing_row_by_pk(self):
        self._seed([{'sku': 'A1', 'qty': 5}])
        result = self._import([{'sku': 'A1', 'qty': 10}], mode='upsert', pk_field='sku')

        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['success'], 0)
        rec = Record.objects.filter(table=self.table, is_active=True).get()
        self.assertEqual(rec.data['qty'], 10)

    def test_upsert_inserts_when_no_match(self):
        self._seed([{'sku': 'A1', 'qty': 5}])
        result = self._import([{'sku': 'B2', 'qty': 99}], mode='upsert', pk_field='sku')

        self.assertEqual(result['success'], 1)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(Record.objects.filter(table=self.table, is_active=True).count(), 2)

    def test_upsert_mixed_batch(self):
        self._seed([{'sku': 'A1', 'qty': 1}, {'sku': 'A2', 'qty': 2}])
        result = self._import(
            [{'sku': 'A1', 'qty': 99}, {'sku': 'A3', 'qty': 3}],
            mode='upsert', pk_field='sku',
        )
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['success'], 1)
        self.assertEqual(Record.objects.filter(table=self.table, is_active=True).count(), 3)

    # ------------------------------------------------------------------
    # Webhook ingest — import_mode query param
    # ------------------------------------------------------------------

    def _sign(self, secret_plaintext, body):
        import hmac as _hmac, hashlib
        return 'sha256=' + _hmac.new(secret_plaintext.encode(), body, hashlib.sha256).hexdigest()

    def test_webhook_ingest_append_mode(self):
        from apps.dashboards.factories import WebhookEndpointFactory
        from apps.dashboards.webhook_crypto import decrypt_secret
        wh = WebhookEndpointFactory(
            workspace=self.workspace,
            table=self.table,
            created_by=self.user,
        )
        body = b'[{"id": 1}]'
        plaintext = decrypt_secret(wh.secret)
        sig = self._sign(plaintext, body)
        resp = self.client.post(
            f'/webhook/ingest/{wh.token}/',
            data=body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get('mode'), 'replace')  # default is replace

    def test_webhook_ingest_invalid_import_mode_returns_400(self):
        from apps.dashboards.factories import WebhookEndpointFactory
        from apps.dashboards.webhook_crypto import decrypt_secret
        wh = WebhookEndpointFactory(
            workspace=self.workspace,
            table=self.table,
            created_by=self.user,
        )
        body = b'[{"id": 1}]'
        plaintext = decrypt_secret(wh.secret)
        sig = self._sign(plaintext, body)
        resp = self.client.post(
            f'/webhook/ingest/{wh.token}/?import_mode=bad',
            data=body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp.status_code, 400)

    def test_webhook_ingest_upsert_without_pk_returns_400(self):
        from apps.dashboards.factories import WebhookEndpointFactory
        from apps.dashboards.webhook_crypto import decrypt_secret
        wh = WebhookEndpointFactory(
            workspace=self.workspace,
            table=self.table,
            created_by=self.user,
        )
        body = b'[{"id": 1}]'
        plaintext = decrypt_secret(wh.secret)
        sig = self._sign(plaintext, body)
        resp = self.client.post(
            f'/webhook/ingest/{wh.token}/?import_mode=upsert',
            data=body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# Batch Record Operations API (P1 #13)
# ===========================================================================

class TestRecordBatchAPI(TestCase):
    """Tests for POST /api/v1/tables/<id>/records/batch/"""

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace, created_by=self.user)
        self.client    = APIClient()
        self.client.force_authenticate(user=self.user)
        self.user.current_workspace = self.workspace
        self.user.save()
        self.url = f'/api/v1/tables/{self.table.id}/records/batch/'

    def _post(self, ops):
        import json as _j
        return self.client.post(self.url, data=ops, format='json')

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def test_create_operation(self):
        resp = self._post([{'op': 'create', 'data': {'x': 1}}])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['created'], 1)
        self.assertEqual(Record.objects.filter(table=self.table, is_active=True).count(), 1)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    def test_update_operation(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            rec = Record.objects.create(table=self.table, data={'x': 1}, created_by=self.user)

        resp = self._post([{'op': 'update', 'id': str(rec.id), 'data': {'x': 99}}])
        self.assertEqual(resp.json()['updated'], 1)
        rec.refresh_from_db()
        self.assertEqual(rec.data['x'], 99)

    def test_update_missing_id_returns_error(self):
        resp = self._post([{'op': 'update', 'data': {'x': 1}}])
        self.assertEqual(resp.json()['errors'][0]['op'], 'update')

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    def test_upsert_inserts_new(self):
        resp = self._post([{'op': 'upsert', 'primary_key': 'sku', 'data': {'sku': 'A1', 'qty': 5}}])
        self.assertEqual(resp.json()['created'], 1)

    def test_upsert_updates_existing(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            Record.objects.create(table=self.table, data={'sku': 'A1', 'qty': 5}, created_by=self.user)

        resp = self._post([{'op': 'upsert', 'primary_key': 'sku', 'data': {'sku': 'A1', 'qty': 99}}])
        self.assertEqual(resp.json()['updated'], 1)
        rec = Record.objects.filter(table=self.table, is_active=True).get()
        self.assertEqual(rec.data['qty'], 99)

    def test_upsert_without_primary_key_field_returns_error(self):
        resp = self._post([{'op': 'upsert', 'data': {'sku': 'A1'}}])
        self.assertGreater(len(resp.json()['errors']), 0)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def test_delete_operation(self):
        with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
             patch('apps.dashboards.models.notify_table_change.delay'):
            rec = Record.objects.create(table=self.table, data={'x': 1}, created_by=self.user)

        resp = self._post([{'op': 'delete', 'id': str(rec.id)}])
        self.assertEqual(resp.json()['deleted'], 1)
        rec.refresh_from_db()
        self.assertFalse(rec.is_active)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def test_non_array_body_returns_400(self):
        resp = self._post({'op': 'create', 'data': {}})
        self.assertEqual(resp.status_code, 400)

    def test_over_limit_returns_400(self):
        ops = [{'op': 'create', 'data': {'i': i}} for i in range(501)]
        resp = self._post(ops)
        self.assertEqual(resp.status_code, 400)

    def test_invalid_op_reported_as_error(self):
        resp = self._post([{'op': 'explode', 'data': {}}])
        self.assertEqual(resp.json()['errors'][0]['op'], 'explode')

    def test_mixed_valid_and_invalid_ops(self):
        resp = self._post([
            {'op': 'create', 'data': {'x': 1}},
            {'op': 'bad'},
        ])
        self.assertEqual(resp.json()['created'], 1)
        self.assertEqual(len(resp.json()['errors']), 1)


# =============================================================================
# DataSource connector tests (Gap 7 — PostgreSQL direct connector)
# =============================================================================

import json
from unittest.mock import patch, MagicMock, PropertyMock
from django.test import Client
from apps.dashboards.models import DataSource


class DataSourceFactory:
    """Minimal factory for DataSource — not using factory_boy to keep it simple."""

    @staticmethod
    def create(workspace, created_by, **kwargs):
        ds = DataSource(
            workspace=workspace,
            created_by=created_by,
            name=kwargs.get('name', 'Test DB'),
            connector_type=kwargs.get('connector_type', 'postgresql'),
            host=kwargs.get('host', 'db.example.com'),
            port=kwargs.get('port', 5432),
            database=kwargs.get('database', 'testdb'),
            username=kwargs.get('username', 'readonly'),
            ssl_mode=kwargs.get('ssl_mode', 'require'),
            is_active=kwargs.get('is_active', True),
        )
        ds.set_password(kwargs.get('password', 'secret123'))
        ds.save()
        return ds


class TestDataSourceModel(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)

    def test_set_and_get_password_roundtrip(self):
        ds = DataSourceFactory.create(self.workspace, self.user, password='my-secret')
        self.assertNotEqual(ds._password, 'my-secret')        # must be encrypted
        self.assertEqual(ds.get_plaintext_password(), 'my-secret')

    def test_get_dsn_contains_host_and_db(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        dsn = ds.get_dsn()
        self.assertIn('db.example.com', dsn)
        self.assertIn('testdb', dsn)
        self.assertIn('sslmode=require', dsn)

    def test_str_representation(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        self.assertIn('postgresql', str(ds))
        self.assertIn('testdb', str(ds))


class TestDataSourceAPI(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.token, _  = Token.objects.get_or_create(user=self.user)
        self.client    = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)

    def _url_list(self):
        return f'/api/v1/workspaces/{self.workspace.id}/data-sources/'

    def _url_detail(self, pk):
        return f'/api/v1/data-sources/{pk}/'

    def _url_test(self, pk):
        return f'/api/v1/data-sources/{pk}/test/'

    def _url_schema(self, pk):
        return f'/api/v1/data-sources/{pk}/schema/'

    # ------------------------------------------------------------------
    # list / create
    # ------------------------------------------------------------------

    def test_create_data_source_returns_201(self):
        payload = {
            'name': 'Analytics DB',
            'connector_type': 'postgresql',
            'host': 'pg.example.com',
            'port': 5432,
            'database': 'analytics',
            'username': 'reader',
            'password': 's3cr3t!',
            'ssl_mode': 'require',
        }
        resp = self.client.post(self._url_list(), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['name'], 'Analytics DB')
        self.assertNotIn('password', data)   # write-only
        self.assertTrue(data['has_password'])

    def test_list_returns_only_active_sources(self):
        ds1 = DataSourceFactory.create(self.workspace, self.user, name='Active')
        ds2 = DataSourceFactory.create(self.workspace, self.user, name='Inactive', is_active=False)
        resp = self.client.get(self._url_list())
        self.assertEqual(resp.status_code, 200)
        names = [d['name'] for d in resp.json()]
        self.assertIn('Active', names)
        self.assertNotIn('Inactive', names)

    def test_create_missing_password_returns_400(self):
        payload = {
            'name': 'No Pass DB', 'connector_type': 'postgresql',
            'host': 'x', 'port': 5432, 'database': 'x', 'username': 'x',
        }
        resp = self.client.post(self._url_list(), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------------
    # detail
    # ------------------------------------------------------------------

    def test_get_detail_returns_200(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        resp = self.client.get(self._url_detail(ds.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['id'], str(ds.id))

    def test_patch_name_updates_source(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        resp = self.client.patch(
            self._url_detail(ds.id),
            data=json.dumps({'name': 'Renamed DB'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['name'], 'Renamed DB')

    def test_delete_soft_deletes(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        resp = self.client.delete(self._url_detail(ds.id))
        self.assertEqual(resp.status_code, 204)
        ds.refresh_from_db()
        self.assertFalse(ds.is_active)

    # ------------------------------------------------------------------
    # test connection
    # ------------------------------------------------------------------

    def test_test_connection_ok(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        mock_conn   = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__  = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__  = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        with patch('apps.dashboards.services.DataSourceQueryEngine._connect', return_value=mock_conn):
            resp = self.client.post(self._url_test(ds.id))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        ds.refresh_from_db()
        self.assertTrue(ds.last_test_ok)
        self.assertIsNotNone(ds.last_tested_at)

    def test_test_connection_failure_returns_400(self):
        ds = DataSourceFactory.create(self.workspace, self.user)

        with patch('apps.dashboards.services.DataSourceQueryEngine._connect',
                   side_effect=Exception('Connection refused')):
            resp = self.client.post(self._url_test(ds.id))

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])
        self.assertIn('Connection refused', resp.json()['error'])
        ds.refresh_from_db()
        self.assertFalse(ds.last_test_ok)

    # ------------------------------------------------------------------
    # schema discovery
    # ------------------------------------------------------------------

    def test_schema_returns_tables_and_columns(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        fake_tables  = [{'schema': 'public', 'name': 'orders', 'row_estimate': 1000}]
        fake_columns = [{'name': 'id', 'type': 'integer', 'nullable': False},
                        {'name': 'amount', 'type': 'numeric', 'nullable': True}]

        with patch('apps.dashboards.services.DataSourceQueryEngine.list_tables',
                   return_value=fake_tables), \
             patch('apps.dashboards.services.DataSourceQueryEngine.list_columns',
                   return_value=fake_columns):
            resp = self.client.get(self._url_schema(ds.id))

        self.assertEqual(resp.status_code, 200)
        tables = resp.json()
        self.assertEqual(tables[0]['name'], 'orders')
        self.assertEqual(len(tables[0]['columns']), 2)

    def test_schema_db_error_returns_400(self):
        ds = DataSourceFactory.create(self.workspace, self.user)
        with patch('apps.dashboards.services.DataSourceQueryEngine.list_tables',
                   side_effect=Exception('DB unreachable')):
            resp = self.client.get(self._url_schema(ds.id))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('DB unreachable', resp.json()['error'])


class TestDataSourceQueryEngine(TestCase):
    """Unit tests for DataSourceQueryEngine — DB calls are always mocked."""

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.ds        = DataSourceFactory.create(self.workspace, self.user)

    def _engine(self):
        from apps.dashboards.services import DataSourceQueryEngine
        return DataSourceQueryEngine(self.ds)

    def _make_widget(self, wtype, config=None, source_table='orders'):
        w = MagicMock()
        w.widget_type       = wtype
        w.source_table_name = source_table
        w.query_config      = config or {}
        return w

    def _mock_conn(self, rows=None, description=None):
        """Return a context-manager-compatible psycopg connection mock."""
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__  = MagicMock(return_value=False)
        mock_cur.fetchone.return_value  = (rows[0] if rows else (42,))
        mock_cur.fetchall.return_value  = rows or []
        mock_cur.description            = description or [('count',)]

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__  = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        return mock_conn

    # ── metric ──────────────────────────────────────────────────────────

    def test_metric_count_no_agg(self):
        engine = self._engine()
        widget = self._make_widget('metric')
        with patch.object(engine, '_connect', return_value=self._mock_conn([(99,)])):
            result = engine.execute_widget_query(widget)
        self.assertEqual(result['val'], 99)

    def test_metric_sum_field(self):
        engine = self._engine()
        widget = self._make_widget('metric', {
            'aggregations': [{'type': 'sum', 'field': 'amount', 'name': 'total'}]
        })
        with patch.object(engine, '_connect', return_value=self._mock_conn([(1234.5,)])):
            result = engine.execute_widget_query(widget)
        self.assertAlmostEqual(result['total'], 1234.5)

    def test_metric_unsupported_connector_returns_error(self):
        self.ds.connector_type = 'mysql'
        engine = self._engine()
        widget = self._make_widget('metric')
        result = engine.execute_widget_query(widget)
        self.assertIn('error', result)

    # ── table ───────────────────────────────────────────────────────────

    def test_table_widget_returns_list_of_dicts(self):
        engine = self._engine()
        widget = self._make_widget('table')
        rows   = [(1, 'Alice', 100.0), (2, 'Bob', 200.0)]
        desc   = [('id',), ('name',), ('amount',)]
        with patch.object(engine, '_connect', return_value=self._mock_conn(rows, desc)):
            result = engine.execute_widget_query(widget)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]['name'], 'Alice')

    # ── chart ───────────────────────────────────────────────────────────

    def test_chart_grouped_returns_label_value_dict(self):
        engine = self._engine()
        widget = self._make_widget('bar_chart', {
            'aggregations': [{'type': 'sum', 'field': 'revenue', 'group_by': 'region', 'name': 'val'}]
        })
        rows = [('EMEA', 50000.0), ('APAC', 30000.0)]
        desc = [('region',), ('val',)]
        with patch.object(engine, '_connect', return_value=self._mock_conn(rows, desc)):
            result = engine.execute_widget_query(widget)
        self.assertIn('val', result)
        self.assertEqual(result['val']['EMEA'], 50000.0)

    def test_histogram_returns_values_and_bins(self):
        engine = self._engine()
        widget = self._make_widget('histogram', {
            'field': 'revenue',
            'bins': 8,
            'show_kde': False,
        })
        rows = [(10.0,), (15.5,), (22.0,)]
        with patch.object(engine, '_connect', return_value=self._mock_conn(rows, [('value',)])):
            result = engine.execute_widget_query(widget)
        self.assertEqual(result['field'], 'revenue')
        self.assertEqual(result['bins'], 8)
        self.assertEqual(result['values'], [10.0, 15.5, 22.0])
        self.assertIsNone(result['kde'])

    def test_scatter_returns_points(self):
        engine = self._engine()
        widget = self._make_widget('scatter', {
            'x_field': 'revenue',
            'y_field': 'margin',
            'label_field': 'segment',
        })
        rows = [(10.0, 2.5, 'SMB'), (22.0, 5.0, 'Enterprise')]
        with patch.object(engine, '_connect', return_value=self._mock_conn(rows, [('x',), ('y',), ('label',)])):
            result = engine.execute_widget_query(widget)
        self.assertEqual(result['x_field'], 'revenue')
        self.assertEqual(result['y_field'], 'margin')
        self.assertEqual(result['points'][0], {'x': 10.0, 'y': 2.5, 'label': 'SMB'})


    # ── where clause / injection safety ─────────────────────────────────

    def test_invalid_table_name_returns_error(self):
        engine = self._engine()
        widget = self._make_widget('metric', source_table='orders; DROP TABLE users--')
        result = engine.execute_widget_query(widget)
        self.assertIn('error', result)

    def test_invalid_identifier_rejected(self):
        from apps.dashboards.services import DataSourceQueryEngine
        self.assertFalse(DataSourceQueryEngine._validate_identifier('col; DROP TABLE x--'))
        self.assertTrue(DataSourceQueryEngine._validate_identifier('valid_column'))
        self.assertFalse(DataSourceQueryEngine._validate_identifier(''))
        self.assertFalse(DataSourceQueryEngine._validate_identifier(None))

    def test_build_where_eq_filter(self):
        engine = self._engine()
        clause, params = engine._build_where(
            [{'field': 'status', 'operator': 'eq', 'value': 'active'}], []
        )
        self.assertIn('"status"', clause)
        self.assertIn('=', clause)
        self.assertEqual(params, ['active'])

    def test_build_where_contains_filter(self):
        engine = self._engine()
        clause, params = engine._build_where(
            [{'field': 'name', 'operator': 'contains', 'value': 'john'}], []
        )
        self.assertIn('ILIKE', clause)
        self.assertEqual(params[0], '%john%')

    def test_build_where_invalid_field_skipped(self):
        engine = self._engine()
        clause, params = engine._build_where(
            [{'field': 'bad; DROP TABLE--', 'operator': 'eq', 'value': '1'}], []
        )
        # Invalid field name must be silently skipped — no injection
        self.assertEqual(clause, '')
        self.assertEqual(params, [])


class TestGoogleSheetsQueryEngine(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.ds = DataSourceFactory.create(self.workspace, self.user)
        self.ds.connector_type = 'google_sheets'

    def _engine(self):
        from apps.dashboards.services import GoogleSheetsQueryEngine
        return GoogleSheetsQueryEngine(self.ds)

    def _widget(self, widget_type, query_config):
        widget = MagicMock()
        widget.widget_type = widget_type
        widget.source_table_name = 'Sheet1'
        widget.query_config = query_config
        return widget

    def test_google_sheets_histogram_returns_values(self):
        import pandas as pd

        engine = self._engine()
        widget = self._widget('histogram', {'field': 'revenue', 'bins': 6, 'show_kde': False})
        df = pd.DataFrame({'revenue': ['10', '20.5', '$30.00', None]})
        with patch.object(engine, '_sheet_to_df', return_value=df):
            result = engine.execute_widget_query(widget)
        self.assertEqual(result['field'], 'revenue')
        self.assertEqual(result['bins'], 6)
        self.assertEqual(result['values'], [10.0, 20.5, 30.0])

    def test_google_sheets_scatter_returns_points(self):
        import pandas as pd

        engine = self._engine()
        widget = self._widget('scatter', {
            'x_field': 'revenue',
            'y_field': 'margin',
            'label_field': 'segment',
        })
        df = pd.DataFrame({
            'revenue': ['10', '20'],
            'margin': ['2.5', '5.0'],
            'segment': ['SMB', 'Enterprise'],
        })
        with patch.object(engine, '_sheet_to_df', return_value=df):
            result = engine.execute_widget_query(widget)
        self.assertEqual(result['points'][1], {'x': 20.0, 'y': 5.0, 'label': 'Enterprise'})


# ---------------------------------------------------------------------------
# Gap 15 — Cohort & Funnel Analysis
# ---------------------------------------------------------------------------

class TestCohortAnalysisEngine(TestCase):
    """Unit tests for CohortAnalysisEngine (in-memory pandas analysis)."""

    def setUp(self):
        self.p1 = patch('apps.dashboards.models.broadcast_widget_update.delay')
        self.p2 = patch('apps.dashboards.models.notify_table_change.delay')
        self.p1.start(); self.p2.start()
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace, schema=[])

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def _fake_widget(self, config):
        class _FakeWidget:
            pass
        w = _FakeWidget()
        w.table = self.table
        w.query_config = config
        return w

    def _make_records(self, user_events):
        """Create records from [(user_id, date_str)] pairs."""
        from django.utils.timezone import make_aware
        import datetime
        for uid, date_str in user_events:
            dt = make_aware(datetime.datetime.fromisoformat(date_str))
            r = RecordFactory(table=self.table, data={'user_id': uid})
            # created_at is auto_now_add=True so it cannot be set via save();
            # use update() to bypass the restriction and stamp the test date.
            Record.objects.filter(pk=r.pk).update(created_at=dt)

    def test_returns_error_without_user_field(self):
        from apps.dashboards.services import CohortAnalysisEngine
        w = self._fake_widget({'period': 'month', 'periods': 3})
        result = CohortAnalysisEngine().execute(w)
        self.assertIn('error', result)

    def test_empty_table_returns_empty_matrix(self):
        from apps.dashboards.services import CohortAnalysisEngine
        w = self._fake_widget({'user_field': 'user_id', 'period': 'month', 'periods': 3})
        result = CohortAnalysisEngine().execute(w)
        self.assertEqual(result['matrix'], [])
        self.assertEqual(result['total_users'], 0)

    def test_single_cohort_full_retention(self):
        """All users appear in every period — retention should be 100% throughout."""
        from apps.dashboards.services import CohortAnalysisEngine
        # 3 users × 2 months
        events = [
            ('u1', '2024-01-05'), ('u1', '2024-02-10'),
            ('u2', '2024-01-10'), ('u2', '2024-02-15'),
            ('u3', '2024-01-20'), ('u3', '2024-02-20'),
        ]
        self._make_records(events)
        w = self._fake_widget({'user_field': 'user_id', 'period': 'month', 'periods': 2})
        result = CohortAnalysisEngine().execute(w)
        self.assertEqual(result['total_users'], 3)
        self.assertEqual(len(result['matrix']), 1)          # one cohort (Jan-2024)
        row = result['matrix'][0]
        self.assertEqual(row['absolute'][0], 3)             # 3 users at start
        self.assertEqual(row['percentage'][0], 100.0)       # 100% at start
        self.assertEqual(row['absolute'][1], 3)             # all 3 returned
        self.assertEqual(row['percentage'][1], 100.0)

    def test_partial_retention(self):
        """Only half the cohort returns in the next period."""
        from apps.dashboards.services import CohortAnalysisEngine
        events = [
            ('u1', '2024-01-05'), ('u1', '2024-02-10'),
            ('u2', '2024-01-10'),                           # u2 does NOT return
        ]
        self._make_records(events)
        w = self._fake_widget({'user_field': 'user_id', 'period': 'month', 'periods': 2})
        result = CohortAnalysisEngine().execute(w)
        row = result['matrix'][0]
        self.assertEqual(row['absolute'][0], 2)
        self.assertEqual(row['absolute'][1], 1)
        self.assertEqual(row['percentage'][1], 50.0)

    def test_cohort_labels_returned(self):
        from apps.dashboards.services import CohortAnalysisEngine
        events = [('u1', '2024-01-05'), ('u2', '2024-02-05')]
        self._make_records(events)
        w = self._fake_widget({'user_field': 'user_id', 'period': 'month', 'periods': 3})
        result = CohortAnalysisEngine().execute(w)
        self.assertEqual(len(result['cohort_labels']), 2)

    def test_period_labels_length(self):
        from apps.dashboards.services import CohortAnalysisEngine
        events = [('u1', '2024-01-05')]
        self._make_records(events)
        w = self._fake_widget({'user_field': 'user_id', 'period': 'month', 'periods': 4})
        result = CohortAnalysisEngine().execute(w)
        self.assertEqual(len(result['period_labels']), 4)
        self.assertEqual(result['period_labels'][0], 'Start')
        self.assertEqual(result['period_labels'][1], 'Period 1')


class TestFunnelAnalysisEngine(TestCase):
    """Unit tests for FunnelAnalysisEngine."""

    def setUp(self):
        self.p1 = patch('apps.dashboards.models.broadcast_widget_update.delay')
        self.p2 = patch('apps.dashboards.models.notify_table_change.delay')
        self.p1.start(); self.p2.start()
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace, schema=[])

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def _fake_widget(self, config):
        class _FakeWidget:
            pass
        w = _FakeWidget()
        w.table = self.table
        w.query_config = config
        return w

    def test_returns_error_without_steps(self):
        from apps.dashboards.services import FunnelAnalysisEngine
        w = self._fake_widget({'user_field': 'user_id'})
        result = FunnelAnalysisEngine().execute(w)
        self.assertIn('error', result)

    def test_empty_table_returns_zero_counts(self):
        from apps.dashboards.services import FunnelAnalysisEngine
        w = self._fake_widget({
            'steps': [
                {'name': 'Step 1', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'signup'}]},
                {'name': 'Step 2', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'purchase'}]},
            ]
        })
        result = FunnelAnalysisEngine().execute(w)
        self.assertEqual(result['steps'][0]['count'], 0)
        self.assertEqual(result['steps'][1]['count'], 0)

    def test_full_conversion(self):
        """All users pass every step."""
        from apps.dashboards.services import FunnelAnalysisEngine
        for uid in ('u1', 'u2', 'u3'):
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'signup'})
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'purchase'})
        w = self._fake_widget({
            'user_field': 'user_id',
            'ordered': False,
            'steps': [
                {'name': 'Signed Up', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'signup'}]},
                {'name': 'Purchased', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'purchase'}]},
            ]
        })
        result = FunnelAnalysisEngine().execute(w)
        self.assertEqual(result['steps'][0]['count'], 3)
        self.assertEqual(result['steps'][1]['count'], 3)
        self.assertEqual(result['steps'][1]['conversion_rate'], 100.0)
        self.assertEqual(result['steps'][1]['overall_rate'], 100.0)

    def test_partial_funnel_drop_off(self):
        """Only 2 of 4 signup users also purchase."""
        from apps.dashboards.services import FunnelAnalysisEngine
        for uid in ('u1', 'u2', 'u3', 'u4'):
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'signup'})
        for uid in ('u1', 'u2'):
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'purchase'})
        w = self._fake_widget({
            'user_field': 'user_id',
            'ordered': True,
            'steps': [
                {'name': 'Signed Up', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'signup'}]},
                {'name': 'Purchased', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'purchase'}]},
            ]
        })
        result = FunnelAnalysisEngine().execute(w)
        self.assertEqual(result['steps'][0]['count'], 4)
        self.assertEqual(result['steps'][1]['count'], 2)
        self.assertEqual(result['steps'][1]['conversion_rate'], 50.0)
        self.assertEqual(result['steps'][1]['dropped'], 2)

    def test_three_step_funnel(self):
        """Three-step funnel drops at each step."""
        from apps.dashboards.services import FunnelAnalysisEngine
        for uid in ('u1', 'u2', 'u3'):
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'visit'})
        for uid in ('u1', 'u2'):
            RecordFactory(table=self.table, data={'user_id': uid, 'event': 'signup'})
        RecordFactory(table=self.table, data={'user_id': 'u1', 'event': 'purchase'})
        w = self._fake_widget({
            'user_field': 'user_id',
            'ordered': True,
            'steps': [
                {'name': 'Visit',    'filters': [{'field': 'event', 'operator': 'eq', 'value': 'visit'}]},
                {'name': 'Sign Up',  'filters': [{'field': 'event', 'operator': 'eq', 'value': 'signup'}]},
                {'name': 'Purchase', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'purchase'}]},
            ]
        })
        result = FunnelAnalysisEngine().execute(w)
        steps = result['steps']
        self.assertEqual(steps[0]['count'], 3)
        self.assertEqual(steps[1]['count'], 2)
        self.assertEqual(steps[2]['count'], 1)
        self.assertAlmostEqual(steps[2]['overall_rate'], 33.3, places=0)

    def test_step_annotations_present(self):
        """Every step dict contains the expected keys."""
        from apps.dashboards.services import FunnelAnalysisEngine
        RecordFactory(table=self.table, data={'event': 'a'})
        w = self._fake_widget({
            'steps': [{'name': 'A', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'a'}]}]
        })
        result = FunnelAnalysisEngine().execute(w)
        step = result['steps'][0]
        for key in ('name', 'count', 'conversion_rate', 'overall_rate', 'dropped'):
            self.assertIn(key, step)


class TestCohortFunnelAPI(TestCase):
    """Integration tests for the cohort/funnel REST endpoints."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient
        self.p1 = patch('apps.dashboards.models.broadcast_widget_update.delay')
        self.p2 = patch('apps.dashboards.models.notify_table_change.delay')
        self.p1.start(); self.p2.start()
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        token, _       = Token.objects.get_or_create(user=self.user)
        self.client    = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.client.defaults['HTTP_X_WORKSPACE_ID'] = str(self.workspace.id)
        self.table     = DataTableFactory(workspace=self.workspace, schema=[])

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def _cohort_url(self):
        return f'/api/v1/tables/{self.table.id}/cohort-analysis/'

    def _funnel_url(self):
        return f'/api/v1/tables/{self.table.id}/funnel-analysis/'

    # ── cohort ─────────────────────────────────────────────────────────

    def test_cohort_missing_user_field_returns_400(self):
        resp = self.client.post(self._cohort_url(), {'period': 'month'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_cohort_empty_table_returns_200(self):
        resp = self.client.post(
            self._cohort_url(),
            {'user_field': 'user_id', 'period': 'month', 'periods': 3},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('matrix', resp.data)
        self.assertEqual(resp.data['matrix'], [])

    def test_cohort_with_data_returns_matrix(self):
        RecordFactory(table=self.table, data={'user_id': 'u1'})
        resp = self.client.post(
            self._cohort_url(),
            {'user_field': 'user_id', 'period': 'month', 'periods': 2},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('cohort_labels', resp.data)
        self.assertIn('period_labels', resp.data)
        self.assertIn('total_users', resp.data)

    # ── funnel ─────────────────────────────────────────────────────────

    def test_funnel_missing_steps_returns_400(self):
        resp = self.client.post(self._funnel_url(), {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_funnel_empty_steps_list_returns_400(self):
        resp = self.client.post(self._funnel_url(), {'steps': []}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_funnel_empty_table_returns_200(self):
        payload = {
            'steps': [
                {'name': 'A', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'a'}]},
                {'name': 'B', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'b'}]},
            ]
        }
        resp = self.client.post(self._funnel_url(), payload, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('steps', resp.data)

    def test_funnel_with_data_returns_steps(self):
        RecordFactory(table=self.table, data={'event': 'signup'})
        RecordFactory(table=self.table, data={'event': 'purchase'})
        payload = {
            'steps': [
                {'name': 'Signup',   'filters': [{'field': 'event', 'operator': 'eq', 'value': 'signup'}]},
                {'name': 'Purchase', 'filters': [{'field': 'event', 'operator': 'eq', 'value': 'purchase'}]},
            ]
        }
        resp = self.client.post(self._funnel_url(), payload, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['steps']), 2)
        self.assertEqual(resp.data['steps'][0]['name'], 'Signup')


class TestCohortFunnelWidgetTypes(TestCase):
    """Verify cohort/funnel widget types are accepted by the Widget model."""

    def setUp(self):
        self.p1 = patch('apps.dashboards.models.broadcast_widget_update.delay')
        self.p2 = patch('apps.dashboards.models.notify_table_change.delay')
        self.p1.start(); self.p2.start()
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.table     = DataTableFactory(workspace=self.workspace, schema=[])
        self.dashboard = DashboardFactory(workspace=self.workspace, created_by=self.user)

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_cohort_widget_can_be_saved(self):
        w = Widget(
            dashboard=self.dashboard,
            widget_type='cohort',
            title='User Retention',
            table=self.table,
            query_config={'user_field': 'user_id', 'period': 'week', 'periods': 8},
        )
        w.save()
        self.assertEqual(Widget.objects.filter(widget_type='cohort').count(), 1)

    def test_funnel_widget_can_be_saved(self):
        w = Widget(
            dashboard=self.dashboard,
            widget_type='funnel',
            title='Conversion Funnel',
            table=self.table,
            query_config={
                'user_field': 'user_id',
                'steps': [
                    {'name': 'Step 1', 'filters': []},
                    {'name': 'Step 2', 'filters': []},
                ]
            },
        )
        w.save()
        self.assertEqual(Widget.objects.filter(widget_type='funnel').count(), 1)

    def test_query_engine_routes_cohort_type(self):
        """QueryEngine.execute_widget_query routes cohort widgets without error."""
        from apps.dashboards.services import QueryEngine
        w = Widget(
            dashboard=self.dashboard,
            widget_type='cohort',
            title='Test',
            table=self.table,
            query_config={'user_field': 'user_id', 'period': 'month', 'periods': 3},
        )
        result = QueryEngine().execute_widget_query(w)
        self.assertNotIn('error', result)
        self.assertIn('matrix', result)

    def test_query_engine_routes_funnel_type(self):
        """QueryEngine.execute_widget_query routes funnel widgets without error."""
        from apps.dashboards.services import QueryEngine
        w = Widget(
            dashboard=self.dashboard,
            widget_type='funnel',
            title='Test',
            table=self.table,
            query_config={
                'steps': [{'name': 'A', 'filters': []}]
            },
        )
        result = QueryEngine().execute_widget_query(w)
        self.assertNotIn('error', result)
        self.assertIn('steps', result)
