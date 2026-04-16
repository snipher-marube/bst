"""
apps/subscriptions/models.py
=============================
Database models for the AnalyticsMeta subscription and billing system.

Overview
--------
AnalyticsMeta uses a **tier-based** subscription model.  Each workspace sits on
one of four tiers (free → starter → professional → enterprise) which gate how
many tables, records, and team members that workspace can have.

Billing is primarily via **M-Pesa** (Safaricom Daraja STK Push) for the Kenyan
market, with Stripe fields reserved for future international expansion.

Model hierarchy
---------------
::

    Workspace (apps/workspaces)
      └── Subscription   (1-to-1)  — active plan + Stripe identifiers
      └── MpesaTransaction (many)  — one record per STK Push attempt

Supporting models
-----------------
* ``Plan``               — catalogue of available tiers and their limits
* ``StripeWebhookEvent`` — raw Stripe webhook payloads stored for idempotency
                           and audit; not yet live but scaffolded for future use.

Plan limits are also maintained as the ``PLAN_LIMITS`` dict in this module so
they can be referenced without a database query (e.g. during STK Push initiation
when we need the limits to create/refresh a Plan row).
"""

import uuid
from datetime import timedelta

from django.db import models
from django.contrib.auth import get_user_model
from django.db.models import JSONField
from django.utils import timezone

from apps.workspaces.models import Workspace

User = get_user_model()


# ---------------------------------------------------------------------------
# Plan limits – single source of truth
# ---------------------------------------------------------------------------

