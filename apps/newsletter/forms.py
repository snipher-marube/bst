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
        }),
        validators=[EmailValidator()],
        error_messages={
            'required': 'Email address is required',
            'invalid': 'Please enter a valid email address',
        }
    )
    
    first_name = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'w-full sm:w-auto px-4 py-3 text-gray-900 placeholder-gray-500 bg-white rounded-lg focus:outline-none focus:ring-2 focus:ring-[#f68712] border-2 border-transparent',
            'placeholder': 'First name (optional)'
        })
    )
    
    consent = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={
            'class': 'rounded border-gray-300 text-[#f68712] focus:ring-[#f68712]'
        }),
        label="I agree to receive newsletters and accept the privacy policy",
        error_messages={
            'required': 'You must consent to receive newsletters'
        }
    )
    
    list_id = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput()
    )
    
    def clean_email(self):
        """Validate and normalize email"""
        email = self.cleaned_data['email'].lower().strip()
        
        # Check for disposable emails
        if self._is_disposable_email(email):
            raise forms.ValidationError(
                'Disposable email addresses are not allowed. Please use a permanent email address.'
            )
        
        return email
    
    def _is_disposable_email(self, email):
        """Check if email domain is disposable"""
        disposable_domains = getattr(settings, 'DISPOSABLE_EMAIL_DOMAINS', [])
        if disposable_domains:
            try:
                domain = email.split('@')[1]
                return domain in disposable_domains
            except IndexError:
                return False
        return False
    
    def clean(self):
        """Validate subscription status"""
        cleaned_data = super().clean()
        email = cleaned_data.get('email')
        
        if email:
            self._check_existing_subscription(email)
        
        return cleaned_data
    
    def _check_existing_subscription(self, email):
        """Check if email is already actively subscribed"""
        try:
            subscriber = Subscriber.objects.get(email=email)
            if subscriber.status == Subscriber.Status.ACTIVE:
                raise forms.ValidationError(
                    'This email is already subscribed to our newsletter.',
                    code='already_subscribed'
                )
        except Subscriber.DoesNotExist:
            pass

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