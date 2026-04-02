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
from django_ratelimit.decorators import ratelimit

from apps.workspaces.models import Workspace
from .models import Plan, Subscription, StripeWebhookEvent, MpesaTransaction, PLAN_LIMITS
from .mpesa_service import MpesaService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safaricom callback IP whitelist (production only)
# Source: Safaricom Daraja developer portal
# ---------------------------------------------------------------------------
_SAFARICOM_PRODUCTION_IPS = frozenset({
    '196.201.214.200', '196.201.214.206', '196.201.213.114',
    '196.201.214.207', '196.201.214.208', '196.201.213.44',
    '196.201.212.127', '196.201.212.138', '196.201.212.129',
    '196.201.212.136', '196.201.212.74',  '196.201.212.69',
})

# Pending transactions older than this are automatically expired on status poll
_PENDING_TIMEOUT_MINUTES = 10


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

    logger.info("Workspace %s upgraded to %s", workspace.name, tier)
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
        logger.error("Stripe webhook processing failed: %s", e)

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
        logger.debug("Unhandled Stripe event: %s", event_type)


def _handle_subscription_updated(data_obj):
    stripe_sub_id = data_obj.get('id', '')
    status = data_obj.get('status', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status = status
        sub.save(update_fields=['status', 'updated_at'])
        logger.info("Subscription %s status → %s", stripe_sub_id, status)
    except Subscription.DoesNotExist:
        logger.warning("No local subscription for Stripe ID %s", stripe_sub_id)


def _handle_subscription_deleted(data_obj):
    stripe_sub_id = data_obj.get('id', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status = 'cancelled'
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=['status', 'cancelled_at'])
        free_plan, _ = Plan.objects.get_or_create(
            tier='free',
            defaults={'name': 'Free', **PLAN_LIMITS['free']},
        )
        sub.plan = free_plan
        sub.save(update_fields=['plan'])
        sub.apply_plan_limits()
    except Subscription.DoesNotExist:
        logger.warning("No local subscription for Stripe ID %s", stripe_sub_id)


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
# Shared helper: apply a completed M-Pesa payment to the workspace
# ---------------------------------------------------------------------------

def _apply_plan_upgrade(txn: MpesaTransaction) -> None:
    """
    Called after a payment is confirmed (from callback OR status poll).
    Idempotent: safe to call more than once for the same transaction.
    """
    if not txn.plan or not txn.workspace:
        return
    sub, _ = Subscription.objects.get_or_create(
        workspace=txn.workspace,
        defaults={'plan': txn.plan, 'status': 'active'},
    )
    sub.plan = txn.plan
    sub.status = 'active'
    sub.save(update_fields=['plan', 'status'])
    sub.apply_plan_limits()
    logger.info(
        "Workspace %s upgraded to %s via M-Pesa (receipt=%s)",
        txn.workspace.name, txn.plan.tier, txn.mpesa_receipt_number or 'N/A',
    )


# ---------------------------------------------------------------------------
# M-Pesa STK Push – initiate payment
# ---------------------------------------------------------------------------

@login_required
@require_POST
@ratelimit(key='user', rate='5/10m', method='POST', block=True)
def mpesa_stk_push(request):
    """
    Initiate an M-Pesa STK Push for a plan upgrade.

    Rate-limited: 5 requests per user per 10 minutes.

    Expected POST body (JSON):
        { "workspace_id": "<uuid>", "tier": "starter|professional|enterprise", "phone": "07XXXXXXXX" }
    """
    # Reject oversized bodies (guard against payload abuse)
    if len(request.body) > 512:
        return JsonResponse({'error': 'Request body too large.'}, status=400)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    workspace_id = body.get('workspace_id', '')
    tier = body.get('tier', '').lower().strip()
    raw_phone = body.get('phone', '').strip()

    if tier not in MPESA_PLAN_PRICES:
        return JsonResponse({'error': 'Invalid plan tier.'}, status=400)

    if not raw_phone:
        return JsonResponse({'error': 'Phone number is required.'}, status=400)

    # Validate and normalise phone number before hitting Daraja
    service = MpesaService()
    try:
        normalised_phone = service.normalize_phone(raw_phone)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    try:
        workspace = Workspace.objects.get(id=workspace_id, owner=request.user)
    except Workspace.DoesNotExist:
        return JsonResponse({'error': 'Workspace not found.'}, status=404)

    # Idempotency: cancel any existing pending transaction for this workspace + tier
    # before creating a new one, so we don't accumulate ghost transactions.
    MpesaTransaction.objects.filter(
        workspace=workspace,
        plan__tier=tier,
        status='pending',
    ).update(status='cancelled', result_desc='Superseded by a new payment request.')

    # In sandbox/dev, Safaricom only processes KES 1 reliably.
    raw_amount = MPESA_PLAN_PRICES[tier]
    amount = 1 if getattr(settings, 'MPESA_SANDBOX', True) else int(raw_amount)

    plan, _ = Plan.objects.get_or_create(
        tier=tier,
        defaults={'name': tier.capitalize(), **PLAN_LIMITS.get(tier, PLAN_LIMITS['free'])},
    )

    # Create a pending transaction record (normalised phone stored)
    txn = MpesaTransaction.objects.create(
        workspace=workspace,
        user=request.user,
        plan=plan,
        phone_number=normalised_phone,
        amount=amount,
        status='pending',
    )

    try:
        result = service.lipa_na_mpesa_online(
            phone_number=normalised_phone,
            amount=amount,
            account_reference='AnalyticsMeta',
            transaction_desc='Plan Upgrade',
        )
    except ValueError as exc:
        # Phone validation inside service (shouldn't reach here after view validation)
        txn.status = 'failed'
        txn.result_desc = str(exc)
        txn.save(update_fields=['status', 'result_desc'])
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception as exc:
        txn.status = 'failed'
        txn.result_desc = 'STK Push network error.'
        txn.save(update_fields=['status', 'result_desc'])
        logger.error("STK Push failed for workspace %s: %s", workspace_id, exc)
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

    Security:
    - In production, only IPs from the known Safaricom range are accepted.
    - Body is size-limited to prevent memory abuse.
    """
    # ---- IP whitelist (production only; sandbox IPs are not fixed) ----
    if not getattr(settings, 'MPESA_SANDBOX', True):
        client_ip = (
            request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', '')
        )
        if client_ip not in _SAFARICOM_PRODUCTION_IPS:
            logger.warning("M-Pesa callback rejected from unlisted IP: %s", client_ip)
            return HttpResponse(status=403)

    # ---- Body size guard ----
    if len(request.body) > 4096:
        logger.warning("M-Pesa callback body too large (%d bytes), rejecting.", len(request.body))
        return HttpResponse(status=400)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    callback = body.get('Body', {}).get('stkCallback', {})
    checkout_request_id = callback.get('CheckoutRequestID', '')
    result_code = str(callback.get('ResultCode', ''))
    result_desc = callback.get('ResultDesc', '')

    if not checkout_request_id:
        logger.warning("M-Pesa callback missing CheckoutRequestID.")
        return HttpResponse(status=200)

    try:
        txn = MpesaTransaction.objects.get(checkout_request_id=checkout_request_id)
    except MpesaTransaction.DoesNotExist:
        logger.warning("M-Pesa callback for unknown CheckoutRequestID: %s", checkout_request_id)
        return HttpResponse(status=200)

    # Idempotency: ignore if already settled
    if txn.status in ('completed', 'failed', 'cancelled'):
        return HttpResponse(status=200)

    txn.result_code = result_code
    txn.result_desc = result_desc

    if result_code == '0':
        items = callback.get('CallbackMetadata', {}).get('Item', [])
        receipt = next(
            (i.get('Value', '') for i in items if i.get('Name') == 'MpesaReceiptNumber'),
            '',
        )
        txn.mpesa_receipt_number = receipt
        txn.status = 'completed'
        txn.save(update_fields=['result_code', 'result_desc', 'mpesa_receipt_number', 'status'])
        _apply_plan_upgrade(txn)
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

    Authorization: transaction must belong to the requesting user AND their
    current workspace (prevents IDOR via guessed checkout_request_id).
    """
    workspace = getattr(request.user, 'current_workspace', None)

    try:
        txn = MpesaTransaction.objects.get(
            checkout_request_id=checkout_request_id,
            user=request.user,
            workspace=workspace,
        )
    except MpesaTransaction.DoesNotExist:
        return JsonResponse({'error': 'Transaction not found.'}, status=404)

    # Auto-expire stale pending transactions (user never approved on phone)
    if txn.status == 'pending':
        age_minutes = (timezone.now() - txn.created_at).total_seconds() / 60
        if age_minutes > _PENDING_TIMEOUT_MINUTES:
            txn.status = 'failed'
            txn.result_desc = 'Payment timed out. Please try again.'
            txn.save(update_fields=['status', 'result_desc'])
            return JsonResponse({
                'status': 'failed',
                'result_desc': txn.result_desc,
                'mpesa_receipt_number': '',
                'plan': txn.plan.tier if txn.plan else '',
                'amount': str(txn.amount),
            })

    data = {
        'status': txn.status,
        'result_desc': txn.result_desc,
        'mpesa_receipt_number': txn.mpesa_receipt_number,
        'plan': txn.plan.tier if txn.plan else '',
        'amount': str(txn.amount),
    }

    # If still pending, query Daraja for live status
    if txn.status == 'pending' and txn.checkout_request_id:
        service = MpesaService()
        try:
            result = service.query_stk_push(txn.checkout_request_id)
            # ResponseCode = API-level success; ResultCode = payment outcome.
            api_response_code = str(result.get('ResponseCode', ''))
            if api_response_code != '0':
                pass  # Daraja API error — keep polling
            else:
                daraja_result_code = str(result.get('ResultCode', ''))
                result_desc = result.get('ResultDesc', '')

                # Codes / phrases that mean "not yet settled — keep polling"
                PENDING_CODES = {
                    '',       # not yet available
                    '1032',   # request in queue / user hasn't responded
                    '1',      # still being processed by Safaricom
                }
                still_processing = 'still under processing' in result_desc.lower()

                if daraja_result_code == '0':
                    # Extract receipt from query response if available
                    items = result.get('CallbackMetadata', {}).get('Item', [])
                    receipt = next(
                        (i.get('Value', '') for i in items if i.get('Name') == 'MpesaReceiptNumber'),
                        '',
                    )
                    txn.status = 'completed'
                    txn.result_code = daraja_result_code
                    txn.result_desc = result_desc
                    txn.mpesa_receipt_number = receipt
                    txn.save(update_fields=['status', 'result_code', 'result_desc', 'mpesa_receipt_number'])
                    _apply_plan_upgrade(txn)
                    data['status'] = 'completed'
                    data['mpesa_receipt_number'] = receipt
                elif daraja_result_code in PENDING_CODES or still_processing:
                    pass  # Still pending — keep polling
                else:
                    txn.status = 'failed'
                    txn.result_code = daraja_result_code
                    txn.result_desc = result_desc
                    txn.save(update_fields=['status', 'result_code', 'result_desc'])
                    data['status'] = 'failed'
                    data['result_desc'] = result_desc
        except Exception:
            pass  # Transient Daraja error — keep polling silently

    return JsonResponse(data)