#: Maps every tier name to its hard limits.
#:
#: These values are used in two places:
#:
#: 1. ``Plan.get_or_create()`` calls to seed / refresh a Plan row from code.
#: 2. ``Subscription.apply_plan_limits()`` to push the limits down to the
#:    ``Workspace`` model after a successful payment.
#:
#: If you need to change a limit, update it here **and** run a data migration
#: to back-fill any existing ``Plan`` rows (or rely on the ``get_or_create``
#: logic to update them on the next payment for that tier).
PLAN_LIMITS: dict[str, dict] = {
    'free':         {'max_tables': 5,   'max_records_per_table': 1_000,  'max_team_members': 1},
    'starter':      {'max_tables': 20,  'max_records_per_table': 10_000, 'max_team_members': 5},
    'professional': {'max_tables': 100, 'max_records_per_table': 100_000,'max_team_members': 20},
    'enterprise':   {'max_tables': 999, 'max_records_per_table': 999_999,'max_team_members': 999},
}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class Plan(models.Model):
    """
    Catalogue entry for a subscription tier.

    One row exists per tier (free / starter / professional / enterprise).
    Rows are created lazily by ``get_or_create`` calls in the billing views the
    first time a payment is attempted for that tier, using ``PLAN_LIMITS`` as
    the authoritative source of limit values.

    Stripe price IDs are stored here so that future Stripe Checkout sessions can
    look up the correct price without hard-coding IDs in view logic.

    Fields
    ------
    tier
        Slug that matches a key in ``PLAN_LIMITS`` and ``MPESA_PLAN_PRICES``.
        Unique — there is exactly one Plan row per tier.
    price_monthly / price_yearly
        Display prices in KES.  The actual amount charged by the Daraja API is
        taken from ``MPESA_PLAN_PRICES`` in ``views.py`` (which may be 1 KES in
        sandbox mode), not from this field.
    max_tables / max_records_per_table / max_team_members
        Denormalised from ``PLAN_LIMITS`` for convenience.
    stripe_price_id_monthly / stripe_price_id_yearly
        Stripe Price object IDs — blank until Stripe integration is activated.
    features
        JSON list of human-readable feature strings, used on the billing and
        public pricing pages.
    is_active
        Set to ``False`` to retire a plan without deleting it (keeps historical
        transaction references intact).
    """

    TIER_CHOICES = [
        ('free',         'Free'),
        ('starter',      'Starter'),
        ('professional', 'Professional'),
        ('enterprise',   'Enterprise'),
    ]

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name           = models.CharField(max_length=50)
    tier           = models.CharField(max_length=20, choices=TIER_CHOICES, unique=True)
    price_monthly  = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_yearly   = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Limit columns — kept in sync with PLAN_LIMITS via get_or_create
    max_tables             = models.IntegerField(default=5)
    max_records_per_table  = models.IntegerField(default=1000)
    max_team_members       = models.IntegerField(default=1)

    # Stripe integration (future)
    stripe_price_id_monthly = models.CharField(max_length=100, blank=True)
    stripe_price_id_yearly  = models.CharField(max_length=100, blank=True)

    features  = JSONField(default=list)   # list[str] rendered in pricing UI
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['price_monthly']

    def __str__(self) -> str:
        return f"{self.name} (KES {self.price_monthly}/mo)"


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class Subscription(models.Model):
    """
    Represents the active (or historical) subscription for a single workspace.

    There is at most **one** Subscription per workspace (``OneToOneField``).
    When a M-Pesa payment succeeds the subscription row is upserted — existing
    row updated, or a new row created — and ``apply_plan_limits()`` is called
    to propagate the new limits to the workspace.

    Stripe fields
    -------------
    The Stripe fields (``stripe_customer_id``, ``stripe_subscription_id``) are
    blank until Stripe integration is activated.  They are populated by Stripe
    webhook handlers in ``views.py`` and are referenced when processing
    ``customer.subscription.*`` and ``invoice.*`` events.

    Status lifecycle
    ----------------
    ::

        (new)  →  active
               ↘  trialing  →  active | cancelled
        active →  past_due  →  active | cancelled
               ↘  cancelled

    Trial / period timestamps are null for M-Pesa subscriptions (Safaricom
    doesn't provide these; they are relevant only for Stripe recurring billing).

    Fields
    ------
    workspace
        1-to-1 link to the workspace this subscription belongs to.
    plan
        FK to the Plan that is currently active.  ``PROTECT`` deletion prevents
        a Plan row from being deleted while subscriptions reference it.
    status
        One of: active, trialing, past_due, cancelled, incomplete.
    is_yearly
        Whether the workspace is on an annual billing cycle (Stripe only for now).
    trial_ends_at / current_period_start / current_period_end / cancelled_at
        Stripe billing period timestamps.  All nullable.
    """

    STATUS_CHOICES = [
        ('active',     'Active'),
        ('trialing',   'Trialing'),
        ('past_due',   'Past Due'),
        ('cancelled',  'Cancelled'),
        ('incomplete', 'Incomplete'),
    ]

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name='subscription')
    plan      = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)

    status    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    is_yearly = models.BooleanField(default=False)

    # Stripe identifiers — blank until Stripe is activated
    stripe_customer_id      = models.CharField(max_length=100, blank=True)
    stripe_subscription_id  = models.CharField(max_length=100, blank=True)

    # Billing period timestamps (Stripe / future use)
    trial_ends_at          = models.DateTimeField(null=True, blank=True)
    current_period_start   = models.DateTimeField(null=True, blank=True)
    current_period_end     = models.DateTimeField(null=True, blank=True)
    cancelled_at           = models.DateTimeField(null=True, blank=True)

    # Grace period — set when a payment fails to give the user 7 days to pay
    # before the workspace is downgraded to the free tier.
    grace_period_ends_at = models.DateTimeField(null=True, blank=True)

    # Tracks which dunning emails have been sent so the daily task can
    # advance through the sequence without re-sending:
    #   0 = day-0 email sent (initial failure notice)
    #   1 = day-3 reminder sent
    #   2 = day-7 final warning sent
    #   3 = grace expired and workspace downgraded
    DUNNING_INITIAL  = 0
    DUNNING_DAY3     = 1
    DUNNING_DAY7     = 2
    DUNNING_EXPIRED  = 3
    dunning_stage = models.SmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'status']),
        ]

    def __str__(self) -> str:
        plan_name = self.plan.name if self.plan else 'Free'
        return f"{self.workspace.name} – {plan_name} ({self.status})"

    GRACE_PERIOD_DAYS = 7

    def start_grace_period(self) -> None:
        """
        Mark the subscription ``'past_due'`` and open a 7-day grace window.

        Called when Stripe reports a failed invoice payment.  The workspace
        remains fully accessible during the grace period so the owner has time
        to update their card.  A dunning email sequence (day 0 → 3 → 7) is
        driven by the ``process_grace_periods`` Celery Beat task.

        Idempotent — if a grace period is already open this is a no-op so that
        multiple ``invoice.payment_failed`` events from Stripe don't reset the
        countdown.
        """
        if self.grace_period_ends_at is not None:
            return  # grace already started — don't reset the clock
        self.status              = 'past_due'
        self.grace_period_ends_at = timezone.now() + timedelta(days=self.GRACE_PERIOD_DAYS)
        self.dunning_stage       = self.DUNNING_INITIAL
        self.save(update_fields=['status', 'grace_period_ends_at', 'dunning_stage'])

    def clear_grace_period(self) -> None:
        """
        Restore the subscription to ``'active'`` and cancel any open grace period.

        Called when Stripe confirms that a previously failed invoice was
        eventually paid (``invoice.paid`` / ``invoice.payment_succeeded``).
        """
        self.status               = 'active'
        self.grace_period_ends_at = None
        self.dunning_stage        = self.DUNNING_INITIAL
        self.save(update_fields=['status', 'grace_period_ends_at', 'dunning_stage'])

    def apply_plan_limits(self) -> None:
        """
        Push the plan's resource limits down to the workspace row.

        Called immediately after a subscription is created or updated so the
        workspace enforcement logic (``can_add_table``, record count checks,
        member invitation guards) picks up the new limits on the next request.

        Falls back to ``PLAN_LIMITS['free']`` if ``self.plan`` is ``None`` or
        the tier key is missing from the dict.

        Does a targeted ``update_fields`` save to avoid clobbering unrelated
        workspace columns that may have been updated concurrently.
        """
        limits = PLAN_LIMITS.get(
            self.plan.tier if self.plan else 'free',
            PLAN_LIMITS['free'],
        )
        self.workspace.max_tables             = limits['max_tables']
        self.workspace.max_records_per_table  = limits['max_records_per_table']
        self.workspace.max_team_members       = limits['max_team_members']
        self.workspace.tier                   = self.plan.tier if self.plan else 'free'
        self.workspace.save(update_fields=[
            'max_tables', 'max_records_per_table', 'max_team_members', 'tier'
        ])


