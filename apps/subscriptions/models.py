import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.db.models import JSONField
from apps.workspaces.models import Workspace

User = get_user_model()

# ---------------------------------------------------------------------------
# Plan definitions – kept in DB so they can be edited by admins
# ---------------------------------------------------------------------------

PLAN_LIMITS = {
    'free':         {'max_tables': 5,   'max_records_per_table': 1_000,  'max_team_members': 1},
    'starter':      {'max_tables': 20,  'max_records_per_table': 10_000, 'max_team_members': 5},
    'professional': {'max_tables': 100, 'max_records_per_table': 100_000,'max_team_members': 20},
    'enterprise':   {'max_tables': 999, 'max_records_per_table': 999_999,'max_team_members': 999},
}


class Plan(models.Model):
    """Available subscription plans."""
    TIER_CHOICES = [
        ('free', 'Free'),
        ('starter', 'Starter'),
        ('professional', 'Professional'),
        ('enterprise', 'Enterprise'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, unique=True)
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_yearly = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    max_tables = models.IntegerField(default=5)
    max_records_per_table = models.IntegerField(default=1000)
    max_team_members = models.IntegerField(default=1)

    stripe_price_id_monthly = models.CharField(max_length=100, blank=True)
    stripe_price_id_yearly = models.CharField(max_length=100, blank=True)

    features = JSONField(default=list)  # list of feature strings
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['price_monthly']

    def __str__(self):
        return f"{self.name} (${self.price_monthly}/mo)"


class Subscription(models.Model):
    """Active subscription for a workspace."""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('trialing', 'Trialing'),
        ('past_due', 'Past Due'),
        ('cancelled', 'Cancelled'),
        ('incomplete', 'Incomplete'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name='subscription')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    is_yearly = models.BooleanField(default=False)

    # Stripe fields (filled when real Stripe is integrated)
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)

    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'status']),
        ]

    def __str__(self):
        plan_name = self.plan.name if self.plan else 'Free'
        return f"{self.workspace.name} – {plan_name} ({self.status})"

    def apply_plan_limits(self):
        """Push plan limits down to the workspace."""
        if not self.plan:
            limits = PLAN_LIMITS['free']
        else:
            limits = PLAN_LIMITS.get(self.plan.tier, PLAN_LIMITS['free'])
        self.workspace.max_tables = limits['max_tables']
        self.workspace.max_records_per_table = limits['max_records_per_table']
        self.workspace.max_team_members = limits['max_team_members']
        self.workspace.tier = self.plan.tier if self.plan else 'free'
        self.workspace.save(update_fields=[
            'max_tables', 'max_records_per_table', 'max_team_members', 'tier'
        ])


class MpesaTransaction(models.Model):
    """Records an M-Pesa STK Push payment attempt for a subscription upgrade."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='mpesa_transactions')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='mpesa_transactions')
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True)

    phone_number = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Daraja API identifiers
    merchant_request_id = models.CharField(max_length=100, blank=True)
    checkout_request_id = models.CharField(max_length=100, blank=True, db_index=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Filled on callback
    mpesa_receipt_number = models.CharField(max_length=50, blank=True)
    result_code = models.CharField(max_length=10, blank=True)
    result_desc = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['checkout_request_id', 'status']),
        ]

    def __str__(self):
        return f"M-Pesa {self.phone_number} → {self.amount} ({self.status})"


class StripeWebhookEvent(models.Model):
    """Raw Stripe webhook payloads for idempotency and audit."""
    stripe_event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=100)
    payload = JSONField(default=dict)
    processed = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type} ({self.stripe_event_id})"
