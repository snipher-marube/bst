"""
apps/subscriptions/views.py
============================
HTTP view layer for the AnalyticsMeta subscription and billing system.

Architecture overview
---------------------
Payments flow through three asynchronous stages, each handled by a separate
view in this module:

::

    ┌─────────────────────────────────────────────────────────────────┐
    │  1. mpesa_stk_push  (POST /subscriptions/mpesa/stk-push/)       │
    │     • Validates request, phone number, workspace ownership       │
    │     • Cancels any superseded pending transaction (idempotency)   │
    │     • Creates MpesaTransaction(status='pending')                 │
    │     • Calls MpesaService.lipa_na_mpesa_online() → Daraja        │
    │     • Returns { checkout_request_id, customer_message }          │
    └────────────────────────────┬────────────────────────────────────┘
                                 │  STK Push sent to phone
                                 ▼
                    ┌────────────┴────────────┐
                    │  Customer enters PIN    │
                    └────────────┬────────────┘
                                 │
               ┌─────────────────┴──────────────────┐
               │                                     │
               ▼                                     ▼
    ┌──────────────────────────┐     ┌───────────────────────────────┐
    │  2. mpesa_callback        │     │  3. mpesa_payment_status      │
    │  (POST /mpesa/callback/)  │     │  (GET /mpesa/status/<id>/)    │
    │                           │     │                               │
    │  Safaricom posts result   │     │  Frontend polls this every    │
    │  here asynchronously.     │     │  4 s until settled.           │
    │                           │     │                               │
    │  • IP-whitelisted (prod)  │     │  • Also queries Daraja live   │
    │  • Idempotent (skips if   │     │    when callback hasn't       │
    │    already settled)       │     │    arrived yet.               │
    │  • Calls                  │     │  • Auto-expires transactions  │
    │    _apply_plan_upgrade()  │     │    pending > 10 minutes.      │
    └──────────────────────────┘     └───────────────────────────────┘

Both paths converge on ``_apply_plan_upgrade(txn)`` which is idempotent and
safe to call from either view without risk of double-upgrading.

Sandbox vs production
---------------------
When ``settings.MPESA_SANDBOX is True`` (the default), the amount charged
is always **KES 1** regardless of plan price.  This prevents spending real
money against the Safaricom sandbox, which only reliably processes KES 1.
The production amount is restored automatically when ``MPESA_SANDBOX=False``.

A yellow sandbox notice banner is also rendered on the billing page (the
``debug`` context variable passed by ``BillingView`` drives this).

Security measures
-----------------
+-------------------------------+------------------------------------------+
| Measure                       | Where applied                            |
+===============================+==========================================+
| ``@login_required``           | STK Push, status poll                    |
+-------------------------------+------------------------------------------+
| ``@ratelimit(5/10m)``         | STK Push — prevents payment spam         |
+-------------------------------+------------------------------------------+
| Body size guard (512 / 4096 B)| STK Push and callback                    |
+-------------------------------+------------------------------------------+
| Phone number validation       | STK Push — before creating DB record     |
+-------------------------------+------------------------------------------+
| Workspace owner check         | STK Push — ``owner=request.user``        |
+-------------------------------+------------------------------------------+
| Safaricom IP whitelist        | Callback — production only               |
+-------------------------------+------------------------------------------+
| Idempotency guard             | Callback — skips already-settled txns    |
+-------------------------------+------------------------------------------+
| IDOR prevention               | Status poll — ``user + workspace`` check |
+-------------------------------+------------------------------------------+
| Stale transaction auto-expiry | Status poll — > 10 min → failed          |
+-------------------------------+------------------------------------------+

Stripe
------
The Stripe views (``upgrade_plan``, ``stripe_webhook``) are partially
scaffolded.  ``upgrade_plan`` applies plan limits immediately without payment
(mock behaviour).  ``stripe_webhook`` stores and routes events but signature
verification is commented out until real Stripe keys are configured.

Module-level constants
----------------------
``MPESA_PLAN_PRICES``
    Canonical KES prices per tier.  Imported by the public pricing view to
    keep prices consistent across the billing and marketing pages.

``_SAFARICOM_PRODUCTION_IPS``
    Frozenset of known Safaricom callback server IPs.  Requests from any other
    IP are rejected with 403 in production.

``_PENDING_TIMEOUT_MINUTES``
    How long a pending transaction is allowed to remain before it is
    automatically expired on the next status poll.  Currently 10 minutes.
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

#: Known Safaricom Daraja callback server IPs.
#:
#: In production (``MPESA_SANDBOX=False``) the callback endpoint rejects any
#: POST that does not originate from one of these addresses.  Sandbox IPs are
#: not fixed so the whitelist is disabled when ``MPESA_SANDBOX=True``.
#:
#: Review against the Safaricom Daraja portal if callbacks are being rejected
#: unexpectedly — Safaricom occasionally adds new egress IPs.
_SAFARICOM_PRODUCTION_IPS = frozenset({
    '196.201.214.200', '196.201.214.206', '196.201.213.114',
    '196.201.214.207', '196.201.214.208', '196.201.213.44',
    '196.201.212.127', '196.201.212.138', '196.201.212.129',
    '196.201.212.136', '196.201.212.74',  '196.201.212.69',
})

#: Pending transactions older than this many minutes are automatically failed
#: on the next status poll.  Prevents permanently-stuck ``'pending'`` rows
#: when a user dismisses the STK prompt without responding.
_PENDING_TIMEOUT_MINUTES = 10


# ===========================================================================
# Mock Stripe checkout
# ===========================================================================

@login_required
def upgrade_plan(request, workspace_id, tier):
    """
    Mock plan-upgrade endpoint.

    .. note::
        This is a **development stub**.  In production, replace with a redirect
        to a ``stripe.checkout.Session`` so the user pays before the plan is
        applied.

    The endpoint immediately applies the requested tier's limits to the
    workspace without requiring payment.  It is used for:

    * Internal testing of plan-limit enforcement.
    * Admin-initiated plan upgrades (e.g. granting a free trial).

    Parameters
    ----------
    workspace_id : UUID
        The primary key of the workspace to upgrade (from URL).
    tier : str
        Target plan tier — one of ``free``, ``starter``, ``professional``,
        ``enterprise`` (from URL).

    Authorization
    -------------
    Only the workspace **owner** (``owner=request.user``) can trigger an
    upgrade.  Members with other roles receive a 404 (to avoid leaking whether
    the workspace exists).

    Returns
    -------
    JsonResponse
        ``200`` with ``{ success, message, limits }`` on success.
        ``404`` if the workspace is not found or the user is not the owner.
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


