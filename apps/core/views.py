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
from apps.newsletter.forms import NewsletterSubscriptionForm

# apps/core/views.py
from django.shortcuts import render
from django.http import JsonResponse
from django.core.cache import cache
from django.db import connections
from django.db.utils import OperationalError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import redis
import os
import sys
from datetime import datetime
import platform

def health_check(request):
    """
    Comprehensive health check endpoint for container orchestration.
    Checks database, cache/Redis, and basic application status.
    """
    health_data = {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {},
        'system': {}
    }
    
    overall_status = 'ok'
    
    # 1. Check Database connection
    try:
        db_conn = connections['default']
        db_conn.cursor()
        health_data['services']['database'] = {
            'status': 'ok',
            'message': 'Database connection successful',
            'engine': db_conn.vendor
        }
    except OperationalError as e:
        overall_status = 'error'
        health_data['services']['database'] = {
            'status': 'error',
            'message': str(e),
            'engine': 'unknown'
        }
    except Exception as e:
        overall_status = 'error'
        health_data['services']['database'] = {
            'status': 'error',
            'message': f'Unexpected error: {str(e)}',
            'engine': 'unknown'
        }
    
    # 2. Check Redis/Cache connection
    try:
        cache.set('health_check_key', 'ok', timeout=5)
        cached_value = cache.get('health_check_key')
        if cached_value == 'ok':
            health_data['services']['cache'] = {
                'status': 'ok',
                'message': 'Cache connection successful',
                'backend': cache.__class__.__name__
            }
        else:
            raise Exception('Cache value mismatch')
    except redis.ConnectionError as e:
        overall_status = 'error'
        health_data['services']['cache'] = {
            'status': 'error',
            'message': f'Redis connection error: {str(e)}',
            'backend': cache.__class__.__name__
        }
    except Exception as e:
        overall_status = 'error'
        health_data['services']['cache'] = {
            'status': 'error',
            'message': f'Cache error: {str(e)}',
            'backend': cache.__class__.__name__
        }
    
    # 3. Check Redis direct connection (more detailed)
    try:
        import redis
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        redis_client = redis.from_url(redis_url)
        redis_client.ping()
        
        health_data['services']['redis'] = {
            'status': 'ok',
            'message': 'Redis connection successful',
            'url': redis_url.replace('redis://', '')  # Hide credentials
        }
    except Exception as e:
        health_data['services']['redis'] = {
            'status': 'error' if overall_status == 'ok' else 'degraded',
            'message': f'Redis direct connection error: {str(e)}',
            'url': 'Not available'
        }
    
    # 4. System information
    health_data['system'] = {
        'python_version': sys.version.split()[0],
        'platform': platform.platform(),
        'django_version': __import__('django').get_version(),
        'environment': os.environ.get('DJANGO_SETTINGS_MODULE', 'unknown'),
        'debug': __import__('django.conf').settings.DEBUG,
    }
    
    # 5. Application version (if available)
    try:
        from django.conf import settings
        health_data['system']['site_name'] = getattr(settings, 'SITE_NAME', 'AnalyticsMeta')
    except:
        pass
    
    # 6. Celery status (optional - checks if celery worker is responsive)
    try:
        from celery import current_app
        inspect = current_app.control.inspect()
        active_workers = inspect.ping()
        if active_workers:
            health_data['services']['celery'] = {
                'status': 'ok',
                'message': f"Active workers: {len(active_workers)}",
                'workers': list(active_workers.keys())
            }
        else:
            health_data['services']['celery'] = {
                'status': 'warning',
                'message': 'No active Celery workers detected'
            }
    except Exception as e:
        health_data['services']['celery'] = {
            'status': 'warning',
            'message': f'Celery check failed: {str(e)}'
        }
    
    # 7. Disk space check (optional)
    try:
        import shutil
        disk_usage = shutil.disk_usage('/')
        free_gb = disk_usage.free / (1024 ** 3)
        total_gb = disk_usage.total / (1024 ** 3)
        health_data['system']['disk'] = {
            'total_gb': round(total_gb, 2),
            'free_gb': round(free_gb, 2),
            'free_percent': round((disk_usage.free / disk_usage.total) * 100, 2)
        }
        if free_gb < 1:  # Less than 1GB free
            overall_status = 'warning'
            health_data['system']['disk']['status'] = 'low_disk_space'
    except Exception:
        pass
    
    # Set overall status
    health_data['status'] = overall_status
    
    # Determine HTTP status code
    status_code = 200 if overall_status == 'ok' else (503 if overall_status == 'error' else 200)
    
    # Return JSON response
    return JsonResponse(health_data, status=status_code)


