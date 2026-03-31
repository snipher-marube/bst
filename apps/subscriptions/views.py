"""
Subscription & Billing views.
Includes M-Pesa Daraja API STK Push and Stripe (stubbed).
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
from .models import Plan, Subscription, StripeWebhookEvent, MpesaTransaction, PLAN_LIMITS
from .mpesa_service import MpesaService

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


# ---------------------------------------------------------------------------
# M-Pesa – KES price map
# ---------------------------------------------------------------------------

MPESA_PLAN_PRICES = {
    'starter':      2500,
    'professional': 6500,
    'enterprise':   12900,
}


# ---------------------------------------------------------------------------
# M-Pesa STK Push – initiate payment
# ---------------------------------------------------------------------------

@login_required
@require_POST
def mpesa_stk_push(request):
    """
    Initiate an M-Pesa STK Push for a plan upgrade.

    Expected POST body (JSON):
        { "workspace_id": "<uuid>", "tier": "starter|professional|enterprise", "phone": "07XXXXXXXX" }
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    workspace_id = body.get('workspace_id', '')
    tier = body.get('tier', '').lower()
    phone = body.get('phone', '').strip()

    if tier not in MPESA_PLAN_PRICES:
        return JsonResponse({'error': 'Invalid plan tier.'}, status=400)

    if not phone:
        return JsonResponse({'error': 'Phone number is required.'}, status=400)

    try:
        workspace = Workspace.objects.get(id=workspace_id, owner=request.user)
    except Workspace.DoesNotExist:
        return JsonResponse({'error': 'Workspace not found.'}, status=404)

    amount = MPESA_PLAN_PRICES[tier]
    plan, _ = Plan.objects.get_or_create(
        tier=tier,
        defaults={'name': tier.capitalize(), **PLAN_LIMITS.get(tier, PLAN_LIMITS['free'])},
    )

    # Create a pending transaction record first
    txn = MpesaTransaction.objects.create(
        workspace=workspace,
        user=request.user,
        plan=plan,
        phone_number=phone,
        amount=amount,
        status='pending',
    )

    service = MpesaService()
    try:
        result = service.lipa_na_mpesa_online(
            phone_number=phone,
            amount=amount,
            account_reference='AnalyticsMeta',
            transaction_desc='Plan Upgrade',
        )
    except Exception as exc:
        txn.status = 'failed'
        txn.result_desc = str(exc)
        txn.save(update_fields=['status', 'result_desc'])
        logger.error("STK Push failed for workspace %s: %s", workspace.name, exc)
        return JsonResponse({'error': 'M-Pesa request failed. Please try again.'}, status=502)

    response_code = result.get('ResponseCode', '')
    if response_code != '0':
        txn.status = 'failed'
        txn.result_desc = result.get('ResponseDescription', '')
        txn.save(update_fields=['status', 'result_desc'])
        return JsonResponse(
            {'error': result.get('ResponseDescription', 'STK Push rejected.')},
            status=400,
        )

    # Save Daraja identifiers for later callback matching
    txn.merchant_request_id = result.get('MerchantRequestID', '')
    txn.checkout_request_id = result.get('CheckoutRequestID', '')
    txn.save(update_fields=['merchant_request_id', 'checkout_request_id'])

    return JsonResponse({
        'success': True,
        'checkout_request_id': txn.checkout_request_id,
        'customer_message': result.get('CustomerMessage', 'STK Push sent. Check your phone.'),
    })


# ---------------------------------------------------------------------------
# M-Pesa Callback – Safaricom posts payment result here
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
def mpesa_callback(request):
    """
    Daraja sends a POST to this endpoint with the payment result.
    The URL must be publicly reachable (use ngrok during development).
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    callback = body.get('Body', {}).get('stkCallback', {})
    merchant_request_id = callback.get('MerchantRequestID', '')
    checkout_request_id = callback.get('CheckoutRequestID', '')
    result_code = str(callback.get('ResultCode', ''))
    result_desc = callback.get('ResultDesc', '')

    try:
        txn = MpesaTransaction.objects.get(checkout_request_id=checkout_request_id)
    except MpesaTransaction.DoesNotExist:
        logger.warning("M-Pesa callback for unknown CheckoutRequestID: %s", checkout_request_id)
        return HttpResponse(status=200)

    txn.result_code = result_code
    txn.result_desc = result_desc

    if result_code == '0':
        # Payment successful – extract M-Pesa receipt number
        items = callback.get('CallbackMetadata', {}).get('Item', [])
        receipt = next((i['Value'] for i in items if i.get('Name') == 'MpesaReceiptNumber'), '')
        txn.mpesa_receipt_number = receipt
        txn.status = 'completed'
        txn.save(update_fields=['result_code', 'result_desc', 'mpesa_receipt_number', 'status'])

        # Apply the plan limits to the workspace
        if txn.plan and txn.workspace:
            sub, _ = Subscription.objects.get_or_create(
                workspace=txn.workspace,
                defaults={'plan': txn.plan, 'status': 'active'},
            )
            sub.plan = txn.plan
            sub.status = 'active'
            sub.save(update_fields=['plan', 'status'])
            sub.apply_plan_limits()
            logger.info(
                "Workspace %s upgraded to %s via M-Pesa (%s)",
                txn.workspace.name, txn.plan.tier, receipt,
            )
    else:
        txn.status = 'failed'
        txn.save(update_fields=['result_code', 'result_desc', 'status'])
        logger.info("M-Pesa payment failed for %s: %s", checkout_request_id, result_desc)

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# M-Pesa payment status – frontend polls this
# ---------------------------------------------------------------------------

@login_required
def mpesa_payment_status(request, checkout_request_id):
    """
    Return the current status of an M-Pesa transaction.
    Frontend polls every few seconds after initiating STK Push.
    """
    try:
        txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_request_id,
            user=request.user,
        )
    except MpesaTransaction.DoesNotExist:
        return JsonResponse({'error': 'Transaction not found.'}, status=404)

    data = {
        'status': txn.status,
        'result_desc': txn.result_desc,
        'mpesa_receipt_number': txn.mpesa_receipt_number,
        'plan': txn.plan.tier if txn.plan else '',
        'amount': str(txn.amount),
    }

    # If still pending, optionally query Daraja for live status
    if txn.status == 'pending' and txn.checkout_request_id:
        service = MpesaService()
        try:
            result = service.query_stk_push(txn.checkout_request_id)
            daraja_result_code = str(result.get('ResultCode', ''))
            if daraja_result_code == '0':
                txn.status = 'completed'
                txn.result_code = daraja_result_code
                txn.result_desc = result.get('ResultDesc', '')
                txn.save(update_fields=['status', 'result_code', 'result_desc'])
                # Apply plan
                if txn.plan and txn.workspace:
                    sub, _ = Subscription.objects.get_or_create(
                        workspace=txn.workspace,
                        defaults={'plan': txn.plan, 'status': 'active'},
                    )
                    sub.plan = txn.plan
                    sub.status = 'active'
                    sub.save(update_fields=['plan', 'status'])
                    sub.apply_plan_limits()
                data['status'] = 'completed'
            elif daraja_result_code not in ('', '1032'):  # 1032 = request cancelled / still pending
                txn.status = 'failed'
                txn.result_code = daraja_result_code
                txn.result_desc = result.get('ResultDesc', '')
                txn.save(update_fields=['status', 'result_code', 'result_desc'])
                data['status'] = 'failed'
                data['result_desc'] = txn.result_desc
        except Exception:
            pass  # Don't fail the poll on transient Daraja errors

    return JsonResponse(data)
