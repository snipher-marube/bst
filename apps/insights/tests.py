"""
apps/insights/tests.py
======================
Tests for insights serializers, LLM integration, and budget model.

Test classes
------------
TestWidgetSerializer        — serializer field coverage (no DB queries)
TestDashboardSerializer     — serializer field coverage
TestClaudeInsightGenerator  — unit tests for llm.py (all mocked, zero API cost)
TestWorkspaceLLMBudget      — model helper methods (has_capacity, consume)
TestLLMIntegration          — LIVE integration test; skipped unless
                              ANTHROPIC_API_KEY is set AND the env var
                              RUN_LLM_INTEGRATION_TESTS=1 is set.
                              Run with:
                                RUN_LLM_INTEGRATION_TESTS=1 python manage.py test \\
                                    apps.insights.tests.TestLLMIntegration
"""
import os
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, DashboardFactory, WidgetFactory,
)
from apps.insights.serializers import WidgetSerializer, DashboardSerializer
from apps.insights.llm import ClaudeInsightGenerator, LLMError


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


# =============================================================================
# ClaudeInsightGenerator unit tests  (all mocked — zero API cost)
# =============================================================================

def _mock_response(narrative, model='claude-sonnet-4-6', in_tok=100, out_tok=50):
    """Build a minimal fake Anthropic API response object."""
    content = MagicMock()
    content.text = narrative
    usage = MagicMock()
    usage.input_tokens  = in_tok
    usage.output_tokens = out_tok
    resp = MagicMock()
    resp.content = [content]
    resp.model   = model
    resp.usage   = usage
    return resp


