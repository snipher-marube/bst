from django.conf import settings
from .models import SiteSettings
from apps.dashboards.models import Dashboard


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
    if request.user.is_authenticated:
        # Avoid circular imports or property issues by getting workspace directly
        from apps.dashboards.models import Workspace
        workspace_id = request.session.get('current_workspace_id')
        if workspace_id:
            insights_dashboard = Dashboard.objects.filter(
                workspace_id=workspace_id,
                slug='workspace-overview',
                is_active=True
            ).first()

    return {
        'insights_dashboard': insights_dashboard,
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
                {'title': 'Integrations', 'url': '/integrations/'},
                {'title': 'API', 'url': '/api/docs/'},
                {'title': 'Roadmap', 'url': '/roadmap/'},
            ],
            'solutions': [
                {'title': 'Small Business', 'url': '/solutions/small-business/'},
                {'title': 'Agriculture', 'url': '/solutions/agriculture/'},
                {'title': 'Personal Finance', 'url': '/solutions/personal/'},
                {'title': 'E-commerce', 'url': '/solutions/ecommerce/'},
                {'title': 'Non-profit', 'url': '/solutions/nonprofit/'},
            ],
            'resources': [
                {'title': 'Documentation', 'url': '/docs/'},
                {'title': 'Tutorials', 'url': '/tutorials/'},
                {'title': 'Blog', 'url': '/blog/'},
                {'title': 'Case Studies', 'url': '/case-studies/'},
                {'title': 'Webinars', 'url': '/webinars/'},
            ],
            'company': [
                {'title': 'About Us', 'url': '/about/'},
                {'title': 'Careers', 'url': '/careers/'},
                {'title': 'Press', 'url': '/press/'},
                {'title': 'Contact', 'url': '/contact/'},
                {'title': 'Legal', 'url': '/legal/'},
            ],
        }
    }