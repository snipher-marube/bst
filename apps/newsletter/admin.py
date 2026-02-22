# newsletter/admin.py
from django.contrib import admin
from django import forms
from django.shortcuts import redirect
from django.contrib import messages
from django.template.response import TemplateResponse
from django.db.models import Count, Q
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone
import json

from .models import (
    Subscriber, Campaign, List, EmailTemplate,
    CampaignRecipient, ClickLink, BounceReport,
    WebhookEvent, ListSubscription
)
from .tasks import send_campaign
from .utils import export_subscribers_csv

# ============================================================================
# FORMS
# ============================================================================

class CampaignSendForm(forms.Form):
    """Form for campaign sending actions"""
    test_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={'class': 'vTextField', 'placeholder': 'test@example.com'}),
        help_text="Send test email to this address"
    )
    confirm_send = forms.BooleanField(
        required=True,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'vCheckbox'}),
        label="I confirm I want to send this campaign"
    )


# ============================================================================
# INLINES
# ============================================================================

class ListSubscriptionInline(admin.TabularInline):
    """Inline for list subscriptions"""
    model = ListSubscription
    extra = 1
    raw_id_fields = ['subscriber']
    classes = ['collapse']


# ============================================================================
# ACTIONS
# ============================================================================

def export_selected_subscribers(modeladmin, request, queryset):
    """Export selected subscribers to CSV"""
    return export_subscribers_csv(queryset)
export_selected_subscribers.short_description = "📥 Export selected subscribers to CSV"


def mark_as_active(modeladmin, request, queryset):
    """Mark subscribers as active"""
    updated = queryset.update(status=Subscriber.Status.ACTIVE)
    modeladmin.message_user(request, f"✅ {updated} subscriber(s) marked as active.")
mark_as_active.short_description = "✅ Mark as active"


def mark_as_unsubscribed(modeladmin, request, queryset):
    """Mark subscribers as unsubscribed"""
    updated = queryset.update(status=Subscriber.Status.UNSUBSCRIBED)
    modeladmin.message_user(request, f"📧 {updated} subscriber(s) unsubscribed.")
mark_as_unsubscribed.short_description = "📧 Mark as unsubscribed"


def export_list_subscribers(modeladmin, request, queryset):
    """Export subscribers from selected lists"""
    subscribers = Subscriber.objects.filter(
        lists__in=queryset,
        status=Subscriber.Status.ACTIVE
    ).distinct()
    return export_subscribers_csv(subscribers)
export_list_subscribers.short_description = "📥 Export subscribers from selected lists"


def duplicate_campaign(modeladmin, request, queryset):
    """Duplicate selected campaigns"""
    for campaign in queryset:
        campaign.pk = None
        campaign.name = f"{campaign.name} (Copy)"
        campaign.status = Campaign.Status.DRAFT
        campaign.scheduled_for = None
        campaign.sent_at = None
        campaign.save()
        
        # Copy lists (many-to-many)
        if queryset.first():
            campaign.lists.set(queryset.first().lists.all())
    
    modeladmin.message_user(request, f"📋 {queryset.count()} campaign(s) duplicated")
duplicate_campaign.short_description = "📋 Duplicate selected campaigns"


# ============================================================================
# ADMIN CLASSES
# ============================================================================