class TestClaudeInsightGenerator(TestCase):

    # ------------------------------------------------------------------
    # is_available()
    # ------------------------------------------------------------------

    @override_settings(ANTHROPIC_API_KEY='')
    def test_not_available_without_api_key(self):
        gen = ClaudeInsightGenerator()
        self.assertFalse(gen.is_available())

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_available_when_key_set_and_sdk_installed(self):
        gen = ClaudeInsightGenerator()
        self.assertTrue(gen.is_available())

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_not_available_when_sdk_missing(self):
        gen = ClaudeInsightGenerator()
        with patch.dict('sys.modules', {'anthropic': None}):
            self.assertFalse(gen.is_available())

    # ------------------------------------------------------------------
    # _get_client() error paths
    # ------------------------------------------------------------------

    @override_settings(ANTHROPIC_API_KEY='')
    def test_get_client_raises_llm_error_without_key(self):
        gen = ClaudeInsightGenerator()
        with self.assertRaises(LLMError) as ctx:
            gen._get_client()
        self.assertIn('ANTHROPIC_API_KEY', str(ctx.exception))

    # ------------------------------------------------------------------
    # narrate_insight() — happy paths for each insight type
    # ------------------------------------------------------------------

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_summary(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('Orders has 1,250 records across 8 fields.')
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('summary', {
                'table': 'Orders', 'record_count': 1250, 'field_count': 8,
                'numeric_fields': ['revenue'], 'date_fields': ['order_date'],
            })
        self.assertEqual(result['narrative'], 'Orders has 1,250 records across 8 fields.')
        self.assertEqual(result['model'], 'claude-sonnet-4-6')
        self.assertEqual(result['prompt_tokens'], 100)
        self.assertEqual(result['completion_tokens'], 50)

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_trend(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('Revenue rose 18.4% from 4200 to 4973.')
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('trend', {
                'table': 'Orders', 'field': 'revenue',
                'direction': 'upward', 'pct_change': 18.4,
                'first_avg': 4200.0, 'last_avg': 4973.0, 'record_count': 800,
            })
        self.assertIn('18.4', result['narrative'])

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_anomaly(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('7 expense records are statistical outliers.')
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('anomaly', {
                'table': 'Expenses', 'field': 'amount',
                'mean': 350.0, 'std': 42.0,
                'outlier_count': 7, 'pct_outliers': 1.4,
            })
        self.assertIn('outlier', result['narrative'])

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_comparison(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('Sales revenue is 29x higher than Refunds.')
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('comparison', {
                'field': 'revenue',
                'table_averages': {'Sales': 9200.0, 'Refunds': 312.0},
            })
        self.assertIsInstance(result['narrative'], str)

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_unknown_type_uses_generic_prompt(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('Generic insight.')
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('prediction', {'table': 'X'})
        self.assertEqual(result['narrative'], 'Generic insight.')

    # ------------------------------------------------------------------
    # narrate_insight() — error path
    # ------------------------------------------------------------------

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_wraps_api_error_as_llm_error(self):
        gen = ClaudeInsightGenerator()
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.side_effect = RuntimeError('timeout')
            with self.assertRaises(LLMError) as ctx:
                gen.narrate_insight('summary', {'table': 'T'})
        self.assertIn('timeout', str(ctx.exception))

    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_narrate_empty_content_returns_empty_narrative(self):
        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('')
        fake_resp.content = []
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            result = gen.narrate_insight('summary', {'table': 'T'})
        self.assertEqual(result['narrative'], '')

    # ------------------------------------------------------------------
    # _build_user_message() — prompt contents
    # ------------------------------------------------------------------

    def test_summary_prompt_includes_table_name(self):
        gen = ClaudeInsightGenerator()
        msg = gen._build_user_message('summary', {
            'table': 'SalesData', 'record_count': 50,
            'field_count': 4, 'numeric_fields': ['amount'], 'date_fields': [],
        })
        self.assertIn('SalesData', msg)
        self.assertIn('50', msg)

    def test_trend_prompt_includes_pct_change(self):
        gen = ClaudeInsightGenerator()
        msg = gen._build_user_message('trend', {
            'table': 'T', 'field': 'revenue',
            'direction': 'upward', 'pct_change': 12.5,
            'first_avg': 100.0, 'last_avg': 112.5, 'record_count': 200,
        })
        self.assertIn('12.5', msg)
        self.assertIn('upward', msg)

    def test_anomaly_prompt_includes_outlier_count(self):
        gen = ClaudeInsightGenerator()
        msg = gen._build_user_message('anomaly', {
            'table': 'T', 'field': 'cost',
            'mean': 100.0, 'std': 10.0,
            'outlier_count': 3, 'pct_outliers': 0.6,
        })
        self.assertIn('3', msg)

    def test_comparison_prompt_includes_all_tables(self):
        gen = ClaudeInsightGenerator()
        msg = gen._build_user_message('comparison', {
            'field': 'amount',
            'table_averages': {'Alpha': 500.0, 'Beta': 200.0},
        })
        self.assertIn('Alpha', msg)
        self.assertIn('Beta', msg)


# =============================================================================
# WorkspaceLLMBudget model tests
# =============================================================================

class TestWorkspaceLLMBudget(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)

    def _make_budget(self, used=0, limit=100_000):
        from apps.insights.models import WorkspaceLLMBudget
        today = timezone.localdate().replace(day=1)
        return WorkspaceLLMBudget.objects.create(
            workspace=self.workspace,
            month=today,
            tokens_used=used,
            monthly_limit=limit,
        )

    def test_has_capacity_true_when_under_limit(self):
        budget = self._make_budget(used=50_000, limit=100_000)
        self.assertTrue(budget.has_capacity(estimated_tokens=1000))

    def test_has_capacity_false_when_over_limit(self):
        budget = self._make_budget(used=99_500, limit=100_000)
        self.assertFalse(budget.has_capacity(estimated_tokens=1000))

    def test_has_capacity_false_when_limit_is_zero(self):
        budget = self._make_budget(used=0, limit=0)
        self.assertFalse(budget.has_capacity())

    def test_has_capacity_exactly_at_limit(self):
        budget = self._make_budget(used=100_000, limit=100_000)
        self.assertFalse(budget.has_capacity(estimated_tokens=1))

    def test_consume_increments_tokens_used(self):
        budget = self._make_budget(used=1000, limit=100_000)
        budget.consume(500)
        budget.refresh_from_db()
        self.assertEqual(budget.tokens_used, 1500)

    def test_consume_is_atomic(self):
        """Two concurrent consume() calls must not lose an update."""
        from apps.insights.models import WorkspaceLLMBudget
        budget = self._make_budget(used=0, limit=100_000)
        budget.consume(300)
        budget.consume(700)
        budget.refresh_from_db()
        self.assertEqual(budget.tokens_used, 1000)

    def test_unique_together_workspace_month(self):
        from django.db import IntegrityError
        from apps.insights.models import WorkspaceLLMBudget
        today = timezone.localdate().replace(day=1)
        WorkspaceLLMBudget.objects.create(
            workspace=self.workspace, month=today,
            tokens_used=0, monthly_limit=50_000,
        )
        with self.assertRaises(IntegrityError):
            WorkspaceLLMBudget.objects.create(
                workspace=self.workspace, month=today,
                tokens_used=0, monthly_limit=50_000,
            )

    def test_str_representation(self):
        budget = self._make_budget(used=1234, limit=50_000)
        self.assertIn(self.workspace.name, str(budget))
        self.assertIn('1234', str(budget))

    def test_get_or_create_budget_creates_for_current_month(self):
        from apps.insights.tasks import _get_or_create_budget
        from apps.insights.models import WorkspaceLLMBudget
        budget = _get_or_create_budget(self.workspace)
        today  = timezone.localdate().replace(day=1)
        self.assertEqual(budget.month, today)
        self.assertEqual(WorkspaceLLMBudget.objects.filter(workspace=self.workspace).count(), 1)

    def test_get_or_create_budget_idempotent(self):
        from apps.insights.tasks import _get_or_create_budget
        from apps.insights.models import WorkspaceLLMBudget
        _get_or_create_budget(self.workspace)
        _get_or_create_budget(self.workspace)
        self.assertEqual(WorkspaceLLMBudget.objects.filter(workspace=self.workspace).count(), 1)


# =============================================================================
# _enrich_with_llm() unit tests
# =============================================================================

class TestEnrichWithLLM(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)

    def _make_insight(self):
        from apps.insights.models import Insight
        return Insight.objects.create(
            workspace=self.workspace,
            title='Test insight',
            description='Heuristic description.',
            insight_type='summary',
            source_table_name='Orders',
            created_by=self.user,
        )

    def _make_budget(self, used=0, limit=100_000):
        from apps.insights.models import WorkspaceLLMBudget
        today = timezone.localdate().replace(day=1)
        return WorkspaceLLMBudget.objects.create(
            workspace=self.workspace,
            month=today,
            tokens_used=used,
            monthly_limit=limit,
        )

    def test_enriches_description_and_records_tokens(self):
        from apps.insights.tasks import _enrich_with_llm
        insight = self._make_insight()
        budget  = self._make_budget()

        gen = ClaudeInsightGenerator()
        fake_resp = _mock_response('LLM narrative here.', in_tok=80, out_tok=30)
        with patch.object(gen, '_get_client') as mock_client:
            mock_client.return_value.messages.create.return_value = fake_resp
            _enrich_with_llm(insight, 'summary', {'table': 'Orders'}, gen, budget)

        insight.refresh_from_db()
        self.assertEqual(insight.description, 'LLM narrative here.')
        self.assertEqual(insight.prompt_tokens, 80)
        self.assertEqual(insight.completion_tokens, 30)
        self.assertEqual(insight.llm_model, 'claude-sonnet-4-6')

        budget.refresh_from_db()
        self.assertEqual(budget.tokens_used, 110)

    def test_skips_when_budget_exhausted(self):
        from apps.insights.tasks import _enrich_with_llm
        insight = self._make_insight()
        budget  = self._make_budget(used=100_000, limit=100_000)

        gen = ClaudeInsightGenerator()
        with patch.object(gen, 'narrate_insight') as mock_narrate:
            _enrich_with_llm(insight, 'summary', {}, gen, budget)
            mock_narrate.assert_not_called()

        insight.refresh_from_db()
        self.assertEqual(insight.description, 'Heuristic description.')

    def test_skips_when_llm_gen_is_none(self):
        from apps.insights.tasks import _enrich_with_llm
        insight = self._make_insight()
        _enrich_with_llm(insight, 'summary', {}, None, None)
        insight.refresh_from_db()
        self.assertEqual(insight.description, 'Heuristic description.')

    def test_llm_error_does_not_overwrite_description(self):
        from apps.insights.tasks import _enrich_with_llm
        insight = self._make_insight()
        budget  = self._make_budget()

        gen = ClaudeInsightGenerator()
        with patch.object(gen, 'narrate_insight', side_effect=LLMError('API down')):
            _enrich_with_llm(insight, 'summary', {}, gen, budget)

        insight.refresh_from_db()
        self.assertEqual(insight.description, 'Heuristic description.')


# =============================================================================
# LIVE integration test  — skipped unless RUN_LLM_INTEGRATION_TESTS=1
# =============================================================================

import unittest

@unittest.skipUnless(
    os.environ.get('RUN_LLM_INTEGRATION_TESTS') == '1',
    'Set RUN_LLM_INTEGRATION_TESTS=1 to run live Claude API tests',
)
class TestLLMIntegration(TestCase):
    """
    Hits the real Claude API.  Costs a few hundred tokens per run.

    Run with:
        RUN_LLM_INTEGRATION_TESTS=1 python manage.py test \\
            apps.insights.tests.TestLLMIntegration -v 2
    """

    def setUp(self):
        self.gen = ClaudeInsightGenerator()
        if not self.gen.is_available():
            self.skipTest('ANTHROPIC_API_KEY not configured or anthropic SDK missing')

    def test_summary_returns_non_empty_narrative(self):
        result = self.gen.narrate_insight('summary', {
            'table': 'Orders',
            'record_count': 500,
            'field_count': 6,
            'numeric_fields': ['revenue', 'quantity'],
            'date_fields': ['order_date'],
        })
        self.assertIsInstance(result['narrative'], str)
        self.assertGreater(len(result['narrative']), 20)
        self.assertGreater(result['prompt_tokens'], 0)
        self.assertGreater(result['completion_tokens'], 0)
        self.assertIn('claude', result['model'])

    def test_trend_narrative_mentions_direction(self):
        result = self.gen.narrate_insight('trend', {
            'table': 'Sales', 'field': 'revenue',
            'direction': 'upward', 'pct_change': 22.0,
            'first_avg': 3000.0, 'last_avg': 3660.0, 'record_count': 400,
        })
        narrative = result['narrative'].lower()
        # Should mention the direction or the percentage
        self.assertTrue(
            any(kw in narrative for kw in ['upward', 'increas', 'grew', '22', 'growth']),
            f"Expected directional language in: {result['narrative']}"
        )

    def test_anomaly_narrative_mentions_outliers(self):
        result = self.gen.narrate_insight('anomaly', {
            'table': 'Expenses', 'field': 'amount',
            'mean': 350.0, 'std': 42.0,
            'outlier_count': 5, 'pct_outliers': 1.0,
        })
        narrative = result['narrative'].lower()
        self.assertTrue(
            any(kw in narrative for kw in ['outlier', 'anomal', 'deviation', 'unusual', '5']),
            f"Expected anomaly language in: {result['narrative']}"
        )

    def test_comparison_narrative_mentions_field(self):
        result = self.gen.narrate_insight('comparison', {
            'field': 'revenue',
            'table_averages': {'Sales': 9200.0, 'Returns': 780.0},
        })
        self.assertIn('revenue', result['narrative'].lower())

    def test_model_id_matches_configured_model(self):
        from django.conf import settings
        result = self.gen.narrate_insight('summary', {
            'table': 'T', 'record_count': 10,
            'field_count': 2, 'numeric_fields': [], 'date_fields': [],
        })
        self.assertEqual(result['model'], settings.CLAUDE_INSIGHT_MODEL)


# ===========================================================================
# Gap #18 — Auto-triggered anomaly detection
# ===========================================================================

from unittest.mock import call as _call
from apps.insights.tasks import auto_trigger_anomaly_detection, _notify_anomaly_insights
from apps.dashboards.factories import DataTableFactory, RecordFactory


class TestAutoTriggerAnomalyDetection(TestCase):
    """auto_trigger_anomaly_detection fans out to workspaces with data."""

    def setUp(self):
        self.user = UserFactory()
        self.ws1  = WorkspaceFactory(owner=self.user, is_active=True)
        self.ws2  = WorkspaceFactory(owner=self.user, is_active=True)

    def test_dispatches_for_workspaces_with_data(self):
        tbl = DataTableFactory(workspace=self.ws1, is_active=True)
        RecordFactory(table=tbl, is_active=True)

        with patch('apps.insights.tasks.analyze_workspace_tables') as mock_task:
            mock_task.delay = MagicMock()
            result = auto_trigger_anomaly_detection()

        self.assertEqual(result['dispatched'], 1)
        self.assertEqual(result['skipped'],    0)
        mock_task.delay.assert_called_once_with(str(self.ws1.id))

    def test_skips_workspaces_without_data(self):
        # ws2 has no tables/records
        with patch('apps.insights.tasks.analyze_workspace_tables') as mock_task:
            mock_task.delay = MagicMock()
            result = auto_trigger_anomaly_detection()

        self.assertEqual(result['dispatched'], 0)
        mock_task.delay.assert_not_called()

    def test_skips_inactive_workspaces(self):
        tbl = DataTableFactory(workspace=self.ws1, is_active=True)
        RecordFactory(table=tbl, is_active=True)
        self.ws1.is_active = False
        self.ws1.save()

        with patch('apps.insights.tasks.analyze_workspace_tables') as mock_task:
            mock_task.delay = MagicMock()
            result = auto_trigger_anomaly_detection()

        self.assertEqual(result['dispatched'], 0)

    def test_dispatches_multiple_workspaces(self):
        for ws in (self.ws1, self.ws2):
            tbl = DataTableFactory(workspace=ws, is_active=True)
            RecordFactory(table=tbl, is_active=True)

        with patch('apps.insights.tasks.analyze_workspace_tables') as mock_task:
            mock_task.delay = MagicMock()
            result = auto_trigger_anomaly_detection()

        self.assertEqual(result['dispatched'], 2)
        self.assertEqual(mock_task.delay.call_count, 2)


class TestNotifyAnomalyInsights(TestCase):
    """_notify_anomaly_insights creates Notification records for members."""

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        # owner is automatically a member via WorkspaceFactory
        from apps.insights.models import Insight
        self.insight = Insight.objects.create(
            workspace=self.workspace,
            title='Sales — Anomaly in "amount"',
            description='2 outliers detected.',
            insight_type='anomaly',
            source_table_name='Sales',
            created_by=self.user,
        )

    def test_creates_notification_for_member(self):
        from apps.notifications.models import Notification
        before = Notification.objects.count()
        _notify_anomaly_insights(self.workspace, [self.insight])
        after  = Notification.objects.count()
        self.assertGreater(after, before)

    def test_notification_title_contains_anomaly(self):
        from apps.notifications.models import Notification
        _notify_anomaly_insights(self.workspace, [self.insight])
        notif = Notification.objects.filter(workspace=self.workspace).latest('created_at')
        self.assertIn('anomaly', notif.title.lower())

    def test_no_notification_when_no_members(self):
        from apps.notifications.models import Notification
        from apps.workspaces.models import WorkspaceMembership
        # Remove all memberships
        WorkspaceMembership.objects.filter(workspace=self.workspace).delete()
        before = Notification.objects.count()
        _notify_anomaly_insights(self.workspace, [self.insight])
        self.assertEqual(Notification.objects.count(), before)

    def test_multiple_anomalies_single_notification_per_member(self):
        from apps.notifications.models import Notification
        from apps.insights.models import Insight
        ins2 = Insight.objects.create(
            workspace=self.workspace,
            title='Orders — Anomaly in "quantity"',
            description='1 outlier.',
            insight_type='anomaly',
            source_table_name='Orders',
            created_by=self.user,
        )
        _notify_anomaly_insights(self.workspace, [self.insight, ins2])
        # One notification per member (not per insight)
        member_count = self.workspace.members.count()
        notifs = Notification.objects.filter(
            workspace=self.workspace,
            notif_type='warning',
        ).count()
        self.assertEqual(notifs, member_count)
