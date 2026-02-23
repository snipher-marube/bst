# newsletter/utils.py
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.urls import reverse
import csv
from io import StringIO
from django.http import HttpResponse
import logging
from django.utils import timezone

from .models import Subscriber, List

logger = logging.getLogger(__name__)

def send_confirmation_email(subscriber):
    """
    Send double opt-in confirmation email
    """
    context = {
        'subscriber': subscriber,
        'confirmation_url': _get_confirmation_url(subscriber),
        'site_name': settings.SITE_NAME,
        'support_email': getattr(settings, 'SUPPORT_EMAIL', 'support@bst.com'),
        'current_year': timezone.now().year,
    }
    
    html_message = render_to_string('newsletter/email_templates/welcome_email.html', context)
    plain_message = strip_tags(html_message)
    
    try:
        send_mail(
            subject=f"Please confirm your subscription to {settings.SITE_NAME}",
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[subscriber.email],
            html_message=html_message,
            fail_silently=False
        )
        logger.info(f"Confirmation email sent to {subscriber.email}")
    except Exception as e:
        logger.error(f"Failed to send confirmation email to {subscriber.email}: {str(e)}")
        raise


def _get_confirmation_url(subscriber):
    """Generate confirmation URL"""
    return f"{settings.SITE_URL.rstrip('/')}{reverse('newsletter:confirm', args=[subscriber.verification_token])}"


def export_subscribers_csv(queryset):
    """
    Export subscribers to CSV
    """
    output = StringIO()
    writer = csv.writer(output)
    
    # Write headers
    writer.writerow([
        'Email', 'First Name', 'Last Name', 'Status',
        'Subscribed Date', 'Confirmed Date', 'Total Opens', 'Total Clicks'
    ])
    
    # Write data
    for subscriber in queryset.select_related().prefetch_related('lists'):
        writer.writerow([
            subscriber.email,
            subscriber.first_name,
            subscriber.last_name,
            subscriber.get_status_display(),
            subscriber.subscribed_at.strftime('%Y-%m-%d %H:%M:%S') if subscriber.subscribed_at else '',
            subscriber.confirmed_at.strftime('%Y-%m-%d %H:%M:%S') if subscriber.confirmed_at else '',
            subscriber.total_opens,
            subscriber.total_clicks
        ])
    
    # Create response
    response = HttpResponse(output.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="subscribers_{}.csv"'.format(
        timezone.now().strftime('%Y%m%d_%H%M%S')
    )
    
    return response


def get_or_create_list(slug='analytics-newsletter', defaults=None):
    """
    Get or create a list by slug
    """
    if defaults is None:
        defaults = {
            'name': 'Analytics Newsletter',
            'description': 'Newsletter for analytics tips, features, and updates',
            'is_public': True,
        }
    
    list_obj, created = List.objects.get_or_create(
        slug=slug,
        defaults=defaults
    )
    
    if created:
        logger.info(f"Created new list: {list_obj.name}")
    
    return list_obj, created


def generate_unsubscribe_link(subscriber, campaign=None):
    """
    Generate unsubscribe link with optional campaign tracking
    """
    base_url = f"{settings.SITE_URL.rstrip('/')}/newsletter/unsubscribe/{subscriber.unsubscribe_token}/"
    if campaign:
        return f"{base_url}?campaign={campaign.id}"
    return base_url


def get_subscriber_stats(subscriber):
    """
    Get comprehensive subscriber statistics
    """
    from django.db.models import Count, Q
    
    campaigns = subscriber.campaign_recipients.all()
    
    return {
        'total_campaigns': campaigns.count(),
        'opens': subscriber.total_opens,
        'clicks': subscriber.total_clicks,
        'lists_count': subscriber.lists.count(),
        'last_opened': subscriber.last_opened,
        'last_clicked': subscriber.last_clicked,
        'campaigns_received': campaigns.filter(status='sent').count(),
        'bounce_count': subscriber.bounce_reports.count(),
    }