from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import TemplateView, ListView, DetailView
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.urls import reverse
from .models import (
    Testimonial, FAQ, Feature, Statistic, Partner, 
    BlogPost, ContactMessage, SiteSettings
)
from .forms import ContactForm
import json


class HomeView(TemplateView):
    """
    Home page view with all sections
    """
    template_name = 'core/home.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get site settings
        context['site_settings'] = SiteSettings.get_settings()
        
        # Get active content
        context['features'] = Feature.objects.filter(is_active=True, is_featured=True)[:6]
        context['testimonials'] = Testimonial.objects.filter(is_active=True, featured=True)[:5]
        context['stats'] = Statistic.objects.filter(is_active=True)
        context['partners'] = Partner.objects.filter(is_active=True)[:8]
        context['faqs'] = FAQ.objects.filter(is_active=True)[:6]
        context['blog_posts'] = BlogPost.objects.filter(is_published=True)[:3]
        
        # SEO metadata
        context['meta_title'] = 'Business Sight Technologies - No-Code Analytics Platform for SMEs'
        context['meta_description'] = 'Build your own analytics dashboard without code. Track expenses, sales, inventory, and more with custom categories and automatic visualizations. Start free today.'
        context['meta_keywords'] = 'analytics, business intelligence, no-code, data tracking, SME, dashboard, custom analytics'
        context['og_image'] = 'images/og-home.jpg'
        
        # Forms
        context['newsletter_form'] = NewsletterForm()
        context['contact_form'] = ContactForm()
        
        return context


class AboutView(TemplateView):
    """
    About us page
    """
    template_name = 'core/about.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['stats'] = Statistic.objects.filter(is_active=True)
        context['team_members'] = []  # Add team members later
        context['testimonials'] = Testimonial.objects.filter(is_active=True)[:4]
        context['meta_title'] = 'About Business Sight Technologies - Our Story & Mission'
        return context


class FeaturesView(ListView):
    """
    Features overview page
    """
    model = Feature
    template_name = 'core/features.html'
    context_object_name = 'features'
    
    def get_queryset(self):
        return Feature.objects.filter(is_active=True)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['meta_title'] = 'Features - Business Sight Technologies Analytics Platform'
        return context


class FeatureDetailView(DetailView):
    """
    Individual feature detail page
    """
    model = Feature
    template_name = 'core/feature_detail.html'
    context_object_name = 'feature'
    slug_field = 'slug'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['meta_title'] = f"{self.object.title} - Business Sight Technologies Features"
        context['meta_description'] = self.object.short_description
        return context


class PricingView(TemplateView):
    """
    Pricing page
    """
    template_name = 'core/pricing.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['plans'] = [
            {
                'name': 'Free',
                'price': '$0',
                'period': 'month',
                'description': 'Perfect for getting started',
                'features': [
                    '5 categories',
                    '1,000 records',
                    'Basic charts',
                    'CSV export',
                    '1 user',
                ],
                'button_text': 'Get Started',
                'button_class': 'bg-gray-800 hover:bg-gray-900',
                'featured': False,
            },
            {
                'name': 'Starter',
                'price': '$15',
                'period': 'month',
                'description': 'For growing businesses',
                'features': [
                    '25 categories',
                    '10,000 records',
                    'All chart types',
                    'CSV import/export',
                    '3 users',
                    'Email support',
                ],
                'button_text': 'Start Free Trial',
                'button_class': 'bg-blue-600 hover:bg-blue-700',
                'featured': True,
            },
            {
                'name': 'Professional',
                'price': '$39',
                'period': 'month',
                'description': 'For advanced analytics',
                'features': [
                    '100 categories',
                    '100,000 records',
                    'Custom formulas',
                    'API access',
                    '10 users',
                    'Priority support',
                    'Advanced permissions',
                ],
                'button_text': 'Start Free Trial',
                'button_class': 'bg-gray-800 hover:bg-gray-900',
                'featured': False,
            },
            {
                'name': 'Enterprise',
                'price': 'Custom',
                'period': 'month',
                'description': 'For large organizations',
                'features': [
                    'Unlimited categories',
                    'Unlimited records',
                    'SSO & 2FA',
                    'Audit logs',
                    'SLA guarantee',
                    'Dedicated support',
                    'Custom integrations',
                ],
                'button_text': 'Contact Sales',
                'button_class': 'bg-gray-800 hover:bg-gray-900',
                'featured': False,
            },
        ]
        context['faqs'] = FAQ.objects.filter(is_active=True, category='pricing')[:8]
        context['meta_title'] = 'Pricing - MetaAnalytics Plans'
        return context


