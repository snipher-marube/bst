from django.conf import settings


def site_settings(request):
    """
    Context processor to inject site-wide settings into all templates
    """
    return {
        'site_name': 'Business Sight Technologies',
        'site_description': 'Custom Analytics Platform for SMEs - Build your own tracking system without code',
        'site_keywords': 'analytics, business intelligence, data tracking, SME analytics, custom dashboards, no-code analytics',
        'site_author': 'Snipher Marube',
        'site_url': request.build_absolute_uri('/')[:-1],
        'current_year': request.now.year if hasattr(request, 'now') else 2024,
        'support_email': settings.SUPPORT_EMAIL if hasattr(settings, 'SUPPORT_EMAIL') else 'support@businesssight.com',
        'sales_email': settings.SALES_EMAIL if hasattr(settings, 'SALES_EMAIL') else 'sales@businesssight.com',
        'phone_number': settings.PHONE_NUMBER if hasattr(settings, 'PHONE_NUMBER') else '+254 798 393 182',
        'address': settings.ADDRESS if hasattr(settings, 'ADDRESS') else '123 Analytics Ave, Nairobi, CA 94105',
        'social_media': {
            'twitter': 'https://twitter.com/businesssight',
            'linkedin': 'https://linkedin.com/company/businesssight',
            'github': 'https://github.com/businesssight',
            'youtube': 'https://youtube.com/@businesssight',
        }
    }


def navigation(request):
    """
    Context processor for navigation menus
    """
    return {
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