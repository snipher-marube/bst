from django.conf import settings
from .models import SiteSettings
from apps.dashboards.models import Dashboard
from apps.notifications.models import Notification


def site_settings(request):
    """
    Context processor to inject site settings from database
    """
    settings = SiteSettings.get_settings()
    
    return {
        'site_settings': settings,  # This provides the actual model instance
        'site_name': settings.site_name if settings else 'Businessight',
        'support_email': settings.support_email if settings else 'support@businessight.com',
        'phone_number': settings.phone if settings else '+254 798 393 182',
        'address': settings.address if settings else '123 Analytics Ave, Nairobi',
        'social_media': {
            'twitter': settings.twitter_url if settings else 'https://twitter.com/businessight',
            'linkedin': settings.linkedin_url if settings else 'https://linkedin.com/company/businessight',
            'github': settings.github_url if settings else 'https://github.com/businessight',
            'youtube': settings.youtube_url if settings else 'https://youtube.com/@businessight',
        }
    }


def navigation(request):
    """
    Context processor for navigation menus
    """
    insights_dashboard = None
    unread_notifications_count = 0
    if request.user.is_authenticated:
        workspace_id = request.session.get('current_workspace_id')
        if workspace_id:
            insights_dashboard = Dashboard.objects.filter(
                workspace_id=workspace_id,
                slug='workspace-overview',
                is_active=True
            ).first()
        unread_notifications_count = Notification.objects.filter(
            user=request.user,
            is_read=False,
        ).count()

    return {
        'insights_dashboard': insights_dashboard,
        'unread_notifications_count': unread_notifications_count,
        'main_nav': [
            {'title': 'Home', 'url': '/', 'active': request.path == '/'},
            {'title': 'Features', 'url': '/features/', 'active': request.path.startswith('/features')},
            {'title': 'Pricing', 'url': '/pricing/', 'active': request.path.startswith('/pricing')},
            {'title': 'About', 'url': '/about/', 'active': request.path.startswith('/about')},
            {'title': 'Blog', 'url': '/blog/', 'active': request.path.startswith('/blog')},
            {'title': 'Contact', 'url': '/contact/', 'active': request.path.startswith('/contact')},
        ],
        'footer_nav': {
            'product': [
                {'title': 'Features', 'url': '/features/'},
                {'title': 'Pricing', 'url': '/pricing/'},
                {'title': 'Blog', 'url': '/blog/'},
            ],
            'company': [
                {'title': 'About Us', 'url': '/about/'},
                {'title': 'Contact', 'url': '/contact/'},
            ],
        }
    }