class ContactView(TemplateView):
    """
    Contact page
    """
    template_name = 'core/contact.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = ContactForm()
        context['meta_title'] = 'Contact Us - MetaAnalytics'
        return context
    
    def post(self, request, *args, **kwargs):
        form = ContactForm(request.POST)
        if form.is_valid():
            # Save to database
            contact = ContactMessage.objects.create(
                name=form.cleaned_data['name'],
                email=form.cleaned_data['email'],
                company=form.cleaned_data.get('company', ''),
                phone=form.cleaned_data.get('phone', ''),
                subject=form.cleaned_data['subject'],
                message=form.cleaned_data['message']
            )
            
            # Send email notification
            try:
                send_mail(
                    subject=f"New Contact Form: {contact.subject}",
                    message=f"""
                    Name: {contact.name}
                    Email: {contact.email}
                    Company: {contact.company}
                    Phone: {contact.phone}
                    
                    Message:
                    {contact.message}
                    """,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[settings.SUPPORT_EMAIL],
                    fail_silently=True,
                )
            except Exception as e:
                # Log error but don't break user experience
                print(f"Email send failed: {e}")
            
            messages.success(request, 'Thank you for contacting us! We\'ll respond within 24 hours.')
            return redirect('core:contact')
        
        context = self.get_context_data()
        context['form'] = form
        return self.render_to_response(context)


class BlogListView(ListView):
    """
    Blog listing page
    """
    model = BlogPost
    template_name = 'core/blog_list.html'
    context_object_name = 'posts'
    paginate_by = 9
    
    def get_queryset(self):
        return BlogPost.objects.filter(is_published=True)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['featured_post'] = BlogPost.objects.filter(is_published=True, is_featured=True).first()
        context['categories'] = BlogPost.objects.filter(is_published=True).values_list('category', flat=True).distinct()
        context['meta_title'] = 'Blog - MetaAnalytics Insights'
        return context


class BlogDetailView(DetailView):
    """
    Individual blog post
    """
    model = BlogPost
    template_name = 'core/blog_detail.html'
    context_object_name = 'post'
    slug_field = 'slug'
    
    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Increment view count
        obj.views += 1
        obj.save(update_fields=['views'])
        return obj
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        post = context.get('post') or self.get_object()
        # Get related posts
        context['related_posts'] = BlogPost.objects.filter(
            is_published=True,
            category=post.category
        ).exclude(id=post.id)[:3]
        
        # SEO metadata
        context['meta_title'] = post.meta_title or post.title
        context['meta_description'] = post.meta_description or post.excerpt
        context['og_image'] = post.featured_image.url if post.featured_image else None
        context['og_title'] = post.og_title or post.title
        context['og_description'] = post.og_description or post.excerpt
        
        return context


def sitemap_view(request):
    """
    XML sitemap for SEO
    """
    from django.contrib.sitemaps import Sitemap
    from django.contrib.sitemaps.views import sitemap
    
    class StaticViewSitemap(Sitemap):
        priority = 0.8
        changefreq = 'weekly'
        
        def items(self):
            return ['home', 'features', 'pricing', 'about', 'contact', 'blog_list']
        
        def location(self, item):
            return reverse(f'core:{item}')
    
    class BlogSitemap(Sitemap):
        priority = 0.6
        changefreq = 'monthly'
        
        def items(self):
            return BlogPost.objects.filter(is_published=True)
        
        def lastmod(self, obj):
            return obj.updated_at
    
    sitemaps = {
        'static': StaticViewSitemap,
        'blog': BlogSitemap,
    }
    
    return sitemap(request, sitemaps)