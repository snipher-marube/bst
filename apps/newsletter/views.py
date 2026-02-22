# newsletter/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache
from django.views.decorators.cache import cache_page
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django_ratelimit.decorators import ratelimit
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
import json
import logging
import hashlib
from .models import (
    Subscriber, Campaign, CampaignRecipient,
    ClickLink, List, BounceReport
)
from .forms import NewsletterSubscriptionForm
from .tasks import send_campaign, process_webhook_event
from .utils import send_confirmation_email

logger = logging.getLogger(__name__)

@require_POST
@ratelimit(key='ip', rate='5/h', method='POST', block=True)
def newsletter_subscribe(request):
    """
    Handle newsletter subscription with rate limiting and validation
    """
    try:
        data = json.loads(request.body) if request.body else request.POST
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid request format'
        }, status=400)
    
    form = NewsletterSubscriptionForm(data)
    
    if not form.is_valid():
        return JsonResponse({
            'success': False,
            'error': form.errors.get('email', ['Invalid email'])[0]
        }, status=400)
    
    email = form.cleaned_data['email'].lower()
    first_name = form.cleaned_data.get('first_name', '')
    
    # Check for existing subscriber
    subscriber, created = Subscriber.objects.get_or_create(
        email=email,
        defaults={
            'first_name': first_name,
            'status': Subscriber.Status.PENDING,
            'ip_address': request.META.get('REMOTE_ADDR'),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            'consent_given': form.cleaned_data.get('consent', False),
            'consent_ip': request.META.get('REMOTE_ADDR'),
            'consent_date': timezone.now(),
            'source': Subscriber.Source.WEBSITE
        }
    )
    
    if not created:
        if subscriber.status == Subscriber.Status.ACTIVE:
            return JsonResponse({
                'success': False,
                'error': 'This email is already subscribed to our newsletter.'
            }, status=400)
        elif subscriber.status == Subscriber.Status.UNSUBSCRIBED:
            # Reactivate
            subscriber.status = Subscriber.Status.PENDING
            subscriber.save(update_fields=['status'])
    
    # Send confirmation email
    try:
        send_confirmation_email(subscriber)
    except Exception as e:
        logger.error(f"Failed to send confirmation email to {email}: {str(e)}")
        # Still return success to user, but log error
    
    # Add to default list if specified
    list_id = form.cleaned_data.get('list_id')
    if list_id:
        try:
            email_list = List.objects.get(id=list_id, is_public=True)
            email_list.subscribers.add(subscriber)
        except List.DoesNotExist:
            pass
    
    return JsonResponse({
        'success': True,
        'message': 'Please check your email to confirm your subscription.'
    })


@require_GET
def confirm_subscription(request, token):
    """
    Confirm subscription via email link
    """
    subscriber = get_object_or_404(Subscriber, verification_token=token)
    
    if subscriber.status == Subscriber.Status.PENDING:
        subscriber.confirm_subscription()
        messages.success(request, 'Your subscription has been confirmed! Thank you.')
    elif subscriber.status == Subscriber.Status.ACTIVE:
        messages.info(request, 'Your subscription is already active.')
    else:
        messages.warning(request, f'Unable to confirm subscription. Status: {subscriber.get_status_display()}')
    
    # Redirect to home page or custom success page
    return redirect(settings.NEWSLETTER_CONFIRM_REDIRECT or '/')


@require_GET
def unsubscribe(request, token):
    """
    Handle unsubscribe requests
    """
    subscriber = get_object_or_404(Subscriber, unsubscribe_token=token)
    
    if subscriber.status != Subscriber.Status.UNSUBSCRIBED:
        subscriber.unsubscribe()
        
        # Record which campaign caused unsubscribe if provided
        campaign_id = request.GET.get('campaign')
        if campaign_id:
            try:
                recipient = CampaignRecipient.objects.get(
                    campaign_id=campaign_id,
                    subscriber=subscriber
                )
                recipient.status = CampaignRecipient.Status.UNSUBSCRIBED
                recipient.save(update_fields=['status'])
            except CampaignRecipient.DoesNotExist:
                pass
        
        messages.success(request, 'You have been unsubscribed successfully.')
    else:
        messages.info(request, 'You are already unsubscribed.')
    
    return render(request, 'newsletter/unsubscribe_confirmed.html', {
        'subscriber': subscriber
    })


@require_GET
@cache_page(60 * 5)  # Cache for 5 minutes
def track_open(request, campaign_id, subscriber_id=None):
    """
    Track email opens via tracking pixel
    """
    try:
        if subscriber_id:
            recipient = CampaignRecipient.objects.get(
                campaign_id=campaign_id,
                subscriber_id=subscriber_id
            )
            recipient.record_open()
    except CampaignRecipient.DoesNotExist:
        # Still return 1x1 pixel
        pass
    
    # Return 1x1 transparent GIF
    pixel = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
    return HttpResponse(pixel, content_type='image/gif')


@require_GET
def track_click(request, campaign_id, subscriber_id):
    """
    Track link clicks and redirect to destination
    """
    url = request.GET.get('url')
    if not url:
        raise Http404("No URL specified")
    
    try:
        recipient = CampaignRecipient.objects.get(
            campaign_id=campaign_id,
            subscriber_id=subscriber_id
        )
        recipient.record_click(url)
        
        # Update link stats
        try:
            link = ClickLink.objects.get(campaign_id=campaign_id, url=url)
            link.click_count += 1
            link.save(update_fields=['click_count'])
        except ClickLink.DoesNotExist:
            pass
            
    except CampaignRecipient.DoesNotExist:
        # Still redirect even if tracking fails
        pass
    
    return redirect(url)


@require_POST
@csrf_exempt
def webhook(request, provider):
    """
    Handle webhooks from email service providers
    """
    from .models import WebhookEvent
    
    try:
        payload = json.loads(request.body)
        
        # Store webhook event for async processing
        event = WebhookEvent.objects.create(
            provider=provider,
            event_type=payload.get('event', 'unknown'),
            payload=payload,
            processed=False
        )
        
        # Queue for processing
        process_webhook_event.enqueue(event.id)
        
        return JsonResponse({'success': True, 'event_id': event.id})
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def campaign_preview(request, campaign_id):
    """
    Preview campaign HTML in browser
    """
    campaign = get_object_or_404(Campaign, id=campaign_id)
    
    # Only allow preview for draft campaigns or staff users
    if campaign.status != 'draft' and not request.user.is_staff:
        raise Http404
    
    return render(request, 'newsletter/email_templates/campaign_template.html', {
        'campaign': campaign,
        'preview_mode': True
    })