# ---------------------------------------------------------------------------
# MpesaTransaction
# ---------------------------------------------------------------------------

class MpesaTransaction(models.Model):
    """
    Records a single M-Pesa STK Push payment attempt.

    One row is created in status ``'pending'`` at the moment the STK Push is
    dispatched.  It is then updated to ``'completed'`` or ``'failed'`` either
    by the Daraja callback (``mpesa_callback`` view) or by the status-polling
    endpoint (``mpesa_payment_status`` view) — whichever arrives first.

    Status lifecycle
    ----------------
    ::

        pending  →  completed   (payment confirmed by Safaricom)
                 →  failed      (user declined, timed out, or Daraja error)
                 →  cancelled   (superseded by a newer STK Push for the same
                                 workspace + tier before the user responds)

    Daraja identifiers
    ------------------
    ``merchant_request_id`` and ``checkout_request_id`` are returned by the
    initial STK Push API call and saved immediately.  The callback and the
    status-query endpoint both use ``checkout_request_id`` as the lookup key.

    Receipt
    -------
    ``mpesa_receipt_number`` (e.g. ``QHJ12ABC34``) is filled from the
    ``CallbackMetadata`` on a successful payment.  It is the reference customers
    see on their M-Pesa SMS confirmation and should be shown in the billing UI.

    Security note
    -------------
    ``phone_number`` is stored in normalised ``2547XXXXXXXX`` form (never the
    raw user input) to ensure consistent audit records.

    Fields
    ------
    workspace
        The workspace being upgraded.  ``CASCADE`` deletion is intentional —
        if a workspace is deleted all its payment records go with it.
    user
        The user who initiated the payment.  ``SET_NULL`` so records survive
        if an account is deleted.
    plan
        The plan tier being purchased.  ``SET_NULL`` keeps the transaction
        record intact if a Plan row is later retired (``is_active=False``).
    phone_number
        Normalised Safaricom number in ``2547XXXXXXXX`` format.
    amount
        KES amount actually sent to Daraja (1 in sandbox, full price in prod).
    merchant_request_id
        Daraja's internal request ID — returned in the STK Push response.
    checkout_request_id
        The primary key used to correlate the STK Push, the callback, and
        status-query responses.  Indexed for fast lookup.
    status
        See lifecycle above.
    mpesa_receipt_number
        Safaricom's transaction reference (populated on success).
    result_code / result_desc
        Raw Daraja result code and description, stored verbatim for debugging.
    """

    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('completed', 'Completed'),
        ('failed',    'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='mpesa_transactions')
    user      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='mpesa_transactions')
    plan      = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True)

    phone_number = models.CharField(max_length=20)
    amount       = models.DecimalField(max_digits=10, decimal_places=2)

    # Daraja API identifiers — populated after a successful STK Push dispatch
    merchant_request_id  = models.CharField(max_length=100, blank=True)
    checkout_request_id  = models.CharField(max_length=100, blank=True, db_index=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Populated when Safaricom confirms payment
    mpesa_receipt_number = models.CharField(max_length=50, blank=True)
    result_code          = models.CharField(max_length=10, blank=True)
    result_desc          = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Composite index used by mpesa_payment_status to look up a pending
            # transaction by checkout_request_id without a full table scan.
            models.Index(fields=['checkout_request_id', 'status']),
        ]

    def __str__(self) -> str:
        return f"M-Pesa {self.phone_number} → KES {self.amount} ({self.status}) [{self.workspace}]"