@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    """Admin for Subscriber model"""
    
    # List display
    list_display = [
        'email_display',
        'name_display',
        'status_badge',
        'list_membership',
        'engagement_score',
        'subscribed_at_display',
    ]
    
    list_filter = [
        'status',
        'source',
        'lists',
        ('subscribed_at', admin.DateFieldListFilter),
        ('confirmed_at', admin.DateFieldListFilter),
    ]
    
    search_fields = ['email', 'first_name', 'last_name']
    
    readonly_fields = [
        'unsubscribe_token', 'verification_token', 'email_hash',
        'total_opens', 'total_clicks', 'last_opened', 'last_clicked',
        'subscribed_at', 'confirmed_at', 'unsubscribed_at', 'updated_at'
    ]
    
    fieldsets = (
        ('📧 Contact Information', {
            'fields': ('email', 'first_name', 'last_name')
        }),
        ('📊 Status & Source', {
            'fields': ('status', 'source', 'lists')
        }),
        ('📈 Engagement Metrics', {
            'fields': (
                ('total_opens', 'total_clicks'),
                ('last_opened', 'last_clicked'),
            ),
            'classes': ('wide',)
        }),
        ('🔒 Security & Tracking', {
            'fields': (
                'unsubscribe_token', 'verification_token', 'email_hash',
                'ip_address', 'user_agent'
            ),
            'classes': ('collapse',)
        }),
        ('⚖️ GDPR Consent', {
            'fields': (
                ('consent_given', 'consent_ip', 'consent_date')
            ),
            'classes': ('collapse',)
        }),
        ('📝 Metadata', {
            'fields': ('metadata', 'tags'),
            'classes': ('collapse',)
        }),
        ('⏱️ Timestamps', {
            'fields': (
                ('subscribed_at', 'confirmed_at'),
                ('unsubscribed_at', 'updated_at')
            ),
            'classes': ('collapse',)
        })
    )
    
    actions = [
        export_selected_subscribers,
        mark_as_active,
        mark_as_unsubscribed,
    ]
    
    list_per_page = 50
    list_select_related = ['lists']
    save_on_top = True
    
    def email_display(self, obj):
        """Display email with icon"""
        return format_html(
            '<span style="font-weight: 500;">{}</span>',
            obj.email
        )
    email_display.short_description = "Email"
    email_display.admin_order_field = 'email'
    
    def name_display(self, obj):
        """Display full name or email if no name"""
        name = obj.full_name
        if name != obj.email:
            return name
        return "—"
    name_display.short_description = "Name"
    name_display.admin_order_field = 'first_name'
    
    def status_badge(self, obj):
        """Display status as colored badge"""
        colors = {
            'active': '#28a745',
            'pending': '#ffc107',
            'unsubscribed': '#6c757d',
            'bounced': '#dc3545',
            'complained': '#dc3545',
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 12px; font-size: 0.85em; font-weight: 500;">{}</span>',
            colors.get(obj.status, '#17a2b8'),
            obj.get_status_display().upper()
        )
    status_badge.short_description = "Status"
    status_badge.admin_order_field = 'status'
    
    def list_membership(self, obj):
        """Display list membership count"""
        count = obj.lists.count()
        return format_html(
            '<span title="{}">{}</span>',
            ', '.join(obj.lists.values_list('name', flat=True)),
            count
        )
    list_membership.short_description = "Lists"
    
    def engagement_score(self, obj):
        """Calculate engagement score"""
        total = obj.total_opens + (obj.total_clicks * 2)
        if total == 0:
            return format_html('<span style="color: #999;">Low</span>')
        elif total < 5:
            return format_html('<span style="color: #ffc107;">Medium</span>')
        else:
            return format_html('<span style="color: #28a745;">High</span>')
    engagement_score.short_description = "Engagement"
    
    def subscribed_at_display(self, obj):
        """Format subscribed date"""
        return obj.subscribed_at.strftime("%Y-%m-%d %H:%M")
    subscribed_at_display.short_description = "Subscribed"
    subscribed_at_display.admin_order_field = 'subscribed_at'


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    """Admin for Campaign model"""
    
    list_display = [
        'name_display',
        'subject_preview',
        'status_badge',
        'priority_badge',
        'recipients_count',
        'stats_display',
        'scheduled_display',
    ]
    
    list_filter = [
        'status',
        'priority',
        'lists',
        ('scheduled_for', admin.DateFieldListFilter),
        ('created_at', admin.DateFieldListFilter),
    ]
    
    search_fields = ['name', 'subject']
    filter_horizontal = ['lists']
    readonly_fields = [
        'total_recipients', 'total_sent', 'total_opens', 'total_clicks',
        'total_bounces', 'total_unsubscribes', 'sent_at', 'created_at',
        'updated_at', 'open_rate_display', 'click_rate_display'
    ]
    
    fieldsets = (
        ('📋 Basic Information', {
            'fields': ('name', 'subject', 'preheader')
        }),
        ('📝 Content', {
            'fields': ('content_html', 'content_text'),
            'classes': ('wide',)
        }),
        ('🎯 Targeting', {
            'fields': ('lists', 'segments')
        }),
        ('⏰ Status & Scheduling', {
            'fields': (
                ('status', 'priority'),
                ('scheduled_for', 'sent_at')
            ),
        }),
        ('📊 Tracking', {
            'fields': (
                ('track_opens', 'track_clicks'),
                'utm_campaign'
            ),
        }),
        ('📧 Email Settings', {
            'fields': ('from_email', 'reply_to'),
            'classes': ('wide',)
        }),
        ('📈 Performance Statistics', {
            'fields': (
                ('total_recipients', 'total_sent'),
                ('total_opens', 'open_rate_display'),
                ('total_clicks', 'click_rate_display'),
                ('total_bounces', 'total_unsubscribes')
            ),
            'classes': ('wide',)
        }),
        ('🔧 Advanced', {
            'fields': ('metadata',),
            'classes': ('collapse',)
        }),
        ('⏱️ System', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = ['send_test', 'send_campaign_action', 'duplicate_campaign']
    
    list_per_page = 25
    save_on_top = True
    
    def name_display(self, obj):
        """Display name with icon"""
        return format_html(
            '<strong>{}</strong>',
            obj.name
        )
    name_display.short_description = "Campaign"
    name_display.admin_order_field = 'name'
    
    def subject_preview(self, obj):
        """Preview subject line"""
        if len(obj.subject) > 40:
            return obj.subject[:40] + '…'
        return obj.subject
    subject_preview.short_description = "Subject"
    subject_preview.admin_order_field = 'subject'
    
    def status_badge(self, obj):
        """Display status as colored badge"""
        colors = {
            'draft': '#6c757d',
            'scheduled': '#17a2b8',
            'sending': '#ffc107',
            'sent': '#28a745',
            'paused': '#fd7e14',
            'cancelled': '#dc3545',
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 12px; font-size: 0.85em; font-weight: 500;">{}</span>',
            colors.get(obj.status, '#6c757d'),
            obj.get_status_display().upper()
        )
    status_badge.short_description = "Status"
    status_badge.admin_order_field = 'status'
    
    def priority_badge(self, obj):
        """Display priority as colored badge"""
        colors = {
            'low': '#6c757d',
            'normal': '#17a2b8',
            'high': '#fd7e14',
            'urgent': '#dc3545',
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 10px; font-size: 0.8em;">{}</span>',
            colors.get(obj.priority, '#6c757d'),
            obj.get_priority_display().upper()
        )
    priority_badge.short_description = "Priority"
    priority_badge.admin_order_field = 'priority'
    
    def recipients_count(self, obj):
        """Display recipient count with tooltip"""
        if obj.total_recipients:
            return format_html(
                '<span title="{} sent">{}</span>',
                obj.total_sent,
                obj.total_recipients
            )
        return "—"
    recipients_count.short_description = "Recipients"
    
    def stats_display(self, obj):
        """Display open/click rates"""
        if obj.total_sent:
            return format_html(
                '<span title="Opens: {} | Clicks: {}">📨 {}% | 🔗 {}%</span>',
                obj.total_opens,
                obj.total_clicks,
                obj.open_rate,
                obj.click_rate
            )
        return "—"
    stats_display.short_description = "Stats"
    
    def scheduled_display(self, obj):
        """Format scheduled date"""
        if obj.scheduled_for:
            if obj.scheduled_for > timezone.now():
                return format_html(
                    '<span style="color: #17a2b8;">📅 {}</span>',
                    obj.scheduled_for.strftime("%Y-%m-%d %H:%M")
                )
            elif obj.sent_at:
                return format_html(
                    '<span style="color: #28a745;">✅ {}</span>',
                    obj.sent_at.strftime("%Y-%m-%d %H:%M")
                )
        return "—"
    scheduled_display.short_description = "Scheduled"
    scheduled_display.admin_order_field = 'scheduled_for'
    
    def open_rate_display(self, obj):
        """Display open rate with percentage"""
        return f"{obj.open_rate}%"
    open_rate_display.short_description = "Open Rate"
    
    def click_rate_display(self, obj):
        """Display click rate with percentage"""
        return f"{obj.click_rate}%"
    click_rate_display.short_description = "Click Rate"
    
    def send_test(self, request, queryset):
        """Send test email action"""
        if 'apply' in request.POST:
            form = CampaignSendForm(request.POST)
            if form.is_valid():
                test_email = form.cleaned_data['test_email']
                for campaign in queryset:
                    send_campaign.delay(
                        campaign.id,
                        test_mode=True,
                        test_email=test_email
                    )
                self.message_user(
                    request,
                    f"✅ Test campaign(s) queued for sending to {test_email}"
                )
                return redirect(request.get_full_path())
        else:
            form = CampaignSendForm()
        
        return TemplateResponse(request, 'admin/newsletter/send_campaign.html', {
            'campaigns': queryset,
            'form': form,
            'action': 'send_test',
            'title': 'Send Test Campaign',
            'opts': self.model._meta,
        })
    send_test.short_description = "📧 Send test email"
    
    def send_campaign_action(self, request, queryset):
        """Send campaign action"""
        if 'apply' in request.POST:
            form = CampaignSendForm(request.POST)
            if form.is_valid() and form.cleaned_data['confirm_send']:
                for campaign in queryset.filter(status='draft'):
                    campaign.status = 'scheduled'
                    campaign.save()
                    send_campaign.delay(campaign.id)
                self.message_user(
                    request,
                    f"✅ {queryset.count()} campaign(s) queued for sending"
                )
                return redirect(request.get_full_path())
        else:
            form = CampaignSendForm()
        
        return TemplateResponse(request, 'admin/newsletter/send_campaign.html', {
            'campaigns': queryset,
            'form': form,
            'action': 'send_campaign_action',
            'title': 'Send Campaign',
            'opts': self.model._meta,
        })
    send_campaign_action.short_description = "🚀 Send selected campaigns"
    
    duplicate_campaign.short_description = "📋 Duplicate selected campaigns"


@admin.register(List)
class ListAdmin(admin.ModelAdmin):
    """Admin for List model"""
    
    list_display = [
        'name_display',
        'slug',
        'status_badge',
        'subscriber_count',
        'created_display',
    ]
    
    list_filter = ['is_public', 'require_double_optin', 'created_at']
    search_fields = ['name', 'slug', 'description']
    prepopulated_fields = {'slug': ('name',)}
    
    # Fixed: Remove filter_horizontal for subscribers with through model
    # Instead, use raw_id_fields or exclude it
    raw_id_fields = ['subscribers']  # Alternative: use raw_id_fields
    # Or simply don't include it in the form:
    # exclude = ['subscribers']
    
    actions = [export_list_subscribers]
    
    fieldsets = (
        ('📋 List Information', {
            'fields': ('name', 'slug', 'description')
        }),
        ('⚙️ Settings', {
            'fields': ('is_public', 'require_double_optin')
        }),
        ('⏱️ Metadata', {
            'fields': ('created_at', 'updated_at', 'created_by'),
            'classes': ('collapse',)
        })
    )
    
    readonly_fields = ['created_at', 'updated_at']
    save_on_top = True
    
    def name_display(self, obj):
        """Display name with icon"""
        return format_html(
            '<strong>{}</strong>',
            obj.name
        )
    name_display.short_description = "Name"
    name_display.admin_order_field = 'name'
    
    def status_badge(self, obj):
        """Display public/private status"""
        if obj.is_public:
            return format_html(
                '<span style="color: #28a745;">🌐 Public</span>'
            )
        return format_html(
            '<span style="color: #6c757d;">🔒 Private</span>'
        )
    status_badge.short_description = "Visibility"
    
    def subscriber_count(self, obj):
        """Count active subscribers"""
        count = obj.active_subscriber_count
        return format_html(
            '<span style="font-weight: 500;">{}</span>',
            count
        )
    subscriber_count.short_description = "Active Subscribers"
    
    def created_display(self, obj):
        """Format created date"""
        return obj.created_at.strftime("%Y-%m-%d")
    created_display.short_description = "Created"
    created_display.admin_order_field = 'created_at'
    
    def get_queryset(self, request):
        """Optimize queryset with counts"""
        return super().get_queryset(request).annotate(
            subscriber_count=Count('subscribers', filter=Q(subscribers__status='active'))
        )


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    """Admin for EmailTemplate model"""
    
    list_display = [
        'name_display',
        'subject_preview',
        'variables_count',
        'updated_display',
        'created_display',
    ]
    
    list_filter = ['created_at', 'updated_at']
    search_fields = ['name', 'subject']
    readonly_fields = ['variables', 'created_at', 'updated_at']
    
    fieldsets = (
        ('📋 Template Information', {
            'fields': ('name', 'subject')
        }),
        ('📝 Content', {
            'fields': ('content_html', 'content_text'),
            'classes': ('wide',)
        }),
        ('🔧 Variables', {
            'fields': ('variables',),
            'classes': ('collapse',)
        }),
        ('⏱️ Metadata', {
            'fields': ('created_at', 'updated_at', 'created_by'),
            'classes': ('collapse',)
        })
    )
    
    def name_display(self, obj):
        """Display name with icon"""
        return format_html(
            '<strong>{}</strong>',
            obj.name
        )
    name_display.short_description = "Name"
    name_display.admin_order_field = 'name'
    
    def subject_preview(self, obj):
        """Preview subject"""
        if len(obj.subject) > 40:
            return obj.subject[:40] + '…'
        return obj.subject
    subject_preview.short_description = "Subject"
    
    def variables_count(self, obj):
        """Count template variables"""
        return len(obj.variables or [])
    variables_count.short_description = "Variables"
    
    def updated_display(self, obj):
        """Format updated date"""
        return obj.updated_at.strftime("%Y-%m-%d")
    updated_display.short_description = "Updated"
    updated_display.admin_order_field = 'updated_at'
    
    def created_display(self, obj):
        """Format created date"""
        return obj.created_at.strftime("%Y-%m-%d")
    created_display.short_description = "Created"
    created_display.admin_order_field = 'created_at'


@admin.register(BounceReport)
class BounceReportAdmin(admin.ModelAdmin):
    """Admin for BounceReport model"""
    
    list_display = [
        'email_display',
        'bounce_type_badge',
        'campaign_link',
        'reason_preview',
        'created_display',
    ]
    
    list_filter = ['bounce_type', 'created_at']
    search_fields = ['subscriber__email', 'reason']
    readonly_fields = ['subscriber', 'campaign', 'bounce_type', 'reason', 'raw_data', 'created_at']
    
    fieldsets = (
        ('📧 Subscriber Information', {
            'fields': ('subscriber', 'campaign')
        }),
        ('⚠️ Bounce Details', {
            'fields': ('bounce_type', 'reason')
        }),
        ('📊 Raw Data', {
            'fields': ('raw_data',),
            'classes': ('collapse',)
        }),
        ('⏱️ Timestamp', {
            'fields': ('created_at',)
        })
    )
    
    list_per_page = 50
    
    def email_display(self, obj):
        """Display subscriber email"""
        return format_html(
            '<strong>{}</strong>',
            obj.subscriber.email
        )
    email_display.short_description = "Email"
    email_display.admin_order_field = 'subscriber__email'
    
    def bounce_type_badge(self, obj):
        """Display bounce type as badge"""
        colors = {
            'hard': '#dc3545',
            'soft': '#ffc107',
            'complaint': '#fd7e14',
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 12px; font-size: 0.85em;">{}</span>',
            colors.get(obj.bounce_type, '#6c757d'),
            obj.get_bounce_type_display().upper()
        )
    bounce_type_badge.short_description = "Type"
    
    def campaign_link(self, obj):
        """Link to campaign if exists"""
        if obj.campaign:
            url = reverse('admin:newsletter_campaign_change', args=[obj.campaign.id])
            return format_html('<a href="{}">{}</a>', url, obj.campaign.name)
        return "—"
    campaign_link.short_description = "Campaign"
    
    def reason_preview(self, obj):
        """Preview bounce reason"""
        if obj.reason and len(obj.reason) > 50:
            return obj.reason[:50] + '…'
        return obj.reason or "—"
    reason_preview.short_description = "Reason"
    
    def created_display(self, obj):
        """Format created date"""
        return obj.created_at.strftime("%Y-%m-%d %H:%M")
    created_display.short_description = "Received"
    created_display.admin_order_field = 'created_at'
    
    def has_add_permission(self, request):
        """Prevent manual addition of bounce reports"""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Prevent editing of bounce reports"""
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """Admin for WebhookEvent model"""
    
    list_display = [
        'id_display',
        'provider_badge',
        'event_type_badge',
        'status_badge',
        'created_display',
    ]
    
    list_filter = ['provider', 'event_type', 'processed', 'created_at']
    readonly_fields = ['provider', 'event_type', 'payload', 'processed', 'processed_at', 'created_at']
    
    fieldsets = (
        ('📨 Webhook Information', {
            'fields': ('provider', 'event_type')
        }),
        ('📦 Payload', {
            'fields': ('payload',),
            'classes': ('wide',)
        }),
        ('⚙️ Processing Status', {
            'fields': ('processed', 'processed_at')
        }),
        ('⏱️ Timestamps', {
            'fields': ('created_at',)
        })
    )
    
    list_per_page = 50
    
    def id_display(self, obj):
        """Display ID with icon"""
        return format_html(
            '<span style="font-family: monospace;">#{}</span>',
            obj.id
        )
    id_display.short_description = "ID"
    id_display.admin_order_field = 'id'
    
    def provider_badge(self, obj):
        """Display provider with color"""
        colors = {
            'sendgrid': '#1A82E2',
            'ses': '#FF9900',
            'mailgun': '#F06B66',
            'postmark': '#FFE01B',
        }
        return format_html(
            '<span style="background-color: {}; color: {}; padding: 3px 8px; '
            'border-radius: 10px; font-size: 0.8em;">{}</span>',
            colors.get(obj.provider, '#6c757d'),
            'black' if obj.provider == 'postmark' else 'white',
            obj.provider.upper()
        )
    provider_badge.short_description = "Provider"
    
    def event_type_badge(self, obj):
        """Display event type"""
        colors = {
            'open': '#28a745',
            'click': '#17a2b8',
            'bounce': '#dc3545',
            'complaint': '#fd7e14',
            'delivered': '#28a745',
        }
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 10px; font-size: 0.8em;">{}</span>',
            colors.get(obj.event_type, '#6c757d'),
            obj.event_type.upper()
        )
    event_type_badge.short_description = "Event"
    
    def status_badge(self, obj):
        """Display processing status"""
        if obj.processed:
            return format_html(
                '<span style="color: #28a745;">✅ Processed</span>'
            )
        return format_html(
            '<span style="color: #ffc107;">⏳ Pending</span>'
        )
    status_badge.short_description = "Status"
    
    def created_display(self, obj):
        """Format created date"""
        return obj.created_at.strftime("%Y-%m-%d %H:%M:%S")
    created_display.short_description = "Received"
    created_display.admin_order_field = 'created_at'
    
    def has_add_permission(self, request):
        """Prevent manual addition"""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Prevent editing"""
        return False