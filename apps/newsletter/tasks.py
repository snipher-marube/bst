# newsletter/tasks.py
import logging
import re
from typing import Optional, Dict, Any
from functools import wraps
from urllib.parse import quote

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction, IntegrityError
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import (
    Campaign, Subscriber, CampaignRecipient,
    ClickLink, BounceReport, WebhookEvent
)

logger = logging.getLogger(__name__)

# ============================================================================
# DECORATORS
# ============================================================================

def task_logger(task_func):
    """Decorator to log task start/end"""
    @wraps(task_func)
    def wrapper(*args, **kwargs):
        task_name = task_func.__name__
        logger.info(f"🚀 Starting {task_name}")
        try:
            result = task_func(*args, **kwargs)
            logger.info(f"✅ Completed {task_name}")
            return result
        except Exception as e:
            logger.error(f"❌ Failed {task_name}: {str(e)}", exc_info=True)
            raise
    return wrapper


# ============================================================================
# TEST TASKS
# ============================================================================

@shared_task
@task_logger
def ping():
    """Simple task to test Celery"""
    return "PONG"

@shared_task
@task_logger
def test_task():
    """Test task to verify Celery is working"""
    return "Task executed successfully"


# ============================================================================
# MAIN CAMPAIGN TASK
# ============================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
@task_logger
def send_campaign(self, campaign_id: int, test_mode: bool = False, test_email: Optional[str] = None):
    """
    Background task to send a campaign to all recipients with retry logic
    """
    campaign = _get_campaign(campaign_id)
    if not campaign:
        logger.error(f"Campaign {campaign_id} not found or invalid status")
        return

    try:
        if test_mode and test_email:
            _send_test_campaign(campaign, test_email)
            # Don't update status for test emails
            logger.info(f"✅ Test campaign {campaign_id} sent to {test_email}")
        else:
            _send_bulk_campaign(campaign)
            _update_campaign_status(campaign, Campaign.Status.SENT, sent_at=timezone.now())
        
    except Exception as exc:
        logger.error(f"Campaign {campaign_id} failed: {str(exc)}", exc_info=True)
        _update_campaign_status(campaign, Campaign.Status.DRAFT)
        
        # Retry with exponential backoff
        countdown = 60 * (2 ** self.request.retries)
        self.retry(exc=exc, countdown=min(countdown, 3600))  # Max 1 hour

# ============================================================================
# WEBHOOK TASK
# ============================================================================

@shared_task
@task_logger
def process_webhook_event(event_id: int):
    """
    Process webhook events from email service providers
    """
    try:
        event = WebhookEvent.objects.get(id=event_id, processed=False)
    except WebhookEvent.DoesNotExist:
        logger.warning(f"Webhook event {event_id} not found or already processed")
        return

    handlers = {
        'open': _handle_open_event,
        'click': _handle_click_event,
        'bounce': _handle_bounce_event,
        'complaint': _handle_complaint_event,
    }

    handler = handlers.get(event.event_type)
    if handler:
        try:
            handler(event.payload)
            event.processed = True
            event.processed_at = timezone.now()
            event.save(update_fields=['processed', 'processed_at'])
            logger.info(f"✅ Processed webhook event {event_id}: {event.event_type}")
        except Exception as e:
            logger.error(f"❌ Failed to process webhook event {event_id}: {str(e)}")
    else:
        logger.warning(f"No handler for event type: {event.event_type}")


# ============================================================================
# CAMPAIGN SENDING HELPERS
# ============================================================================
def _get_campaign(campaign_id: int) -> Optional[Campaign]:
    """Get campaign by ID with proper status - allow scheduled status"""
    try:
        return Campaign.objects.select_related().get(
            id=campaign_id,
            status__in=['draft', 'scheduled', 'sending'] 
        )
    except Campaign.DoesNotExist:
        logger.error(f"Campaign {campaign_id} not found or not in valid status")
        return None

def _update_campaign_status(campaign: Campaign, status: str, **extra_fields):
    """Update campaign status and optional fields"""
    update_fields = ['status']
    campaign.status = status
    
    for field, value in extra_fields.items():
        if hasattr(campaign, field):
            setattr(campaign, field, value)
            update_fields.append(field)
    
    campaign.save(update_fields=update_fields)
    logger.info(f"Campaign {campaign.id} status updated to {status}")


