# newsletter/models.py
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags
from django.template.loader import render_to_string
from django.conf import settings
import uuid
import hashlib
import re

User = get_user_model()

class Subscriber(models.Model):
    """Professional subscriber model with GDPR compliance"""
    
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PENDING = 'pending', 'Pending Confirmation'
        UNSUBSCRIBED = 'unsubscribed', 'Unsubscribed'
        BOUNCED = 'bounced', 'Bounced'
        COMPLAINED = 'complained', 'Complained'
        
    class Source(models.TextChoices):
        WEBSITE = 'website', 'Website Footer'
        LANDING_PAGE = 'landing', 'Landing Page'
        IMPORT = 'import', 'Bulk Import'
        API = 'api', 'API'
        REFERRAL = 'referral', 'Referral'
        
    # Core fields
    email = models.EmailField(
        unique=True,
        db_index=True,
        validators=[EmailValidator()],
        help_text="Subscriber's email address"
    )
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    
    # Status and tracking
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.WEBSITE
    )
    
    # Engagement metrics
    total_opens = models.PositiveIntegerField(default=0)
    total_clicks = models.PositiveIntegerField(default=0)
    last_opened = models.DateTimeField(null=True, blank=True)
    last_clicked = models.DateTimeField(null=True, blank=True)
    
    # Security and tracking
    unsubscribe_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    verification_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email_hash = models.CharField(max_length=64, unique=True, editable=False)
    
    # IP and user agent for security
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    
    # Metadata
    subscribed_at = models.DateTimeField(default=timezone.now, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # GDPR compliance
    consent_given = models.BooleanField(default=False)
    consent_ip = models.GenericIPAddressField(null=True, blank=True)
    consent_date = models.DateTimeField(null=True, blank=True)
    
    # Custom fields (JSON for flexibility)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['email', 'status']),
            models.Index(fields=['unsubscribe_token']),
            models.Index(fields=['-subscribed_at']),
        ]
        ordering = ['-subscribed_at']
        verbose_name = 'Subscriber'
        verbose_name_plural = 'Subscribers'
    
    def save(self, *args, **kwargs):
        """Generate email hash for privacy"""
        if not self.email_hash:
            self.email_hash = hashlib.sha256(self.email.lower().encode()).hexdigest()
        super().save(*args, **kwargs)
    
    def clean(self):
        """Validate email format"""
        if self.email:
            # Additional validation for disposable emails (optional)
            disposable_domains = getattr(settings, 'DISPOSABLE_EMAIL_DOMAINS', [])
            domain = self.email.split('@')[1].lower()
            if domain in disposable_domains:
                raise ValidationError('Disposable email addresses are not allowed.')
    
    def __str__(self):
        return f"{self.email} ({self.get_status_display()})"
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.email
    
    def confirm_subscription(self):
        """Confirm subscriber after email verification"""
        self.status = self.Status.ACTIVE
        self.confirmed_at = timezone.now()
        self.save(update_fields=['status', 'confirmed_at'])
    
    def unsubscribe(self):
        """Unsubscribe and record time"""
        self.status = self.Status.UNSUBSCRIBED
        self.unsubscribed_at = timezone.now()
        self.save(update_fields=['status', 'unsubscribed_at'])


class List(models.Model):
    """Email lists/categories for segmentation"""
    
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, db_index=True)
    description = models.TextField(blank=True)
    
    # Subscribers many-to-many with through model for additional data
    subscribers = models.ManyToManyField(
        Subscriber,
        through='ListSubscription',
        related_name='lists'
    )
    
    # List settings
    is_public = models.BooleanField(default=True)
    require_double_optin = models.BooleanField(default=True)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_lists'
    )
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name
    
    @property
    def active_subscriber_count(self):
        return self.subscribers.filter(status=Subscriber.Status.ACTIVE).count()


class ListSubscription(models.Model):
    """Through model for list-subscriber relationship with metadata"""
    
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE)
    list = models.ForeignKey(List, on_delete=models.CASCADE)
    subscribed_at = models.DateTimeField(default=timezone.now)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['subscriber', 'list']
        indexes = [
            models.Index(fields=['subscriber', 'list', 'subscribed_at']),
        ]


class Campaign(models.Model):
    """Professional campaign model with A/B testing capabilities"""
    
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SCHEDULED = 'scheduled', 'Scheduled'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        PAUSED = 'paused', 'Paused'
        CANCELLED = 'cancelled', 'Cancelled'
    
    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'
    
    # Basic info
    name = models.CharField(max_length=255, db_index=True)
    subject = models.CharField(max_length=255)
    preheader = models.CharField(
        max_length=255,
        blank=True,
        help_text="Preview text shown after subject line"
    )
    
    # Content
    content_html = models.TextField(help_text="HTML email content")
    content_text = models.TextField(
        blank=True,
        help_text="Plain text version (auto-generated if empty)"
    )
    
    # Targeting
    lists = models.ManyToManyField(List, related_name='campaigns')
    segments = models.JSONField(default=dict, blank=True)  # JSON for complex targeting
    
    # Status and tracking
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True
    )
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True
    )
    
    # Scheduling
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    
    # A/B testing
    is_ab_test = models.BooleanField(default=False)
    ab_test_variants = models.JSONField(default=list, blank=True)
    ab_test_winner = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ab_test_variants_set'
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_campaigns'
    )
    
    # Statistics (cached for performance)
    total_recipients = models.PositiveIntegerField(default=0)
    total_sent = models.PositiveIntegerField(default=0)
    total_opens = models.PositiveIntegerField(default=0)
    total_clicks = models.PositiveIntegerField(default=0)
    total_bounces = models.PositiveIntegerField(default=0)
    total_unsubscribes = models.PositiveIntegerField(default=0)
    
    # Settings
    track_opens = models.BooleanField(default=True)
    track_clicks = models.BooleanField(default=True)
    utm_campaign = models.CharField(max_length=255, blank=True)
    reply_to = models.EmailField(blank=True)
    from_email = models.EmailField(default=settings.DEFAULT_FROM_EMAIL)
    
    # JSON metadata for flexibility
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['-created_at']),
        ]
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    def save(self, *args, **kwargs):
        """Auto-generate plain text version if not provided"""
        if not self.content_text and self.content_html:
            self.content_text = strip_tags(self.content_html)
        super().save(*args, **kwargs)
    
    @property
    def open_rate(self):
        if self.total_sent:
            return round((self.total_opens / self.total_sent) * 100, 2)
        return 0.0
    
    @property
    def click_rate(self):
        if self.total_sent:
            return round((self.total_clicks / self.total_sent) * 100, 2)
        return 0.0


