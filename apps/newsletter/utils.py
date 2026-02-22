# newsletter/utils.py
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.urls import reverse
import csv
from io import StringIO
from django.http import HttpResponse


def send_confirmation_email(subscriber):
    """
    Send double opt-in confirmation email
    """
    subject = f"Please confirm your subscription to {settings.SITE_NAME}"
    
    confirmation_url = f"{settings.SITE_URL}{reverse('newsletter:confirm', args=[subscriber.verification_token])}"
    
    html_message = render_to_string('newsletter/email_templates/welcome_email.html', {
        'subscriber': subscriber,
        'confirmation_url': confirmation_url,
        'site_name': settings.SITE_NAME
    })
    
    plain_message = strip_tags(html_message)
    
    send_mail(
        subject=subject,
        message=plain_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[subscriber.email],
        html_message=html_message,
        fail_silently=False
    )


def export_subscribers_csv(queryset):
    """
    Export subscribers to CSV
    """
    output = StringIO()
    writer = csv.writer(output)
    
    # Write headers
    writer.writerow([
        'Email', 'First Name', 'Last Name', 'Status',
        'Subscribed Date', 'Total Opens', 'Total Clicks'
    ])
    
    # Write data
    for subscriber in queryset:
        writer.writerow([
            subscriber.email,
            subscriber.first_name,
            subscriber.last_name,
            subscriber.get_status_display(),
            subscriber.subscribed_at.strftime('%Y-%m-%d %H:%M:%S'),
            subscriber.total_opens,
            subscriber.total_clicks
        ])
    
    # Create response
    response = HttpResponse(output.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="subscribers.csv"'
    
    return response


def generate_unsubscribe_link(subscriber, campaign=None):
    """
    Generate unsubscribe link with optional campaign tracking
    """
    base_url = f"{settings.SITE_URL}/newsletter/unsubscribe/{subscriber.unsubscribe_token}/"
    if campaign:
        return f"{base_url}?campaign={campaign.id}"
    return base_url