def _send_test_campaign(campaign: Campaign, test_email: str):
    """Send a test campaign to a single email"""
    _send_single_email(campaign, None, test_email)
    logger.info(f"✅ Test campaign {campaign.name} sent to {test_email}")



def _send_bulk_campaign(campaign: Campaign):
    """Send campaign to all active subscribers in batches"""
    subscribers = _get_active_subscribers(campaign)
    total = subscribers.count()
    
    if total == 0:
        logger.warning(f"⚠️ No active subscribers for campaign {campaign.name}")
        campaign.total_recipients = 0
        campaign.total_sent = 0
        campaign.save(update_fields=['total_recipients', 'total_sent'])
        return

    campaign.total_recipients = total
    campaign.save(update_fields=['total_recipients'])

    stats = {
        'sent': 0,
        'failed': 0,
        'total': total
    }

    for subscriber in subscribers.iterator(chunk_size=100):
        try:
            success = _process_recipient(campaign, subscriber)
            if success:
                stats['sent'] += 1
            else:
                stats['failed'] += 1
        except Exception as e:
            logger.error(f"Error processing {subscriber.email}: {str(e)}")
            stats['failed'] += 1
            _create_failed_recipient(campaign, subscriber, str(e))

        # Log progress every 100 emails
        if stats['sent'] % 100 == 0 or stats['sent'] == total:
            logger.info(
                f"Campaign {campaign.name}: "
                f"{stats['sent']}/{stats['total']} sent, "
                f"{stats['failed']} failed"
            )
            campaign.total_sent = stats['sent']
            campaign.save(update_fields=['total_sent'])

    _finalize_campaign(campaign, stats)

def _get_active_subscribers(campaign: Campaign):
    """Get active subscribers for campaign lists"""
    return Subscriber.objects.filter(
        lists__in=campaign.lists.all(),
        status=Subscriber.Status.ACTIVE
    ).distinct().only('id', 'email', 'first_name', 'last_name', 'unsubscribe_token')


def _process_recipient(campaign: Campaign, subscriber: Subscriber) -> bool:
    """Process a single recipient with better error handling"""
    try:
        # Create recipient record first
        recipient, created = CampaignRecipient.objects.get_or_create(
            campaign=campaign,
            subscriber=subscriber,
            defaults={'status': CampaignRecipient.Status.PENDING}
        )
        
        # Send email
        _send_single_email(campaign, subscriber, subscriber.email)
        
        # Update recipient status
        recipient.status = CampaignRecipient.Status.SENT
        recipient.sent_at = timezone.now()
        recipient.save(update_fields=['status', 'sent_at'])
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to send to {subscriber.email}: {str(e)}")
        _create_failed_recipient(campaign, subscriber, str(e))
        return False

def _log_progress(campaign: Campaign, stats: Dict[str, int]):
    """Log progress periodically"""
    if stats['sent'] % 100 == 0 or stats['sent'] == stats['total']:
        logger.info(
            f"Campaign {campaign.name}: "
            f"{stats['sent']}/{stats['total']} sent, "
            f"{stats['failed']} failed"
        )
        campaign.total_sent = stats['sent']
        campaign.save(update_fields=['total_sent'])


def _finalize_campaign(campaign: Campaign, stats: Dict[str, int]):
    """Finalize campaign after sending"""
    campaign.total_sent = stats['sent']
    campaign.save(update_fields=['total_sent'])
    
    logger.info(
        f"✅ Campaign {campaign.name} completed: "
        f"{stats['sent']}/{stats['total']} sent, "
        f"{stats['failed']} failed"
    )


# ============================================================================
# EMAIL SENDING HELPERS
# ============================================================================

def _send_single_email(campaign: Campaign, subscriber: Subscriber, email: str):
    """
    Send a single email with personalization and tracking
    """
    html_content, text_content = _prepare_email_content(campaign, subscriber)

    email_message = _create_email_message(campaign, email, text_content, html_content)
    
    # Add message ID for tracking
    sub_id = subscriber.id if subscriber else 'test'
    message_id = f"<{campaign.id}.{sub_id}.{timezone.now().timestamp()}@{settings.SITE_NAME}>"
    email_message.extra_headers['Message-ID'] = message_id
    
    email_message.send(fail_silently=False)
    
    # Update recipient with message_id if we have one
    if subscriber:
        CampaignRecipient.objects.filter(
            campaign=campaign,
            subscriber=subscriber
        ).update(message_id=message_id)

