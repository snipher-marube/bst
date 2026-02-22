# newsletter/forms.py
from django import forms
from django.core.validators import EmailValidator
from django.conf import settings
from django.utils import timezone
from .models import Subscriber, Campaign, List
import json

class NewsletterSubscriptionForm(forms.Form):
    """Secure newsletter subscription form with validation"""
    
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'w-full px-4 py-3 text-gray-900 placeholder-gray-500 bg-white rounded-lg focus:outline-none focus:ring-2 focus:ring-[#f68712] border-2 border-transparent',
            'placeholder': 'Enter your email',
            'id': 'newsletter-email',
            'required': True
        }),
        validators=[EmailValidator()]
    )
    
    first_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'w-full px-4 py-3 text-gray-900 placeholder-gray-500 bg-white rounded-lg focus:outline-none focus:ring-2 focus:ring-[#f68712]',
            'placeholder': 'First name (optional)'
        })
    )
    
    consent = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={
            'class': 'rounded border-gray-300 text-[#f68712] focus:ring-[#f68712]'
        }),
        label="I agree to receive newsletters and accept the privacy policy"
    )
    
    list_id = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput()
    )
    
    def clean_email(self):
        """Additional email validation"""
        email = self.cleaned_data['email'].lower().strip()
        
        # Check for disposable emails if enabled
        if hasattr(settings, 'DISPOSABLE_EMAIL_DOMAINS'):
            domain = email.split('@')[1]
            if domain in settings.DISPOSABLE_EMAIL_DOMAINS:
                raise forms.ValidationError(
                    'Disposable email addresses are not allowed. Please use a permanent email address.'
                )
        
        return email
    
    def clean(self):
        cleaned_data = super().clean()
        
        # Check if already subscribed but unsubscribed
        email = cleaned_data.get('email')
        if email:
            try:
                subscriber = Subscriber.objects.get(email=email)
                if subscriber.status == Subscriber.Status.UNSUBSCRIBED:
                    # Allow resubscription
                    pass
                elif subscriber.status == Subscriber.Status.ACTIVE:
                    raise forms.ValidationError(
                        'This email is already subscribed to our newsletter.'
                    )
            except Subscriber.DoesNotExist:
                pass
        
        return cleaned_data


class CampaignForm(forms.ModelForm):
    """Form for creating/editing campaigns"""
    
    class Meta:
        model = Campaign
        fields = [
            'name', 'subject', 'preheader', 'content_html',
            'lists', 'scheduled_for', 'priority', 'track_opens',
            'track_clicks', 'reply_to', 'from_email'
        ]
        widgets = {
            'content_html': forms.Textarea(attrs={
                'class': 'w-full px-4 py-3 border rounded-lg font-mono',
                'rows': 20,
                'data-editor': 'html'
            }),
            'scheduled_for': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
                'class': 'w-full px-4 py-3 border rounded-lg'
            }),
            'lists': forms.CheckboxSelectMultiple(),
        }
    
    def clean_content_html(self):
        """Validate HTML content"""
        content = self.cleaned_data['content_html']
        
        # Basic security checks
        if '<?php' in content.lower():
            raise forms.ValidationError('PHP code is not allowed in email content')
        
        if '<script' in content.lower():
            raise forms.ValidationError('JavaScript is not allowed in email content for security')
        
        return content
    
    def clean_scheduled_for(self):
        """Validate scheduled date"""
        scheduled = self.cleaned_data.get('scheduled_for')
        if scheduled and scheduled < timezone.now():
            raise forms.ValidationError('Scheduled time must be in the future')
        return scheduled