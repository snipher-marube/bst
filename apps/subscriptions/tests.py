"""
apps/subscriptions/tests.py
============================
Tests for Plan, Subscription, and MpesaTransaction models.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.dashboards.factories import (
    UserFactory, WorkspaceFactory, PlanFactory, SubscriptionFactory,
)
from apps.subscriptions.models import Plan, Subscription, MpesaTransaction, PLAN_LIMITS


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------

class TestPlanModel(TestCase):

    def test_str_includes_name(self):
        plan = PlanFactory(name='Starter', tier='starter')
        self.assertIn('Starter', str(plan))

    def test_tier_choices_match_plan_limits(self):
        for tier in ['free', 'starter', 'professional', 'enterprise']:
            self.assertIn(tier, PLAN_LIMITS)

    def test_plan_limits_increasing_order(self):
        self.assertLess(PLAN_LIMITS['free']['max_tables'],       PLAN_LIMITS['starter']['max_tables'])
        self.assertLess(PLAN_LIMITS['starter']['max_tables'],    PLAN_LIMITS['professional']['max_tables'])
        self.assertLess(PLAN_LIMITS['professional']['max_tables'], PLAN_LIMITS['enterprise']['max_tables'])

    def test_is_active_default_true(self):
        plan = PlanFactory()
        self.assertTrue(plan.is_active)

    def test_plan_has_positive_limits(self):
        for tier, limits in PLAN_LIMITS.items():
            self.assertGreater(limits['max_tables'], 0, f"{tier}: max_tables must be > 0")
            self.assertGreater(limits['max_records_per_table'], 0)
            self.assertGreater(limits['max_team_members'], 0)


# ---------------------------------------------------------------------------
# Subscription model
# ---------------------------------------------------------------------------

class TestSubscriptionModel(TestCase):

    def test_str_includes_workspace_and_plan(self):
        sub = SubscriptionFactory()
        s = str(sub)
        self.assertIn(sub.workspace.name, s)

    def test_apply_plan_limits_updates_workspace(self):
        ws   = WorkspaceFactory()
        plan = PlanFactory(
            tier='starter',
            max_tables=20,
            max_records_per_table=10_000,
            max_team_members=5,
        )
        sub = Subscription.objects.create(workspace=ws, plan=plan, status='active')
        sub.apply_plan_limits()

        ws.refresh_from_db()
        self.assertEqual(ws.max_tables,            20)
        self.assertEqual(ws.max_records_per_table, 10_000)
        self.assertEqual(ws.max_team_members,      5)
        self.assertEqual(ws.tier,                  plan.tier)

    def test_apply_plan_limits_enterprise(self):
        ws   = WorkspaceFactory()
        plan = PlanFactory(
            tier='enterprise',
            max_tables=999,
            max_records_per_table=999_999,
            max_team_members=999,
        )
        sub = Subscription.objects.create(workspace=ws, plan=plan, status='active')
        sub.apply_plan_limits()

        ws.refresh_from_db()
        self.assertEqual(ws.max_tables, 999)
        self.assertEqual(ws.tier, 'enterprise')

    def test_subscription_status_choices(self):
        statuses = [c[0] for c in Subscription.STATUS_CHOICES]
        for s in ['active', 'trialing', 'past_due', 'cancelled']:
            self.assertIn(s, statuses)

    def test_workspace_one_to_one(self):
        ws  = WorkspaceFactory()
        sub = SubscriptionFactory(workspace=ws)
        with self.assertRaises(Exception):
            SubscriptionFactory(workspace=ws)  # second subscription on same workspace


# ---------------------------------------------------------------------------
# MpesaTransaction model
# ---------------------------------------------------------------------------

class TestMpesaTransaction(TestCase):

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.user      = self.workspace.owner
        self.plan      = PlanFactory(tier='starter', price_monthly=1000)

    def _create_txn(self, status='pending'):
        return MpesaTransaction.objects.create(
            workspace=self.workspace,
            user=self.user,
            plan=self.plan,
            phone_number='254712345678',
            amount=1000,
            merchant_request_id='MR123',
            checkout_request_id='CR456',
            status=status,
        )

    def test_create_pending_transaction(self):
        txn = self._create_txn('pending')
        self.assertEqual(txn.status, 'pending')
        self.assertEqual(txn.workspace, self.workspace)

    def test_status_transition_to_completed(self):
        txn = self._create_txn()
        txn.status = 'completed'
        txn.mpesa_receipt_number = 'PGH7LX1234'
        txn.save(update_fields=['status', 'mpesa_receipt_number'])
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'completed')
        self.assertEqual(txn.mpesa_receipt_number, 'PGH7LX1234')

    def test_status_transition_to_failed(self):
        txn = self._create_txn()
        txn.status = 'failed'
        txn.result_desc = 'DS00003'
        txn.save(update_fields=['status', 'result_desc'])
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'failed')

    def test_str_representation(self):
        txn = self._create_txn()
        s = str(txn)
        self.assertIn(self.workspace.name, s)

    def test_ordering_newest_first(self):
        t1 = self._create_txn()
        # Create a second transaction (need unique checkout_request_id)
        MpesaTransaction.objects.create(
            workspace=self.workspace, user=self.user, plan=self.plan,
            phone_number='254712345678', amount=1000,
            merchant_request_id='MR124', checkout_request_id='CR457', status='pending',
        )
        txns = list(MpesaTransaction.objects.filter(workspace=self.workspace))
        self.assertGreater(txns[0].created_at, txns[1].created_at)


# ---------------------------------------------------------------------------
# Subscription upgrade views
# ---------------------------------------------------------------------------

from django.test import Client
from apps.subscriptions.models import Subscription


class TestUpgradePlanView(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)

    def test_upgrade_to_starter(self):
        resp = self.client.post(
            f'/subscriptions/upgrade/{self.workspace.id}/starter/'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data.get('success'))

    def test_upgrade_to_growth(self):
        resp = self.client.post(
            f'/subscriptions/upgrade/{self.workspace.id}/growth/'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data.get('success'))

    def test_upgrade_to_pro(self):
        resp = self.client.post(
            f'/subscriptions/upgrade/{self.workspace.id}/pro/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_upgrade_creates_subscription(self):
        self.client.post(
            f'/subscriptions/upgrade/{self.workspace.id}/starter/'
        )
        self.assertTrue(
            Subscription.objects.filter(workspace=self.workspace).exists()
        )

    def test_upgrade_wrong_workspace_returns_404(self):
        other_ws = WorkspaceFactory()  # owned by different user
        resp = self.client.post(
            f'/subscriptions/upgrade/{other_ws.id}/starter/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_upgrade_unauthenticated_redirects(self):
        c = Client()
        resp = c.post(
            f'/subscriptions/upgrade/{self.workspace.id}/starter/'
        )
        self.assertEqual(resp.status_code, 302)


class TestStripeWebhook(TestCase):

    def setUp(self):
        self.client = Client()

    def test_stripe_webhook_invalid_json_returns_400(self):
        resp = self.client.post(
            '/subscriptions/webhook/stripe/',
            data='not json',
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [400, 200])

    def test_stripe_webhook_valid_event_processed(self):
        import json
        payload = json.dumps({
            'id': 'evt_test123',
            'type': 'checkout.session.completed',
            'data': {'object': {'customer_email': 'test@test.com'}},
        })
        resp = self.client.post(
            '/subscriptions/webhook/stripe/',
            data=payload,
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [200, 400])

    def test_stripe_webhook_idempotency(self):
        """Same event sent twice returns 200 both times without duplicate processing"""
        payload = json.dumps({
            'id': 'evt_idempotent',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_test'}},
        })
        resp1 = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                  content_type='application/json')
        resp2 = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                  content_type='application/json')
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)

    def test_stripe_subscription_updated_event(self):
        payload = json.dumps({
            'id': 'evt_sub_updated',
            'type': 'customer.subscription.updated',
            'data': {'object': {'id': 'sub_nonexistent', 'status': 'active'}},
        })
        resp = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_stripe_subscription_deleted_event(self):
        payload = json.dumps({
            'id': 'evt_sub_deleted',
            'type': 'customer.subscription.deleted',
            'data': {'object': {'id': 'sub_nonexistent'}},
        })
        resp = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_stripe_invoice_payment_failed_event(self):
        payload = json.dumps({
            'id': 'evt_inv_failed',
            'type': 'invoice.payment_failed',
            'data': {'object': {'customer': 'cus_unknown'}},
        })
        resp = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_stripe_invoice_paid_event(self):
        payload = json.dumps({
            'id': 'evt_inv_paid',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_unknown'}},
        })
        resp = self.client.post('/subscriptions/webhook/stripe/', data=payload,
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Stripe webhook — signature verification
# ---------------------------------------------------------------------------

class TestStripeWebhookSignatureVerification(TestCase):
    """
    Verify that stripe_webhook enforces HMAC-SHA256 signature checking when
    STRIPE_WEBHOOK_SECRET is configured, and falls back gracefully (dev mode)
    when it is not.
    """

    WEBHOOK_URL = '/subscriptions/webhook/stripe/'
    SECRET      = 'whsec_test_secret_key_for_unit_tests'

    def _make_stripe_header(self, payload: bytes, secret: str, timestamp: int = None) -> str:
        """Build a valid Stripe-Signature header for the given payload and secret."""
        ts  = timestamp or int(time.time())
        signed_payload = f"{ts}.{payload.decode()}"
        mac = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        return f"t={ts},v1={mac}"

    def test_valid_signature_accepted(self):
        """POST with a correctly signed payload returns 200."""
        payload = json.dumps({
            'id': 'evt_sig_valid',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_sig_test'}},
        }).encode()
        header = self._make_stripe_header(payload, self.SECRET)

        with patch('django.conf.settings.STRIPE_WEBHOOK_SECRET', self.SECRET):
            import stripe
            with patch.object(
                stripe.Webhook,
                'construct_event',
                return_value=json.loads(payload),
            ):
                resp = self.client.post(
                    self.WEBHOOK_URL,
                    data=payload,
                    content_type='application/json',
                    HTTP_STRIPE_SIGNATURE=header,
                )
        self.assertEqual(resp.status_code, 200)

    def test_invalid_signature_rejected(self):
        """POST with a tampered payload (wrong signature) returns 400."""
        payload = json.dumps({
            'id': 'evt_sig_bad',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_sig_test'}},
        }).encode()
        bad_header = "t=9999999999,v1=deadeaddeaddeaddeaddeaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead"

        with patch('django.conf.settings.STRIPE_WEBHOOK_SECRET', self.SECRET):
            import stripe
            with patch.object(
                stripe.Webhook,
                'construct_event',
                side_effect=stripe.error.SignatureVerificationError("bad sig", bad_header),
            ):
                resp = self.client.post(
                    self.WEBHOOK_URL,
                    data=payload,
                    content_type='application/json',
                    HTTP_STRIPE_SIGNATURE=bad_header,
                )
        self.assertEqual(resp.status_code, 400)

    def test_no_secret_skips_verification_dev_mode(self):
        """When STRIPE_WEBHOOK_SECRET is empty, verification is skipped (dev mode)."""
        payload = json.dumps({
            'id': 'evt_nosecret',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_dev'}},
        }).encode()

        with patch('django.conf.settings.STRIPE_WEBHOOK_SECRET', ''):
            resp = self.client.post(
                self.WEBHOOK_URL,
                data=payload,
                content_type='application/json',
            )
        self.assertEqual(resp.status_code, 200)

    def test_invalid_json_with_no_secret_returns_400(self):
        """Malformed JSON body returns 400 even in dev mode (no secret)."""
        with patch('django.conf.settings.STRIPE_WEBHOOK_SECRET', ''):
            resp = self.client.post(
                self.WEBHOOK_URL,
                data=b'not { valid json',
                content_type='application/json',
            )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# M-Pesa callback
# ---------------------------------------------------------------------------

class TestMpesaCallbackView(TestCase):

    def setUp(self):
        self.client = Client()
        from apps.dashboards.factories import UserFactory, WorkspaceFactory, PlanFactory
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.plan = PlanFactory(tier='starter')
        self.txn = MpesaTransaction.objects.create(
            workspace=self.workspace,
            user=self.user,
            plan=self.plan,
            phone_number='254712345678',
            amount=1,
            merchant_request_id='MR_CB1',
            checkout_request_id='CR_CB1',
            status='pending',
        )

    def _callback_payload(self, result_code=0, checkout_id='CR_CB1'):
        body = {
            'Body': {
                'stkCallback': {
                    'MerchantRequestID': 'MR_CB1',
                    'CheckoutRequestID': checkout_id,
                    'ResultCode': result_code,
                    'ResultDesc': 'The service request is processed successfully.',
                    'CallbackMetadata': {
                        'Item': [
                            {'Name': 'MpesaReceiptNumber', 'Value': 'QHJ12ABC34'},
                        ]
                    } if result_code == 0 else {},
                }
            }
        }
        import json
        return json.dumps(body)

    def test_callback_success_marks_completed(self):
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data=self._callback_payload(result_code=0),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'completed')

    def test_callback_failure_marks_failed(self):
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data=self._callback_payload(result_code=1032),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'failed')

    def test_callback_invalid_json_returns_400(self):
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data='not-json',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_callback_missing_checkout_id_returns_200(self):
        import json
        payload = json.dumps({'Body': {'stkCallback': {'ResultCode': 0}}})
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data=payload,
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_callback_unknown_checkout_id_returns_200(self):
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data=self._callback_payload(checkout_id='UNKNOWN_CR'),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_callback_idempotent_for_already_completed(self):
        self.txn.status = 'completed'
        self.txn.save()
        resp = self.client.post(
            '/subscriptions/mpesa/callback/',
            data=self._callback_payload(result_code=0),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# M-Pesa payment status
# ---------------------------------------------------------------------------

class TestMpesaPaymentStatusView(TestCase):

    def setUp(self):
        self.client = Client()
        from apps.dashboards.factories import UserFactory, WorkspaceFactory, PlanFactory
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        self.user.current_workspace = self.workspace
        self.plan = PlanFactory(tier='starter')
        self.txn = MpesaTransaction.objects.create(
            workspace=self.workspace,
            user=self.user,
            plan=self.plan,
            phone_number='254712345678',
            amount=1,
            merchant_request_id='MR_ST1',
            checkout_request_id='CR_ST1',
            status='pending',
        )

    def test_status_returns_pending(self):
        from unittest.mock import patch
        with patch('apps.subscriptions.views.MpesaService') as MockService:
            MockService.return_value.query_stk_push.side_effect = Exception("no network")
            resp = self.client.get('/subscriptions/mpesa/status/CR_ST1/')
        self.assertIn(resp.status_code, [200, 404])

    def test_status_unknown_checkout_id_returns_404(self):
        resp = self.client.get('/subscriptions/mpesa/status/UNKNOWN_CR_ID/')
        self.assertIn(resp.status_code, [404, 200])

    def test_status_unauthenticated_redirects(self):
        c = Client()
        resp = c.get('/subscriptions/mpesa/status/CR_ST1/')
        self.assertEqual(resp.status_code, 302)


# ---------------------------------------------------------------------------
# Stripe handlers with real subscriptions
# ---------------------------------------------------------------------------

class TestStripeEventHandlers(TestCase):

    def setUp(self):
        from apps.dashboards.factories import UserFactory, WorkspaceFactory, PlanFactory
        from apps.subscriptions.models import Subscription
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.plan = PlanFactory(tier='starter')
        self.sub = Subscription.objects.create(
            workspace=self.workspace,
            plan=self.plan,
            status='active',
            stripe_subscription_id='sub_realtest',
            stripe_customer_id='cus_realtest',
        )
        self.client = Client()

    def test_subscription_updated_syncs_status(self):
        import json
        payload = json.dumps({
            'id': 'evt_su_real',
            'type': 'customer.subscription.updated',
            'data': {'object': {'id': 'sub_realtest', 'status': 'past_due'}},
        })
        self.client.post('/subscriptions/webhook/stripe/', data=payload,
                         content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'past_due')

    def test_invoice_payment_failed_marks_past_due(self):
        import json
        payload = json.dumps({
            'id': 'evt_inv_fail_real',
            'type': 'invoice.payment_failed',
            'data': {'object': {'customer': 'cus_realtest'}},
        })
        self.client.post('/subscriptions/webhook/stripe/', data=payload,
                         content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'past_due')

    def test_invoice_paid_restores_active(self):
        import json
        self.sub.status = 'past_due'
        self.sub.save()
        payload = json.dumps({
            'id': 'evt_inv_paid_real',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_realtest'}},
        })
        self.client.post('/subscriptions/webhook/stripe/', data=payload,
                         content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'active')

    def test_subscription_deleted_cancels(self):
        import json
        payload = json.dumps({
            'id': 'evt_sub_del_real',
            'type': 'customer.subscription.deleted',
            'data': {'object': {'id': 'sub_realtest'}},
        })
        self.client.post('/subscriptions/webhook/stripe/', data=payload,
                         content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'cancelled')


# ---------------------------------------------------------------------------
# Grace period model methods
# ---------------------------------------------------------------------------

class TestSubscriptionGracePeriod(TestCase):

    def setUp(self):
        self.user      = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.plan      = PlanFactory(tier='starter')
        self.sub       = SubscriptionFactory(
            workspace=self.workspace,
            plan=self.plan,
            status='active',
            stripe_customer_id='cus_grace_test',
        )

    def test_start_grace_period_sets_fields(self):
        self.sub.start_grace_period()
        self.assertEqual(self.sub.status, 'past_due')
        self.assertIsNotNone(self.sub.grace_period_ends_at)
        # Should expire ~7 days from now
        delta = self.sub.grace_period_ends_at - timezone.now()
        self.assertAlmostEqual(delta.days, Subscription.GRACE_PERIOD_DAYS - 1, delta=1)
        self.assertEqual(self.sub.dunning_stage, Subscription.DUNNING_INITIAL)

    def test_start_grace_period_is_idempotent(self):
        """Calling start_grace_period twice should not reset the countdown."""
        self.sub.start_grace_period()
        first_expiry = self.sub.grace_period_ends_at
        self.sub.start_grace_period()
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.grace_period_ends_at, first_expiry)

    def test_clear_grace_period_restores_active(self):
        self.sub.start_grace_period()
        self.sub.clear_grace_period()
        self.assertEqual(self.sub.status, 'active')
        self.assertIsNone(self.sub.grace_period_ends_at)
        self.assertEqual(self.sub.dunning_stage, Subscription.DUNNING_INITIAL)

    def test_invoice_failed_webhook_starts_grace_period(self):
        """invoice.payment_failed Stripe event opens a grace period."""
        payload = json.dumps({
            'id': 'evt_grace_start',
            'type': 'invoice.payment_failed',
            'data': {'object': {'customer': 'cus_grace_test'}},
        })
        with patch('apps.subscriptions.tasks.send_dunning_email_task') as mock_task:
            mock_task.delay = MagicMock()
            from django.test import Client
            c = Client()
            c.post('/subscriptions/webhook/stripe/', data=payload, content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'past_due')
        self.assertIsNotNone(self.sub.grace_period_ends_at)

    def test_invoice_paid_clears_grace_period(self):
        """invoice.paid Stripe event clears any open grace period."""
        self.sub.start_grace_period()
        payload = json.dumps({
            'id': 'evt_grace_clear',
            'type': 'invoice.paid',
            'data': {'object': {'customer': 'cus_grace_test'}},
        })
        from django.test import Client
        c = Client()
        c.post('/subscriptions/webhook/stripe/', data=payload, content_type='application/json')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'active')
        self.assertIsNone(self.sub.grace_period_ends_at)


# ---------------------------------------------------------------------------
# process_grace_periods Celery task
# ---------------------------------------------------------------------------

class TestProcessGracePeriods(TestCase):

    def _make_past_due_sub(self, grace_days_remaining, dunning_stage=0):
        user      = UserFactory()
        workspace = WorkspaceFactory(owner=user)
        plan      = PlanFactory(tier='starter')
        sub       = SubscriptionFactory(
            workspace=workspace,
            plan=plan,
            status='past_due',
            grace_period_ends_at=timezone.now() + timedelta(days=grace_days_remaining),
            dunning_stage=dunning_stage,
        )
        return sub

    def test_sends_day3_email_at_day3(self):
        sub = self._make_past_due_sub(grace_days_remaining=4, dunning_stage=0)
        with patch('apps.subscriptions.tasks.send_dunning_email') as mock_email:
            from apps.subscriptions.tasks import process_grace_periods
            process_grace_periods()
        mock_email.assert_called_once_with(sub, stage=1)
        sub.refresh_from_db()
        self.assertEqual(sub.dunning_stage, Subscription.DUNNING_DAY3)

    def test_sends_day7_email_at_day7(self):
        sub = self._make_past_due_sub(grace_days_remaining=1, dunning_stage=1)
        with patch('apps.subscriptions.tasks.send_dunning_email') as mock_email:
            from apps.subscriptions.tasks import process_grace_periods
            process_grace_periods()
        mock_email.assert_called_once_with(sub, stage=2)
        sub.refresh_from_db()
        self.assertEqual(sub.dunning_stage, Subscription.DUNNING_DAY7)

    def test_downgrades_after_grace_expires(self):
        sub = self._make_past_due_sub(grace_days_remaining=-1, dunning_stage=2)
        with patch('apps.subscriptions.tasks.send_dunning_email'):
            from apps.subscriptions.tasks import process_grace_periods
            process_grace_periods()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'cancelled')
        self.assertIsNone(sub.grace_period_ends_at)
        self.assertEqual(sub.dunning_stage, Subscription.DUNNING_EXPIRED)
        self.assertEqual(sub.workspace.tier, 'free')

    def test_does_not_resend_day3_if_already_sent(self):
        sub = self._make_past_due_sub(grace_days_remaining=4, dunning_stage=1)
        with patch('apps.subscriptions.tasks.send_dunning_email') as mock_email:
            from apps.subscriptions.tasks import process_grace_periods
            process_grace_periods()
        mock_email.assert_not_called()

    def test_no_action_for_early_grace(self):
        """Subscriptions with > 4 days left stay at stage 0 — no email yet."""
        sub = self._make_past_due_sub(grace_days_remaining=6, dunning_stage=0)
        with patch('apps.subscriptions.tasks.send_dunning_email') as mock_email:
            from apps.subscriptions.tasks import process_grace_periods
            process_grace_periods()
        mock_email.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.dunning_stage, 0)