def _get_or_create_recipient(campaign: Campaign, subscriber: Optional[Subscriber]):
    """Get or create campaign recipient record"""
    if not subscriber:
        return None
    
    recipient, _ = CampaignRecipient.objects.get_or_create(
        campaign=campaign,
        subscriber=subscriber,
        defaults={'status': 'pending'}
    )
    return recipient


def _create_email_message(campaign: Campaign, to_email: str, text_content: str, html_content: str):
    """
    Build a multipart/alternative email: plain-text body + HTML alternative.
    Email clients always prefer the HTML part; the text part is the fallback.
    """
    # X-Priority: 1=Urgent  2=High  3=Normal  4=Low  5=Very Low
    priority_map = {'urgent': '1', 'high': '2', 'normal': '3', 'low': '5'}

    headers = {
        'List-Unsubscribe': (
            f'<mailto:{settings.DEFAULT_FROM_EMAIL}?subject=unsubscribe>,'
            f' <{settings.SITE_URL}/newsletter/unsubscribe/>'
        ),
        'X-Campaign-ID': str(campaign.id),
        'X-Mailer': 'AnalyticsMeta-Newsletter/1.0',
        'X-Priority': priority_map.get(campaign.priority, '3'),
        'MIME-Version': '1.0',
    }

    message = EmailMultiAlternatives(
        subject=campaign.subject,
        body=text_content,                             # plain-text fallback
        from_email=campaign.from_email or settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
        reply_to=[campaign.reply_to] if campaign.reply_to else None,
        headers=headers,
    )
    # Attach the fully-rendered HTML as the preferred alternative.
    # RFC 2046 §5.1.4: the LAST alternative is preferred by clients.
    message.attach_alternative(html_content, 'text/html')
    return message


def _update_recipient_status(recipient: Optional[CampaignRecipient]):
    """Update recipient status after sending"""
    if recipient:
        recipient.status = CampaignRecipient.Status.SENT
        recipient.sent_at = timezone.now()
        recipient.save(update_fields=['status', 'sent_at'])


# ============================================================================
# CONTENT PREPARATION
# ============================================================================

def _prepare_email_content(campaign: Campaign, subscriber: Optional[Subscriber]) -> tuple:
    """
    Render campaign content through the branded email template, then apply tracking.
    """
    unsubscribe_url = (
        f"{settings.SITE_URL}/newsletter/unsubscribe/"
        f"{subscriber.unsubscribe_token}/?campaign={campaign.id}"
        if subscriber else "#"
    )

    context = {
        'campaign': campaign,
        'subscriber': subscriber,
        'unsubscribe_url': unsubscribe_url,
        'site_name': getattr(settings, 'SITE_NAME', 'Businessight'),
        'site_url': getattr(settings, 'SITE_URL', '').rstrip('/'),
        'support_email': getattr(settings, 'SUPPORT_EMAIL', ''),
        'address': getattr(settings, 'ADDRESS', ''),
    }

    html_content = render_to_string(
        'newsletter/email_templates/campaign_email.html', context
    )
    text_content = campaign.content_text or strip_tags(campaign.content_html)

    if subscriber:
        # Append plain-text unsubscribe footer
        text_content += (
            f"\n\n---\nTo unsubscribe visit: {unsubscribe_url}"
        )

    if not subscriber:
        return html_content, text_content

    # Apply tracking after the full template is rendered
    if campaign.track_opens:
        html_content = _add_tracking_pixel(html_content, campaign, subscriber)

    if campaign.track_clicks:
        html_content = _add_click_tracking(html_content, campaign, subscriber)

    return html_content, text_content


def _add_tracking_pixel(html: str, campaign: Campaign, subscriber: Subscriber) -> str:
    """Add tracking pixel to HTML content"""
    tracking_url = f"{settings.SITE_URL}/newsletter/track/open/{campaign.id}/{subscriber.id}/"
    return html + f'<img src="{tracking_url}" width="1" height="1" alt="" style="display:none"/>'