# ---------------------------------------------------------------------------
# StripeWebhookEvent
# ---------------------------------------------------------------------------

class StripeWebhookEvent(models.Model):
    """
    Persists every inbound Stripe webhook payload.

    Purpose
    -------
    1. **Idempotency** — before processing an event, ``stripe_webhook`` checks
       this table.  If the ``stripe_event_id`` is already present the event is
       skipped and Stripe receives a 200 OK (prevents duplicate processing on
       Stripe retries).

    2. **Audit trail** — raw payloads are kept indefinitely so that billing
       disputes or missed state transitions can be replayed by re-running the
       ``_process_stripe_event`` handler against the stored payload.

    3. **Error capture** — if processing raises an exception the traceback is
       stored in ``error`` and ``processed`` remains ``False``, making it easy
       to find and replay failed events via Django admin.

    Fields
    ------
    stripe_event_id
        Stripe's globally unique event ID (e.g. ``evt_1ABC...``).  Unique
        constraint provides the idempotency guarantee.
    event_type
        The Stripe event name (e.g. ``customer.subscription.updated``).
    payload
        Complete JSON body from Stripe, stored as-is for replay purposes.
    processed
        ``True`` once ``_process_stripe_event`` completes without error.
    error
        Non-empty if processing raised an exception — contains the string
        representation of the exception for later triage.
    """

    stripe_event_id = models.CharField(max_length=100, unique=True)
    event_type      = models.CharField(max_length=100)
    payload         = JSONField(default=dict)
    processed       = models.BooleanField(default=False)
    error           = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['processed', 'event_type']),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.stripe_event_id})"
