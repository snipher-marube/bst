# newsletter/tasks.py
import logging
import re
from typing import Optional, Union

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from .models import (
    Campaign, Subscriber, CampaignRecipient,
    ClickLink, BounceReport, WebhookEvent
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_campaign(self, campaign_id: int, test_mode: bool = False, test_email: Optional[str] = None):
    """
    Background task to send a campaign to all recipients with retry logic
    """
    try:
        campaign = Campaign.objects.select_related().get(
            id=campaign_id, 
            status__in=['draft', 'scheduled']
        )
    except Campaign.DoesNotExist:
        logger.error(f"Campaign {campaign_id} not found or not in draft/scheduled status")
        return

    # Update campaign status
    campaign.status = Campaign.Status.SENDING
    campaign.save(update_fields=['status'])

    try:
        if test_mode and test_email:
            _send_test_campaign(campaign, test_email)
        else:
            _send_bulk_campaign(campaign)
    except Exception as exc:
        # Retry on failure
        logger.error(f"Campaign {campaign_id} failed: {str(exc)}")
        campaign.status = Campaign.Status.DRAFT
        campaign.save(update_fields=['status'])
        self.retry(exc=exc)


@shared_task
def process_webhook_event(event_id: int):
    """
    Process webhook events from email service providers
    """
    try:
        event = WebhookEvent.objects.get(id=event_id, processed=False)
    except WebhookEvent.DoesNotExist:
        return

    handlers = {
        'open': _handle_open_event,
        'click': _handle_click_event,
        'bounce': _handle_bounce_event,
        'complaint': _handle_complaint_event,
    }

    handler = handlers.get(event.event_type)
    if handler:
        handler(event.payload)

    event.processed = True
    event.processed_at = timezone.now()
    event.save(update_fields=['processed', 'processed_at'])


def _send_test_campaign(campaign: Campaign, test_email: str):
    """Send a test campaign to a single email"""
    _send_single_email(campaign, None, test_email)
    
    logger.info(f"Test campaign {campaign.name} sent to {test_email}")


def _send_bulk_campaign(campaign: Campaign):
    """Send campaign to all active subscribers in batches"""
    subscribers = Subscriber.objects.filter(
        lists__in=campaign.lists.all(),
        status=Subscriber.Status.ACTIVE
    ).distinct().only('id', 'email', 'first_name', 'unsubscribe_token')

    total_recipients = subscribers.count()
    campaign.total_recipients = total_recipients
    campaign.save(update_fields=['total_recipients'])

    sent_count = 0
    batch_size = 100

    for subscriber in subscribers.iterator(chunk_size=batch_size):
        try:
            with transaction.atomic():
                _send_single_email(campaign, subscriber, subscriber.email)
                sent_count += 1

                # Update progress periodically
                if sent_count % batch_size == 0:
                    campaign.total_sent = sent_count
                    campaign.save(update_fields=['total_sent'])
                    logger.info(f"Campaign {campaign.name}: {sent_count}/{total_recipients} sent")

        except Exception as e:
            logger.error(f"Failed to send to {subscriber.email}: {str(e)}")
            _create_failed_recipient(campaign, subscriber, str(e))

    # Final update
    campaign.total_sent = sent_count
    campaign.status = Campaign.Status.SENT
    campaign.sent_at = timezone.now()
    campaign.save(update_fields=['total_sent', 'status', 'sent_at'])

    logger.info(f"Campaign {campaign.name} completed: {sent_count}/{total_recipients} sent")


def _send_single_email(campaign: Campaign, subscriber: Optional[Subscriber], email: str):
    """
    Send a single email with personalization and tracking
    """
    # Get or create recipient record
    recipient = None
    if subscriber:
        recipient, _ = CampaignRecipient.objects.get_or_create(
            campaign=campaign,
            subscriber=subscriber,
            defaults={'status': 'pending'}
        )

    # Prepare content
    html_content, text_content = _prepare_email_content(campaign, subscriber)

    # Create email message
    email_message = EmailMultiAlternatives(
        subject=campaign.subject,
        body=text_content,
        from_email=campaign.from_email or settings.DEFAULT_FROM_EMAIL,
        to=[email],
        reply_to=[campaign.reply_to] if campaign.reply_to else None,
        headers={
            'List-Unsubscribe': f'<{settings.SITE_URL}/newsletter/unsubscribe/>',
            'X-Campaign-ID': str(campaign.id),
            'X-Mailer': 'Django-Newsletter/1.0'
        }
    )
    email_message.attach_alternative(html_content, "text/html")

    # Send
    email_message.send(fail_silently=False)

    # Update recipient record
    if recipient:
        recipient.status = CampaignRecipient.Status.SENT
        recipient.sent_at = timezone.now()
        recipient.save(update_fields=['status', 'sent_at'])


def _prepare_email_content(campaign: Campaign, subscriber: Optional[Subscriber]) -> tuple:
    """
    Prepare HTML and plain text content with personalization and tracking
    """
    html_content = campaign.content_html
    text_content = campaign.content_text or strip_tags(html_content)

    if subscriber:
        # Add unsubscribe link
        unsubscribe_url = f"{settings.SITE_URL}/newsletter/unsubscribe/{subscriber.unsubscribe_token}/"
        unsubscribe_html = f'''
            <p style="font-size:12px;color:#666;margin-top:20px;">
                If you no longer wish to receive these emails, 
                <a href="{unsubscribe_url}" style="color:#f68712;">unsubscribe here</a>.
            </p>
        '''
        unsubscribe_text = f"\n\nTo unsubscribe: {unsubscribe_url}"
        
        html_content += unsubscribe_html
        text_content += unsubscribe_text

        # Add tracking pixel
        if campaign.track_opens:
            tracking_url = f"{settings.SITE_URL}/newsletter/track/open/{campaign.id}/{subscriber.id}/"
            html_content += f'<img src="{tracking_url}" width="1" height="1" alt="" style="display:none"/>'

        # Add click tracking
        if campaign.track_clicks:
            html_content = _process_click_tracking(html_content, campaign, subscriber)

    return html_content, text_content


def _process_click_tracking(html_content: str, campaign: Campaign, subscriber: Subscriber) -> str:
    """
    Replace links with tracking URLs
    """
    # Pattern to find href links
    pattern = r'href="([^"]+)"'

    def replace_link(match):
        original_url = match.group(1)
        
        # Skip mailto and anchor links
        if original_url.startswith(('mailto:', '#', 'tel:')):
            return match.group(0)

        # Create or update link record
        ClickLink.objects.get_or_create(
            campaign=campaign,
            url=original_url
        )

        # Create tracking URL
        tracking_url = (
            f"{settings.SITE_URL}/newsletter/track/click/"
            f"{campaign.id}/{subscriber.id}/?url={original_url}"
        )
        
        return f'href="{tracking_url}"'

    return re.sub(pattern, replace_link, html_content)


def _create_failed_recipient(campaign: Campaign, subscriber: Subscriber, error: str):
    """Create or update failed recipient record"""
    CampaignRecipient.objects.update_or_create(
        campaign=campaign,
        subscriber=subscriber,
        defaults={
            'status': CampaignRecipient.Status.FAILED,
            'error_message': error
        }
    )


def _handle_open_event(payload: dict):
    """Handle email open event"""
    message_id = payload.get('message_id')
    if not message_id:
        return

    recipient = CampaignRecipient.objects.filter(message_id=message_id).first()
    if recipient:
        recipient.record_open()


def _handle_click_event(payload: dict):
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
        ).update(click_count=models.F('click_count') + 1)


def _handle_bounce_event(payload: dict):
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
        reason=reason,
        raw_data=payload
    )

    # Auto-unsubscribe on hard bounce
    if bounce_type == 'hard':
        subscriber.status = Subscriber.Status.BOUNCED
        subscriber.save(update_fields=['status'])


def _handle_complaint_event(payload: dict):
    """Handle spam complaint"""
    email = payload.get('email')
    
    subscriber = Subscriber.objects.filter(email=email).first()
    if subscriber:
        subscriber.status = Subscriber.Status.COMPLAINED
        subscriber.save(update_fields=['status'])
        