class CampaignRecipient(models.Model):
    """Track individual campaign sends and engagement"""
    
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        DELIVERED = 'delivered', 'Delivered'
        OPENED = 'opened', 'Opened'
        CLICKED = 'clicked', 'Clicked'
        BOUNCED = 'bounced', 'Bounced'
        COMPLAINED = 'complained', 'Complained'
        UNSUBSCRIBED = 'unsubscribed', 'Unsubscribed'
        FAILED = 'failed', 'Failed'
    
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='recipients'
    )
    subscriber = models.ForeignKey(
        Subscriber,
        on_delete=models.CASCADE,
        related_name='campaign_recipients'
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    
    # Tracking
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    
    # Engagement data
    open_count = models.PositiveIntegerField(default=0)
    click_count = models.PositiveIntegerField(default=0)
    
    # For click tracking
    clicked_links = models.JSONField(default=list, blank=True)
    
    # Technical details
    message_id = models.CharField(max_length=255, blank=True, db_index=True)
    error_message = models.TextField(blank=True)
    
    # Metadata
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        unique_together = ['campaign', 'subscriber']
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['message_id']),
        ]
    
    def __str__(self):
        return f"{self.subscriber.email} - {self.campaign.name}"
    
    def record_open(self):
        """Record an email open"""
        self.open_count += 1
        self.status = self.Status.OPENED
        if not self.opened_at:
            self.opened_at = timezone.now()
        self.save(update_fields=['open_count', 'status', 'opened_at'])
        
        # Update subscriber stats
        self.subscriber.total_opens += 1
        self.subscriber.last_opened = timezone.now()
        self.subscriber.save(update_fields=['total_opens', 'last_opened'])
    
    def record_click(self, url):
        """Record a link click"""
        self.click_count += 1
        if url not in self.clicked_links:
            self.clicked_links.append(url)
        self.status = self.Status.CLICKED
        if not self.clicked_at:
            self.clicked_at = timezone.now()
        self.save(update_fields=['click_count', 'clicked_links', 'status', 'clicked_at'])
        
        # Update subscriber stats
        self.subscriber.total_clicks += 1
        self.subscriber.last_clicked = timezone.now()
        self.subscriber.save(update_fields=['total_clicks', 'last_clicked'])


class EmailTemplate(models.Model):
    """Reusable email templates"""
    
    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    content_html = models.TextField()
    content_text = models.TextField(blank=True)
    
    # Template variables
    variables = models.JSONField(default=list, blank=True)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name
    
    def save(self, *args, **kwargs):
        """Extract template variables from content"""
        if not self.variables:
            # Extract {{ variable }} patterns
            pattern = r'\{\{\s*([^}]+)\s*\}\}'
            self.variables = list(set(re.findall(pattern, self.content_html)))
        super().save(*args, **kwargs)


class ClickLink(models.Model):
    """Track links in campaigns for click analytics"""
    
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='links'
    )
    url = models.URLField(max_length=2000)
    url_hash = models.CharField(max_length=64, unique=True, editable=False)
    click_count = models.PositiveIntegerField(default=0)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    
    def save(self, *args, **kwargs):
        """Generate URL hash for tracking"""
        if not self.url_hash:
            self.url_hash = hashlib.sha256(self.url.encode()).hexdigest()
        super().save(*args, **kwargs)
    
    class Meta:
        indexes = [
            models.Index(fields=['url_hash']),
            models.Index(fields=['campaign', '-click_count']),
        ]


class BounceReport(models.Model):
    """Track email bounces for list hygiene"""
    
    class BounceType(models.TextChoices):
        HARD = 'hard', 'Hard Bounce'
        SOFT = 'soft', 'Soft Bounce'
        COMPLAINT = 'complaint', 'Complaint'
    
    subscriber = models.ForeignKey(
        Subscriber,
        on_delete=models.CASCADE,
        related_name='bounce_reports'
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    
    bounce_type = models.CharField(max_length=20, choices=BounceType.choices)
    reason = models.TextField(blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['subscriber', '-created_at']),
        ]
        ordering = ['-created_at']


class WebhookEvent(models.Model):
    """Track incoming webhooks from email service providers"""
    
    provider = models.CharField(max_length=50)  # sendgrid, ses, etc.
    event_type = models.CharField(max_length=50, db_index=True)
    payload = models.JSONField()
    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['provider', 'event_type', 'processed']),
        ]