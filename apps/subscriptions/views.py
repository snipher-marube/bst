"""
Subscription & Billing views.
Real Stripe integration is stubbed – replace the STRIPE_WEBHOOK_SECRET env var
and uncomment the signature-verification block when going live.
"""
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from apps.workspaces.models import Workspace
from .models import Plan, Subscription, StripeWebhookEvent, PLAN_LIMITS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mock Stripe checkout – in production swap this for stripe.checkout.Session
# ---------------------------------------------------------------------------

@login_required
def upgrade_plan(request, workspace_id, tier):
    """
    Mock upgrade endpoint. In production this would redirect to Stripe Checkout.
    For now it immediately applies the new plan limits.
    """
    try:
        workspace = Workspace.objects.get(id=workspace_id, owner=request.user)
    except Workspace.DoesNotExist:
        return JsonResponse({'error': 'Workspace not found or permission denied'}, status=404)

    plan, _ = Plan.objects.get_or_create(
        tier=tier,
        defaults={
            'name': tier.capitalize(),
            **PLAN_LIMITS.get(tier, PLAN_LIMITS['free']),
        }
    )

    sub, created = Subscription.objects.get_or_create(
        workspace=workspace,
        defaults={'plan': plan, 'status': 'active'},
    )
    if not created:
        sub.plan = plan
        sub.status = 'active'
        sub.updated_at = timezone.now()
        sub.save(update_fields=['plan', 'status', 'updated_at'])

    sub.apply_plan_limits()

    logger.info(f"Workspace {workspace.name} upgraded to {tier}")
    return JsonResponse({
        'success': True,
        'message': f'Workspace upgraded to {tier}',
        'limits': PLAN_LIMITS.get(tier, PLAN_LIMITS['free']),
    })


# ---------------------------------------------------------------------------
# Stripe Webhook handler (mock – does not verify signature in dev)
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')

    # ---- Signature verification (uncomment when Stripe keys are configured) ----
    # import stripe
    # try:
    #     event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    # except (ValueError, stripe.error.SignatureVerificationError) as e:
    #     return HttpResponse(status=400)

    try:
        event_data = json.loads(payload)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event_id = event_data.get('id', '')
    event_type = event_data.get('type', '')

    # Idempotency guard
    if StripeWebhookEvent.objects.filter(stripe_event_id=event_id).exists():
        return HttpResponse(status=200)

    webhook_event = StripeWebhookEvent.objects.create(
        stripe_event_id=event_id,
        event_type=event_type,
        payload=event_data,
    )

    try:
        _process_stripe_event(event_type, event_data)
        webhook_event.processed = True
        webhook_event.save(update_fields=['processed'])
    except Exception as e:
        webhook_event.error = str(e)
        webhook_event.save(update_fields=['error'])
        logger.error(f"Stripe webhook processing failed: {e}")

    return HttpResponse(status=200)


def _process_stripe_event(event_type, event_data):
    """Route Stripe events to the appropriate handler."""
    data_obj = event_data.get('data', {}).get('object', {})

    if event_type == 'customer.subscription.updated':
        _handle_subscription_updated(data_obj)
    elif event_type == 'customer.subscription.deleted':
        _handle_subscription_deleted(data_obj)
    elif event_type in ('invoice.paid', 'invoice.payment_succeeded'):
        _handle_invoice_paid(data_obj)
    elif event_type == 'invoice.payment_failed':
        _handle_invoice_failed(data_obj)
    else:
        logger.debug(f"Unhandled Stripe event: {event_type}")


def _handle_subscription_updated(data_obj):
    stripe_sub_id = data_obj.get('id', '')
    status = data_obj.get('status', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status = status
        sub.save(update_fields=['status', 'updated_at'])
        logger.info(f"Subscription {stripe_sub_id} status → {status}")
    except Subscription.DoesNotExist:
        logger.warning(f"No local subscription for Stripe ID {stripe_sub_id}")


def _handle_subscription_deleted(data_obj):
    stripe_sub_id = data_obj.get('id', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status = 'cancelled'
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=['status', 'cancelled_at'])
        # Downgrade to free
        free_plan, _ = Plan.objects.get_or_create(
            tier='free',
            defaults={'name': 'Free', **PLAN_LIMITS['free']},
        )
        sub.plan = free_plan
        sub.save(update_fields=['plan'])
        sub.apply_plan_limits()
    except Subscription.DoesNotExist:
        logger.warning(f"No local subscription for Stripe ID {stripe_sub_id}")


def _handle_invoice_paid(data_obj):
    customer_id = data_obj.get('customer', '')
    try:
        sub = Subscription.objects.get(stripe_customer_id=customer_id)
        sub.status = 'active'
        sub.save(update_fields=['status'])
    except Subscription.DoesNotExist:
        pass


def _handle_invoice_failed(data_obj):
    customer_id = data_obj.get('customer', '')
    try:
        sub = Subscription.objects.get(stripe_customer_id=customer_id)
        sub.status = 'past_due'
        sub.save(update_fields=['status'])
    except Subscription.DoesNotExist:
        pass