def _add_click_tracking(html: str, campaign: Campaign, subscriber: Subscriber) -> str:
    """
    Wrap all trackable links with the click-tracking redirect URL.

    ClickLink records are created once per campaign/URL (not once per subscriber).
    We use get_or_create and swallow IntegrityError to handle concurrent workers
    attempting to insert the same URL hash simultaneously.
    """
    # Skip URLs that belong to our own tracking/unsubscribe infrastructure
    _skip_prefixes = ('mailto:', '#', 'tel:')
    _skip_contains = ('/newsletter/track/', '/newsletter/unsubscribe/')
    site_url = settings.SITE_URL.rstrip('/')

    pattern = r'href="([^"]+)"'

    # First pass: collect all unique trackable URLs and ensure ClickLink rows exist
    all_urls = set(re.findall(pattern, html))
    for url in all_urls:
        if url.startswith(_skip_prefixes):
            continue
        if any(s in url for s in _skip_contains):
            continue
        try:
            ClickLink.objects.get_or_create(campaign=campaign, url=url)
        except IntegrityError:
            # Another worker already created it — safe to ignore
            pass

    # Second pass: rewrite the href values
    def replace_link(match):
        original_url = match.group(1)
        if original_url.startswith(_skip_prefixes):
            return match.group(0)
        if any(s in original_url for s in _skip_contains):
            return match.group(0)
        tracking_url = (
            f"{site_url}/newsletter/track/click/"
            f"{campaign.id}/{subscriber.id}/?url={quote(original_url, safe='')}"
        )
        return f'href="{tracking_url}"'

    return re.sub(pattern, replace_link, html)


def _create_failed_recipient(campaign: Campaign, subscriber: Subscriber, error: str):
    """Create or update failed recipient record"""
    CampaignRecipient.objects.update_or_create(
        campaign=campaign,
        subscriber=subscriber,
        defaults={
            'status': CampaignRecipient.Status.FAILED,
            'error_message': error[:500]  # Truncate long error messages
        }
    )


# ============================================================================
# WEBHOOK EVENT HANDLERS
# ============================================================================

def _handle_open_event(payload: Dict[str, Any]):
    """Handle email open event"""
    message_id = payload.get('message_id')
    if not message_id:
        return

    recipient = CampaignRecipient.objects.filter(message_id=message_id).first()
    if recipient:
        recipient.record_open()
        logger.debug(f"Recorded open for {recipient.subscriber.email}")


def _handle_click_event(payload: Dict[str, Any]):
    """Handle link click event"""
    message_id = payload.get('message_id')
    url = payload.get('url')
    
    if not message_id or not url:
        return

    recipient = CampaignRecipient.objects.filter(message_id=message_id).first()
    if recipient:
        recipient.record_click(url)
        
        # Update link stats
        ClickLink.objects.filter(
            campaign=recipient.campaign,
            url=url
        ).update(click_count=F('click_count') + 1)
        
        logger.debug(f"Recorded click for {recipient.subscriber.email}")


def _handle_bounce_event(payload: Dict[str, Any]):
    """Handle bounce event"""
    email = payload.get('email')
    bounce_type = payload.get('bounce_type', 'soft')
    reason = payload.get('reason', '')

    subscriber = Subscriber.objects.filter(email=email).first()
    if not subscriber:
        return

    BounceReport.objects.create(
        subscriber=subscriber,
        bounce_type='hard' if bounce_type == 'hard' else 'soft',
        reason=reason[:500],
        raw_data=payload
    )

    if bounce_type == 'hard':
        subscriber.status = Subscriber.Status.BOUNCED
        subscriber.save(update_fields=['status'])
        logger.info(f"Hard bounce recorded for {email}")


def _handle_complaint_event(payload: Dict[str, Any]):
    """Handle spam complaint"""
    email = payload.get('email')
    
    subscriber = Subscriber.objects.filter(email=email).first()
    if subscriber:
        subscriber.status = Subscriber.Status.COMPLAINED
        subscriber.save(update_fields=['status'])
        logger.warning(f"Spam complaint recorded for {email}")


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    'ping',
    'test_task',
    'send_campaign',
    'process_webhook_event',
]