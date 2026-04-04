from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from django.db.models import Count, Sum, Q
from django.urls import reverse
from django.http import HttpResponse
import csv

from .models import Plan, Subscription, MpesaTransaction, StripeWebhookEvent, PLAN_LIMITS


# ─── Helpers ────────────────────────────────────────────────────────────────

def _status_badge(status, label=None):
    colours = {
        'active':     ('#14a44d', '#fff'),
        'trialing':   ('#54b4d3', '#fff'),
        'past_due':   ('#e4a11b', '#fff'),
        'cancelled':  ('#dc4c64', '#fff'),
        'incomplete': ('#a9a9a9', '#fff'),
        'pending':    ('#e4a11b', '#fff'),
        'completed':  ('#14a44d', '#fff'),
        'failed':     ('#dc4c64', '#fff'),
    }
    bg, fg = colours.get(status, ('#6c757d', '#fff'))
    text = label or status.replace('_', ' ').title()
    return format_html(
        '<span style="background:{};color:{};padding:2px 10px;border-radius:12px;'
        'font-size:11px;font-weight:600;letter-spacing:.4px">{}</span>',
        bg, fg, text,
    )


# ─── Plan ────────────────────────────────────────────────────────────────────

@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display  = ('name', 'tier', 'price_monthly_display', 'price_yearly_display',
                      'max_tables', 'max_records_per_table', 'max_team_members',
                      'active_subscribers', 'is_active')
    list_filter   = ('tier', 'is_active')
    search_fields = ('name', 'tier')
    readonly_fields = ('id', 'active_subscribers', 'limits_preview')
    ordering      = ('price_monthly',)

    fieldsets = (
        ('Identity', {
            'fields': ('id', 'name', 'tier', 'is_active'),
        }),
        ('Pricing (KES)', {
            'fields': ('price_monthly', 'price_yearly'),
        }),
        ('Resource Limits', {
            'description': 'Limits enforced on workspaces using this plan.',
            'fields': ('max_tables', 'max_records_per_table', 'max_team_members', 'limits_preview'),
        }),
        ('Stripe Integration', {
            'classes': ('collapse',),
            'fields': ('stripe_price_id_monthly', 'stripe_price_id_yearly'),
        }),
        ('Features', {
            'fields': ('features',),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _subscriber_count=Count(
                'subscription', filter=Q(subscription__status='active')
            )
        )

    @admin.display(description='Monthly price', ordering='price_monthly')
    def price_monthly_display(self, obj):
        return f'KES {obj.price_monthly:,.0f}' if obj.price_monthly else 'Free'

    @admin.display(description='Yearly price', ordering='price_yearly')
    def price_yearly_display(self, obj):
        return f'KES {obj.price_yearly:,.0f}' if obj.price_yearly else '—'

    @admin.display(description='Active subscribers', ordering='_subscriber_count')
    def active_subscribers(self, obj):
        return getattr(obj, '_subscriber_count', 0)

    @admin.display(description='Limits vs PLAN_LIMITS constant')
    def limits_preview(self, obj):
        source = PLAN_LIMITS.get(obj.tier, {})
        rows = ''
        for key in ('max_tables', 'max_records_per_table', 'max_team_members'):
            db_val  = getattr(obj, key)
            src_val = source.get(key, '—')
            match   = '✓' if db_val == src_val else '⚠ mismatch'
            colour  = '#14a44d' if db_val == src_val else '#dc4c64'
            rows += (
                f'<tr><td style="padding:3px 8px">{key}</td>'
                f'<td style="padding:3px 8px">{db_val}</td>'
                f'<td style="padding:3px 8px">{src_val}</td>'
                f'<td style="padding:3px 8px;color:{colour}">{match}</td></tr>'
            )
        return format_html(
            '<table style="font-size:12px;border-collapse:collapse">'
            '<thead><tr><th style="padding:3px 8px;text-align:left">Field</th>'
            '<th style="padding:3px 8px">DB value</th>'
            '<th style="padding:3px 8px">Code constant</th>'
            '<th style="padding:3px 8px">Status</th></tr></thead>'
            '<tbody>{}</tbody></table>', rows
        )


# ─── Subscription ─────────────────────────────────────────────────────────────

class MpesaTransactionInline(admin.TabularInline):
    """Shown inside the Workspace admin (MpesaTransaction → Workspace FK)."""
    model          = MpesaTransaction
    fk_name        = 'workspace'
    extra          = 0
    can_delete     = False
    max_num        = 10
    show_change_link = True
    readonly_fields  = ('id', 'created_at', 'phone_number', 'amount',
                        'status_badge', 'mpesa_receipt_number', 'plan')
    fields           = ('created_at', 'phone_number', 'amount',
                        'status_badge', 'mpesa_receipt_number', 'plan')
    ordering         = ('-created_at',)
    verbose_name     = 'Recent M-Pesa transaction'

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def status_badge(self, obj):
        return _status_badge(obj.status)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display  = ('workspace_link', 'plan', 'status_badge', 'is_yearly',
                      'current_period_end', 'days_remaining', 'updated_at')
    list_filter   = ('status', 'is_yearly', 'plan__tier')
    search_fields = ('workspace__name', 'stripe_customer_id', 'stripe_subscription_id')
    readonly_fields = ('id', 'created_at', 'updated_at', 'billing_summary')
    ordering       = ('-updated_at',)
    date_hierarchy = 'created_at'
    actions        = ['export_csv', 'mark_cancelled', 'sync_plan_limits']
    fieldsets = (
        ('Workspace', {
            'fields': ('id', 'workspace', 'billing_summary'),
        }),
        ('Plan & Status', {
            'fields': ('plan', 'status', 'is_yearly'),
        }),
        ('Billing Period', {
            'fields': ('trial_ends_at', 'current_period_start',
                       'current_period_end', 'cancelled_at'),
        }),
        ('Stripe Identifiers', {
            'classes': ('collapse',),
            'fields': ('stripe_customer_id', 'stripe_subscription_id'),
        }),
        ('Timestamps', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('workspace', 'plan')

    @admin.display(description='Workspace', ordering='workspace__name')
    def workspace_link(self, obj):
        url = reverse('admin:workspaces_workspace_change', args=[obj.workspace_id])
        return format_html('<a href="{}">{}</a>', url, obj.workspace.name)

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _status_badge(obj.status)

    @admin.display(description='Days remaining', ordering='current_period_end')
    def days_remaining(self, obj):
        if not obj.current_period_end:
            return '—'
        delta = (obj.current_period_end - timezone.now()).days
        if delta < 0:
            return format_html('<span style="color:#dc4c64">Expired</span>')
        if delta <= 7:
            return format_html('<span style="color:#e4a11b">{} days</span>', delta)
        return f'{delta} days'

    @admin.display(description='Billing summary')
    def billing_summary(self, obj):
        txns = obj.workspace.mpesa_transactions.all()
        total_paid = txns.filter(status='completed').aggregate(s=Sum('amount'))['s'] or 0
        completed  = txns.filter(status='completed').count()
        failed     = txns.filter(status='failed').count()
        return format_html(
            '<table style="font-size:12px">'
            '<tr><td style="padding:2px 8px">Total paid (M-Pesa)</td>'
            '<td><strong>KES {:,.0f}</strong></td></tr>'
            '<tr><td style="padding:2px 8px">Successful payments</td><td>{}</td></tr>'
            '<tr><td style="padding:2px 8px">Failed attempts</td><td>{}</td></tr>'
            '</table>',
            total_paid, completed, failed,
        )

    # ── Actions ──────────────────────────────────────────────────────────────

    @admin.action(description='Export selected subscriptions to CSV')
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="subscriptions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Workspace', 'Plan', 'Status', 'Yearly',
            'Period Start', 'Period End', 'Stripe Customer ID',
        ])
        for sub in queryset.select_related('workspace', 'plan'):
            writer.writerow([
                sub.workspace.name,
                sub.plan.name if sub.plan else '',
                sub.status,
                sub.is_yearly,
                sub.current_period_start or '',
                sub.current_period_end or '',
                sub.stripe_customer_id,
            ])
        return response

    @admin.action(description='Mark selected subscriptions as cancelled')
    def mark_cancelled(self, request, queryset):
        updated = queryset.exclude(status='cancelled').update(
            status='cancelled', cancelled_at=timezone.now()
        )
        self.message_user(request, f'{updated} subscription(s) marked as cancelled.')

    @admin.action(description='Sync plan limits to workspace')
    def sync_plan_limits(self, request, queryset):
        count = 0
        for sub in queryset.select_related('workspace', 'plan'):
            try:
                sub.apply_plan_limits()
                count += 1
            except Exception as exc:
                self.message_user(request, f'Failed for {sub.workspace.name}: {exc}', level='error')
        self.message_user(request, f'Plan limits synced for {count} workspace(s).')


# ─── MpesaTransaction ─────────────────────────────────────────────────────────

@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'workspace_link', 'user_display', 'phone_number',
                      'amount_display', 'plan', 'status_badge', 'mpesa_receipt_number')
    list_filter   = ('status', 'plan__tier', 'created_at')
    search_fields = ('phone_number', 'mpesa_receipt_number', 'checkout_request_id',
                      'merchant_request_id', 'workspace__name', 'user__email')
    readonly_fields = ('id', 'created_at', 'updated_at', 'workspace', 'user', 'plan',
                       'phone_number', 'amount', 'merchant_request_id', 'checkout_request_id',
                       'mpesa_receipt_number', 'result_code', 'result_desc')
    ordering        = ('-created_at',)
    date_hierarchy  = 'created_at'
    actions         = ['export_csv', 'mark_failed']

    fieldsets = (
        ('Transaction', {
            'fields': ('id', 'workspace', 'user', 'plan', 'phone_number', 'amount'),
        }),
        ('Daraja Identifiers', {
            'fields': ('merchant_request_id', 'checkout_request_id'),
        }),
        ('Result', {
            'fields': ('status', 'mpesa_receipt_number', 'result_code', 'result_desc'),
        }),
        ('Timestamps', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def has_add_permission(self, request):
        return False  # transactions are created programmatically only

    def has_delete_permission(self, request, obj=None):
        return False  # preserve audit trail

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('workspace', 'user', 'plan')

    @admin.display(description='Workspace', ordering='workspace__name')
    def workspace_link(self, obj):
        url = reverse('admin:workspaces_workspace_change', args=[obj.workspace_id])
        return format_html('<a href="{}">{}</a>', url, obj.workspace.name)

    @admin.display(description='User', ordering='user__email')
    def user_display(self, obj):
        if not obj.user:
            return '—'
        return obj.user.get_full_name() or obj.user.email

    @admin.display(description='Amount (KES)', ordering='amount')
    def amount_display(self, obj):
        return f'KES {obj.amount:,.2f}'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _status_badge(obj.status)

    @admin.action(description='Export selected transactions to CSV')
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="mpesa_transactions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Date', 'Workspace', 'User', 'Phone', 'Amount (KES)',
            'Plan', 'Status', 'Receipt', 'Checkout Request ID',
        ])
        for txn in queryset.select_related('workspace', 'user', 'plan'):
            writer.writerow([
                txn.created_at.strftime('%Y-%m-%d %H:%M'),
                txn.workspace.name,
                txn.user.email if txn.user else '',
                txn.phone_number,
                txn.amount,
                txn.plan.tier if txn.plan else '',
                txn.status,
                txn.mpesa_receipt_number,
                txn.checkout_request_id,
            ])
        return response

    @admin.action(description='Mark selected transactions as failed')
    def mark_failed(self, request, queryset):
        updated = queryset.filter(status='pending').update(
            status='failed', result_desc='Manually marked failed by admin'
        )
        self.message_user(request, f'{updated} pending transaction(s) marked as failed.')


# ─── StripeWebhookEvent ────────────────────────────────────────────────────────

@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'event_type', 'stripe_event_id',
                      'processed_badge', 'error_summary')
    list_filter   = ('processed', 'event_type', 'created_at')
    search_fields = ('stripe_event_id', 'event_type', 'error')
    readonly_fields = ('stripe_event_id', 'event_type', 'payload',
                        'processed', 'error', 'created_at')
    ordering      = ('-created_at',)
    date_hierarchy = 'created_at'
    actions       = ['mark_unprocessed']

    def has_add_permission(self, request):
        return False  # events come from Stripe only

    def has_delete_permission(self, request, obj=None):
        return False  # preserve audit trail

    @admin.display(description='Processed', ordering='processed', boolean=False)
    def processed_badge(self, obj):
        if obj.processed:
            return format_html(
                '<span style="color:#14a44d;font-weight:600">✓ Yes</span>'
            )
        label = 'Pending' if not obj.error else 'Failed'
        return _status_badge('failed' if obj.error else 'pending', label)

    @admin.display(description='Error')
    def error_summary(self, obj):
        if not obj.error:
            return '—'
        excerpt = obj.error[:80] + ('…' if len(obj.error) > 80 else '')
        return format_html(
            '<span style="color:#dc4c64;font-size:11px">{}</span>', excerpt
        )

    @admin.action(description='Mark selected events as unprocessed (for replay)')
    def mark_unprocessed(self, request, queryset):
        updated = queryset.filter(processed=True).update(processed=False, error='')
        self.message_user(request, f'{updated} event(s) reset to unprocessed.')