# ===========================================================================
# Stripe webhook
# ===========================================================================

@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Receive and process inbound Stripe webhook events.

    .. note::
        Stripe signature verification is **commented out** until real Stripe
        keys are configured.  Uncomment the ``stripe.Webhook.construct_event``
        block and add ``STRIPE_WEBHOOK_SECRET`` to the environment before going
        live with Stripe.

    Flow
    ----
    1. Parse raw JSON body from Stripe.
    2. Check ``StripeWebhookEvent`` table for duplicate ``stripe_event_id``
       (idempotency guard — Stripe retries failed deliveries).
    3. Persist the raw payload as a new ``StripeWebhookEvent`` row so it can
       be replayed if processing fails.
    4. Route to the appropriate private handler via ``_process_stripe_event``.
    5. Mark the event ``processed=True`` on success, or store the error string
       on failure (the event remains ``processed=False`` for manual retry).

    Always returns HTTP 200 so Stripe stops retrying — processing errors are
    captured in the database, not surfaced as HTTP errors.

    Supported event types
    ---------------------
    * ``customer.subscription.updated``   → ``_handle_subscription_updated``
    * ``customer.subscription.deleted``   → ``_handle_subscription_deleted``
    * ``invoice.paid``                    → ``_handle_invoice_paid``
    * ``invoice.payment_succeeded``       → ``_handle_invoice_paid``
    * ``invoice.payment_failed``          → ``_handle_invoice_failed``
    * All others                          → logged at DEBUG, ignored.
    """
    payload        = request.body
    webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')  # noqa: F841

    # ---- Signature verification (uncomment when Stripe keys are configured) ----
    # import stripe
    # sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    # try:
    #     event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    # except (ValueError, stripe.error.SignatureVerificationError):
    #     return HttpResponse(status=400)

    try:
        event_data = json.loads(payload)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event_id   = event_data.get('id', '')
    event_type = event_data.get('type', '')

    # Idempotency: Stripe can deliver the same event more than once.
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


def _process_stripe_event(event_type: str, event_data: dict) -> None:
    """
    Dispatch a Stripe event to the correct handler function.

    Parameters
    ----------
    event_type
        The Stripe event name (e.g. ``'customer.subscription.updated'``).
    event_data
        Full parsed JSON payload from Stripe.
    """
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


def _handle_subscription_updated(data_obj: dict) -> None:
    """
    Sync the local ``Subscription.status`` when Stripe reports a change.

    Stripe fires this event whenever a subscription transitions between states
    (e.g. ``trialing → active``, ``active → past_due``).  We mirror the status
    string directly — Stripe status values map to our ``STATUS_CHOICES``.

    Logs a warning if no local subscription matches the Stripe ID (can happen
    if the subscription was created outside of this application, e.g. manually
    via the Stripe dashboard).
    """
    stripe_sub_id = data_obj.get('id', '')
    status        = data_obj.get('status', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status = status
        sub.save(update_fields=['status', 'updated_at'])
        logger.info("Subscription %s status → %s", stripe_sub_id, status)
    except Subscription.DoesNotExist:
        logger.warning("No local subscription for Stripe ID %s", stripe_sub_id)


def _handle_subscription_deleted(data_obj: dict) -> None:
    """
    Downgrade a workspace to the free plan when its Stripe subscription ends.

    Called on ``customer.subscription.deleted`` — typically when a subscription
    is cancelled (either by the customer or by Stripe after failed payment
    recovery).

    Behaviour
    ---------
    1. Marks the local ``Subscription`` as ``'cancelled'`` with a timestamp.
    2. Upserts the ``'free'`` ``Plan`` row.
    3. Switches the subscription to the free plan and calls
       ``apply_plan_limits()`` so the workspace is immediately restricted.
    """
    stripe_sub_id = data_obj.get('id', '')
    try:
        sub = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
        sub.status      = 'cancelled'
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


def _handle_invoice_paid(data_obj: dict) -> None:
    """
    Restore a subscription to ``'active'`` after a successful invoice payment.

    Relevant after recovering from ``'past_due'`` — Stripe fires
    ``invoice.paid`` / ``invoice.payment_succeeded`` when a previously failed
    charge eventually succeeds.

    Silently ignores unknown ``customer`` IDs (e.g. old Stripe test data).
    """
    customer_id = data_obj.get('customer', '')
    try:
        sub = Subscription.objects.get(stripe_customer_id=customer_id)
        sub.status = 'active'
        sub.save(update_fields=['status'])
    except Subscription.DoesNotExist:
        pass


def _handle_invoice_failed(data_obj: dict) -> None:
    """
    Mark a subscription ``'past_due'`` when a Stripe invoice payment fails.

    Stripe retries failed invoices on a configurable schedule.  The workspace
    remains accessible while ``'past_due'`` — you may want to add a dunning
    banner to the dashboard template to prompt the user to update their card.

    Silently ignores unknown ``customer`` IDs.
    """
    customer_id = data_obj.get('customer', '')
    try:
        sub = Subscription.objects.get(stripe_customer_id=customer_id)
        sub.status = 'past_due'
        sub.save(update_fields=['status'])
    except Subscription.DoesNotExist:
        pass


# ===========================================================================
# M-Pesa — price map
# ===========================================================================

#: Canonical M-Pesa plan prices in KES.
#:
#: Used by ``mpesa_stk_push`` to determine the amount to charge.
#: Also imported by ``apps.core.views.PricingView`` so the public pricing page
#: always shows the same numbers as the billing page — a single source of truth.
#:
#: In sandbox mode (``MPESA_SANDBOX=True``) the view overrides these to KES 1.
MPESA_PLAN_PRICES: dict[str, int] = {
    'starter':      2500,
    'professional': 6500,
    'enterprise':   12900,
}


# ===========================================================================
# Shared post-payment helper
# ===========================================================================

def _apply_plan_upgrade(txn: MpesaTransaction) -> None:
    """
    Activate the plan associated with a completed M-Pesa transaction.

    This function is the **single place** where a confirmed payment is
    translated into an active subscription.  It is called from:

    * ``mpesa_callback`` — when Safaricom's server posts a success result.
    * ``mpesa_payment_status`` — when the status-poll query to Daraja confirms
      success (used when the callback URL is not reachable, e.g. in development
      without ngrok).

    Idempotency
    -----------
    Safe to call multiple times for the same transaction.
    ``Subscription.get_or_create`` ensures no duplicate rows are created, and
    ``apply_plan_limits()`` is a pure write that overwrites the workspace
    columns with the same values on repeated calls.

    Parameters
    ----------
    txn
        The ``MpesaTransaction`` whose ``status`` has just been set to
        ``'completed'``.  Must have non-null ``plan`` and ``workspace``
        references; the function exits silently if either is missing.

    Side effects
    ------------
    * Upserts a ``Subscription`` row (``status='active'``, ``plan=txn.plan``).
    * Calls ``Subscription.apply_plan_limits()`` which updates four columns on
      the ``Workspace`` row (``max_tables``, ``max_records_per_table``,
      ``max_team_members``, ``tier``).
    * Logs an INFO message with workspace name, tier, and M-Pesa receipt number.
    """
    if not txn.plan or not txn.workspace:
        return

    sub, _ = Subscription.objects.get_or_create(
        workspace=txn.workspace,
        defaults={'plan': txn.plan, 'status': 'active'},
    )
    sub.plan   = txn.plan
    sub.status = 'active'
    sub.save(update_fields=['plan', 'status'])
    sub.apply_plan_limits()

    logger.info(
        "Workspace %s upgraded to %s via M-Pesa (receipt=%s)",
        txn.workspace.name, txn.plan.tier, txn.mpesa_receipt_number or 'N/A',
    )


# ===========================================================================
# M-Pesa STK Push — initiate payment
# ===========================================================================

@login_required
@require_POST
@ratelimit(key='user', rate='5/10m', method='POST', block=True)
def mpesa_stk_push(request):
    """
    Initiate an M-Pesa STK Push payment for a workspace plan upgrade.

    This is the entry point for the M-Pesa payment flow.  The frontend JS
    calls this endpoint when the user clicks "Pay with M-Pesa", then begins
    polling ``mpesa_payment_status`` every 4 seconds.

    Rate limiting
    -------------
    Limited to **5 requests per user per 10 minutes** via ``django-ratelimit``.
    Exceeding the limit returns HTTP 429.  This prevents:

    * Accidental double-submission (user clicking the button repeatedly).
    * Intentional payment spam that would trigger many Safaricom STK prompts.

    Request
    -------
    ``POST /subscriptions/mpesa/stk-push/``

    Headers: ``Content-Type: application/json``, ``X-CSRFToken: <token>``

    Body (JSON, max 512 bytes)::

        {
            "workspace_id": "<uuid>",
            "tier":         "starter|professional|enterprise",
            "phone":        "07XXXXXXXX"
        }

    Validation
    ----------
    In order:

    1. Body ≤ 512 bytes.
    2. Valid JSON.
    3. ``tier`` is one of the keys in ``MPESA_PLAN_PRICES``.
    4. ``phone`` is non-empty.
    5. ``phone`` is a valid Kenyan number (via ``MpesaService.normalize_phone``).
    6. ``workspace`` exists and is owned by ``request.user``.

    Idempotency
    -----------
    Any existing ``'pending'`` transactions for the same workspace + tier are
    cancelled before the new transaction is created.  This prevents accumulating
    ghost rows when a user retries after dismissing the STK prompt.

    Amount
    ------
    ``MPESA_SANDBOX=True`` → KES 1 (safe for sandbox testing).
    ``MPESA_SANDBOX=False`` → the real plan price from ``MPESA_PLAN_PRICES``.

    The transaction row always records the *actual amount sent to Daraja*, not
    the display price.

    Response (success) — HTTP 200
    ------------------------------
    ::

        {
            "success":             true,
            "checkout_request_id": "ws_CO_...",
            "customer_message":    "Success. Request accepted..."
        }

    Error responses
    ---------------
    +--------+-------------------------------------------------------+
    | Status | Reason                                                |
    +========+=======================================================+
    | 400    | Body too large / invalid JSON / bad tier / bad phone  |
    | 404    | Workspace not found or caller is not the owner        |
    | 400    | Daraja rejected the STK Push (ResponseCode != '0')    |
    | 502    | Network error reaching Daraja                         |
    | 429    | Rate limit exceeded                                   |
    +--------+-------------------------------------------------------+
    """
    # Guard against payload abuse before parsing
    if len(request.body) > 512:
        return JsonResponse({'error': 'Request body too large.'}, status=400)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    workspace_id = body.get('workspace_id', '')
    tier         = body.get('tier', '').lower().strip()
    raw_phone    = body.get('phone', '').strip()

    if tier not in MPESA_PLAN_PRICES:
        return JsonResponse({'error': 'Invalid plan tier.'}, status=400)

    if not raw_phone:
        return JsonResponse({'error': 'Phone number is required.'}, status=400)

    # Validate and normalise before creating any DB records or hitting Daraja.
    service = MpesaService()
    try:
        normalised_phone = service.normalize_phone(raw_phone)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    try:
        workspace = Workspace.objects.get(id=workspace_id, owner=request.user)
    except Workspace.DoesNotExist:
        return JsonResponse({'error': 'Workspace not found.'}, status=404)

    # Cancel any ghost pending transactions for this workspace + tier so we
    # don't accumulate stale rows when the user retries.
    MpesaTransaction.objects.filter(
        workspace=workspace,
        plan__tier=tier,
        status='pending',
    ).update(status='cancelled', result_desc='Superseded by a new payment request.')

    # KES 1 in sandbox so we don't burn real money during testing.
    raw_amount = MPESA_PLAN_PRICES[tier]
    amount     = 1 if getattr(settings, 'MPESA_SANDBOX', True) else int(raw_amount)

    plan, _ = Plan.objects.get_or_create(
        tier=tier,
        defaults={'name': tier.capitalize(), **PLAN_LIMITS.get(tier, PLAN_LIMITS['free'])},
    )

    # Create the pending row *before* calling Daraja so we have a record even
    # if the network request fails.
    txn = MpesaTransaction.objects.create(
        workspace=workspace,
        user=request.user,
        plan=plan,
        phone_number=normalised_phone,   # always normalised form
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
        # Shouldn't reach here because we already validated above, but guard anyway.
        txn.status      = 'failed'
        txn.result_desc = str(exc)
        txn.save(update_fields=['status', 'result_desc'])
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception as exc:
        txn.status      = 'failed'
        txn.result_desc = 'STK Push network error.'
        txn.save(update_fields=['status', 'result_desc'])
        logger.error("STK Push failed for workspace %s: %s", workspace_id, exc)
        return JsonResponse({'error': 'M-Pesa request failed. Please try again.'}, status=502)

    # ResponseCode '0' = Daraja accepted the request (not that the user paid).
    response_code = result.get('ResponseCode', '')
    if response_code != '0':
        txn.status      = 'failed'
        txn.result_desc = result.get('ResponseDescription', '')
        txn.save(update_fields=['status', 'result_desc'])
        return JsonResponse(
            {'error': result.get('ResponseDescription', 'STK Push rejected.')},
            status=400,
        )

    # Persist Daraja identifiers used for callback matching and status polling.
    txn.merchant_request_id = result.get('MerchantRequestID', '')
    txn.checkout_request_id = result.get('CheckoutRequestID', '')
    txn.save(update_fields=['merchant_request_id', 'checkout_request_id'])

    return JsonResponse({
        'success':             True,
        'checkout_request_id': txn.checkout_request_id,
        'customer_message':    result.get('CustomerMessage', 'STK Push sent. Check your phone.'),
    })


# ===========================================================================
# M-Pesa callback — Safaricom posts result here
# ===========================================================================

@csrf_exempt
@require_POST
def mpesa_callback(request):
    """
    Receive the asynchronous payment result from Safaricom Daraja.

    Safaricom POSTs to this URL after the customer either confirms or rejects
    the STK prompt on their phone.  The URL **must be publicly reachable over
    HTTPS** — it cannot be ``localhost``.  During development, expose the local
    server via ngrok and set ``MPESA_CALLBACK_URL`` accordingly.

    CSRF
    ----
    ``@csrf_exempt`` is required because Safaricom does not include a Django
    CSRF token.  Security is instead provided by the IP whitelist (production)
    and the ``checkout_request_id`` lookup which acts as an implicit secret.

    Security
    --------
    **IP whitelist (production only)**
        When ``MPESA_SANDBOX=False``, the client IP is checked against
        ``_SAFARICOM_PRODUCTION_IPS``.  Requests from any other IP receive
        HTTP 403 and a WARNING log entry.  The IP is extracted from
        ``X-Forwarded-For`` (first entry) when the app runs behind a proxy,
        falling back to ``REMOTE_ADDR``.

    **Body size limit**
        The request body is rejected at 4096 bytes to prevent memory abuse
        from maliciously large payloads.

    **Idempotency**
        Already-settled transactions (``completed``, ``failed``, ``cancelled``)
        are silently ignored and receive HTTP 200 so Safaricom stops retrying.
        This is safe because ``_apply_plan_upgrade`` is idempotent anyway.

    Daraja callback payload structure
    ----------------------------------
    ::

        {
          "Body": {
            "stkCallback": {
              "MerchantRequestID":  "...",
              "CheckoutRequestID":  "ws_CO_...",
              "ResultCode":         0,
              "ResultDesc":         "The service request is processed successfully.",
              "CallbackMetadata": {
                "Item": [
                  { "Name": "Amount",             "Value": 1 },
                  { "Name": "MpesaReceiptNumber", "Value": "QHJ12ABC34" },
                  { "Name": "TransactionDate",    "Value": 20260402163231 },
                  { "Name": "PhoneNumber",         "Value": 254712345678 }
                ]
              }
            }
          }
        }

    ``ResultCode == 0``    → payment succeeded → extract receipt, mark
                             completed, call ``_apply_plan_upgrade``.
    ``ResultCode != 0``    → user cancelled or payment failed → mark failed.

    ``CallbackMetadata`` is absent when payment fails (``ResultCode != 0``).

    Always returns HTTP 200 — a non-200 response causes Safaricom to retry.
    """
    # --- IP whitelist (production only; sandbox IPs are not fixed) ----------
    if not getattr(settings, 'MPESA_SANDBOX', True):
        client_ip = (
            request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', '')
        )
        if client_ip not in _SAFARICOM_PRODUCTION_IPS:
            logger.warning("M-Pesa callback rejected from unlisted IP: %s", client_ip)
            return HttpResponse(status=403)

    # --- Body size guard -----------------------------------------------------
    if len(request.body) > 4096:
        logger.warning("M-Pesa callback body too large (%d bytes), rejecting.", len(request.body))
        return HttpResponse(status=400)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    callback          = body.get('Body', {}).get('stkCallback', {})
    checkout_request_id = callback.get('CheckoutRequestID', '')
    result_code       = str(callback.get('ResultCode', ''))
    result_desc       = callback.get('ResultDesc', '')

    if not checkout_request_id:
        logger.warning("M-Pesa callback missing CheckoutRequestID.")
        return HttpResponse(status=200)

    try:
        txn = MpesaTransaction.objects.get(checkout_request_id=checkout_request_id)
    except MpesaTransaction.DoesNotExist:
        logger.warning("M-Pesa callback for unknown CheckoutRequestID: %s", checkout_request_id)
        return HttpResponse(status=200)

    # Idempotency: if this transaction was already settled by a prior callback
    # delivery or by the status-poll path, acknowledge and return.
    if txn.status in ('completed', 'failed', 'cancelled'):
        return HttpResponse(status=200)

    txn.result_code = result_code
    txn.result_desc = result_desc

    if result_code == '0':
        # Extract the M-Pesa receipt number from the metadata items list.
        items   = callback.get('CallbackMetadata', {}).get('Item', [])
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


# ===========================================================================
# M-Pesa payment status — frontend polling endpoint
# ===========================================================================

@login_required
def mpesa_payment_status(request, checkout_request_id):
    """
    Return the current status of an M-Pesa transaction for frontend polling.

    The billing page JavaScript calls this endpoint every 4 seconds after
    initiating an STK Push, until the transaction reaches a terminal state
    (``completed`` or ``failed``) or the frontend times out after ~2 minutes
    (30 polls × 4 seconds).

    This endpoint also acts as a **fallback payment confirmer** for the case
    where the Daraja callback was never received (e.g. during development with
    no public callback URL, or due to Safaricom delivery failures).  When the
    transaction is still ``'pending'`` it actively queries Daraja and updates
    the transaction if a definitive result is available.

    Authorization
    -------------
    The transaction must satisfy **both** conditions:

    * ``user = request.user``           — the initiating user
    * ``workspace = user.current_workspace`` — the active workspace

    This prevents IDOR: a user who learns another user's ``checkout_request_id``
    (e.g. from a browser history leak) cannot check or claim that payment.

    Stale transaction expiry
    ------------------------
    If the transaction has been ``'pending'`` for more than
    ``_PENDING_TIMEOUT_MINUTES`` (10 min) it is automatically failed with the
    message *"Payment timed out. Please try again."*  This prevents permanently
    stuck rows when a user dismisses the STK prompt without responding.

    Live Daraja query (pending only)
    ---------------------------------
    When ``status == 'pending'`` the endpoint calls
    ``MpesaService.query_stk_push()`` and interprets the response:

    +---------------+---------------------------------------------+----------+
    | ResultCode    | Meaning                                     | Action   |
    +===============+=============================================+==========+
    | ``'0'``       | Payment confirmed                           | complete |
    +---------------+---------------------------------------------+----------+
    | ``''``        | Not yet available                           | keep poll|
    +---------------+---------------------------------------------+----------+
    | ``'1032'``    | Pending / user has not responded yet        | keep poll|
    +---------------+---------------------------------------------+----------+
    | ``'1'``       | Still being processed by Safaricom          | keep poll|
    +---------------+---------------------------------------------+----------+
    | contains      | "still under processing" in ResultDesc      | keep poll|
    | phrase        |                                             |          |
    +---------------+---------------------------------------------+----------+
    | other         | Definitive failure                          | fail     |
    +---------------+---------------------------------------------+----------+

    If the Daraja query raises any exception (network error, auth failure) it
    is silently swallowed and the transaction remains ``'pending'`` so the
    frontend keeps polling.

    URL parameter
    -------------
    ``checkout_request_id``
        The ``CheckoutRequestID`` string returned by ``mpesa_stk_push``.

    Response JSON
    -------------
    ::

        {
            "status":               "pending|completed|failed|cancelled",
            "result_desc":          "...",
            "mpesa_receipt_number": "QHJ12ABC34",   // non-empty on success
            "plan":                 "starter",
            "amount":               "1"             // or "2500" in production
        }

    Returns HTTP 404 if no matching transaction is found.
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

    # Auto-expire stale pending transactions before doing any Daraja query.
    if txn.status == 'pending':
        age_minutes = (timezone.now() - txn.created_at).total_seconds() / 60
        if age_minutes > _PENDING_TIMEOUT_MINUTES:
            txn.status      = 'failed'
            txn.result_desc = 'Payment timed out. Please try again.'
            txn.save(update_fields=['status', 'result_desc'])
            return JsonResponse({
                'status':               'failed',
                'result_desc':          txn.result_desc,
                'mpesa_receipt_number': '',
                'plan':                 txn.plan.tier if txn.plan else '',
                'amount':               str(txn.amount),
            })

    data = {
        'status':               txn.status,
        'result_desc':          txn.result_desc,
        'mpesa_receipt_number': txn.mpesa_receipt_number,
        'plan':                 txn.plan.tier if txn.plan else '',
        'amount':               str(txn.amount),
    }

    # If still pending, probe Daraja directly as a callback-delivery fallback.
    if txn.status == 'pending' and txn.checkout_request_id:
        service = MpesaService()
        try:
            result = service.query_stk_push(txn.checkout_request_id)

            # ResponseCode = API call success; ResultCode = payment outcome.
            # Only interpret ResultCode when the API call itself succeeded.
            api_response_code = str(result.get('ResponseCode', ''))
            if api_response_code != '0':
                pass  # Daraja API error — keep polling, don't fail the txn
            else:
                daraja_result_code = str(result.get('ResultCode', ''))
                result_desc        = result.get('ResultDesc', '')

                # These codes/phrases all mean "not yet settled — keep polling".
                PENDING_CODES = {
                    '',       # ResultCode not yet available in the response
                    '1032',   # Request in queue / user hasn't responded
                    '1',      # Safaricom is still processing
                }
                still_processing = 'still under processing' in result_desc.lower()

                if daraja_result_code == '0':
                    items   = result.get('CallbackMetadata', {}).get('Item', [])
                    receipt = next(
                        (i.get('Value', '') for i in items if i.get('Name') == 'MpesaReceiptNumber'),
                        '',
                    )
                    txn.status               = 'completed'
                    txn.result_code          = daraja_result_code
                    txn.result_desc          = result_desc
                    txn.mpesa_receipt_number = receipt
                    txn.save(update_fields=['status', 'result_code', 'result_desc', 'mpesa_receipt_number'])
                    _apply_plan_upgrade(txn)
                    data['status']               = 'completed'
                    data['mpesa_receipt_number'] = receipt

                elif daraja_result_code in PENDING_CODES or still_processing:
                    pass  # Still pending — keep polling

                else:
                    # Any other code is a definitive failure.
                    txn.status      = 'failed'
                    txn.result_code = daraja_result_code
                    txn.result_desc = result_desc
                    txn.save(update_fields=['status', 'result_code', 'result_desc'])
                    data['status']      = 'failed'
                    data['result_desc'] = result_desc

        except Exception:
            pass  # Transient Daraja error — keep polling silently

    return JsonResponse(data)