def health_check_html(request):
    """
    HTML version of health check for human-readable output.
    """
    from django.core.cache import cache
    from django.db import connections
    import redis
    
    health_context = {
        'timestamp': datetime.utcnow().isoformat(),
        'database_status': 'unknown',
        'database_error': None,
        'cache_status': 'unknown',
        'cache_error': None,
        'redis_status': 'unknown',
        'redis_error': None,
        'celery_status': 'unknown',
        'celery_error': None,
        'system_info': {},
        'overall_status': 'unknown'
    }
    
    # Check Database
    try:
        connections['default'].cursor()
        health_context['database_status'] = 'healthy'
    except Exception as e:
        health_context['database_status'] = 'unhealthy'
        health_context['database_error'] = str(e)
        health_context['overall_status'] = 'unhealthy'
    
    # Check Cache
    try:
        cache.set('health', 'ok', 5)
        if cache.get('health') == 'ok':
            health_context['cache_status'] = 'healthy'
    except Exception as e:
        health_context['cache_status'] = 'unhealthy'
        health_context['cache_error'] = str(e)
        health_context['overall_status'] = 'unhealthy'
    
    # Check Redis directly
    try:
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        redis_client = redis.from_url(redis_url)
        redis_client.ping()
        health_context['redis_status'] = 'healthy'
        health_context['redis_url'] = redis_url.replace('redis://', '')
    except Exception as e:
        health_context['redis_status'] = 'unhealthy'
        health_context['redis_error'] = str(e)
        health_context['overall_status'] = 'unhealthy'
    
    # Check Celery
    try:
        from celery import current_app
        inspect = current_app.control.inspect()
        active_workers = inspect.ping()
        if active_workers:
            health_context['celery_status'] = 'healthy'
            health_context['celery_workers'] = len(active_workers)
        else:
            health_context['celery_status'] = 'warning'
            health_context['celery_message'] = 'No active workers'
    except Exception as e:
        health_context['celery_status'] = 'unavailable'
        health_context['celery_error'] = str(e)
    
    # System info
    health_context['system_info'] = {
        'python_version': sys.version.split()[0],
        'django_version': __import__('django').get_version(),
        'platform': platform.platform(),
        'environment': os.environ.get('DJANGO_SETTINGS_MODULE', 'unknown'),
    }
    
    # If no errors found, overall is healthy
    if not health_context.get('overall_status'):
        health_context['overall_status'] = 'healthy'
    
    # Status code
    status_code = 200 if health_context['overall_status'] == 'healthy' else 503
    
    # Render template
    return render(request, 'health.html', health_context, status=status_code)


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def liveness_probe(request):
    """
    Simple liveness probe for Kubernetes/Docker.
    Returns 200 if the application is running.
    """
    return JsonResponse({
        'status': 'alive',
        'timestamp': datetime.utcnow().isoformat()
    }, status=200)


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def readiness_probe(request):
    """
    Readiness probe for Kubernetes/Docker.
    Checks if the application is ready to serve traffic.
    """
    # Check database connectivity
    try:
        from django.db import connections
        connections['default'].cursor()
        db_ok = True
    except:
        db_ok = False
    
    # Check cache connectivity
    try:
        from django.core.cache import cache
        cache.set('readiness', 'ok', 2)
        cache_ok = cache.get('readiness') == 'ok'
    except:
        cache_ok = False
    
    if db_ok and cache_ok:
        return JsonResponse({
            'status': 'ready',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {
                'database': 'ok',
                'cache': 'ok'
            }
        }, status=200)
    else:
        return JsonResponse({
            'status': 'not_ready',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {
                'database': 'ok' if db_ok else 'failed',
                'cache': 'ok' if cache_ok else 'failed'
            }
        }, status=503)


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
        context['newsletter_form'] = NewsletterSubscriptionForm()
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
    Public pricing page.
    Authenticated users are redirected to their billing page so they can pay
    immediately without re-reading marketing copy.
    """
    template_name = 'core/pricing.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect('dashboard:billing')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        signup_url = reverse('account_signup')
        context['plans'] = [
            {
                'tier': 'free',
                'name': 'Free',
                'price': 'KES 0',
                'period': 'mo',
                'description': 'Perfect for getting started — no card required.',
                'features': [
                    '5 tables',
                    '1,000 records per table',
                    '1 team member',
                    'Auto-generated dashboards',
                    'CSV / Excel import',
                ],
                'cta': 'Get Started Free',
                'cta_url': signup_url,
                'featured': False,
                'badge': None,
            },
            {
                'tier': 'starter',
                'name': 'Starter',
                'price': 'KES 2,500',
                'period': 'mo',
                'description': 'For small teams ready to grow.',
                'features': [
                    '20 tables',
                    '10,000 records per table',
                    '5 team members',
                    'All Free features',
                    'Real-time collaboration',
                    'AI insights',
                ],
                'cta': 'Start with Starter',
                'cta_url': f"{signup_url}?plan=starter",
                'featured': False,
                'badge': None,
            },
            {
                'tier': 'professional',
                'name': 'Professional',
                'price': 'KES 6,500',
                'period': 'mo',
                'description': 'Advanced analytics for power users.',
                'features': [
                    '100 tables',
                    '100,000 records per table',
                    '20 team members',
                    'All Starter features',
                    'Priority support',
                    'Advanced widgets & gauges',
                ],
                'cta': 'Go Professional',
                'cta_url': f"{signup_url}?plan=professional",
                'featured': True,
                'badge': 'Most Popular',
            },
            {
                'tier': 'enterprise',
                'name': 'Enterprise',
                'price': 'KES 12,900',
                'period': 'mo',
                'description': 'Unlimited scale for large organisations.',
                'features': [
                    'Unlimited tables',
                    'Unlimited records',
                    'Unlimited members',
                    'All Pro features',
                    'SSO / SAML',
                    'Dedicated success manager',
                    'SLA guarantee',
                ],
                'cta': 'Get Enterprise',
                'cta_url': f"{signup_url}?plan=enterprise",
                'featured': False,
                'badge': None,
            },
        ]
        context['faqs'] = FAQ.objects.filter(is_active=True, category='pricing')[:8]
        context['meta_title'] = 'Pricing – AnalyticsMeta Plans'
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