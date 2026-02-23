# newsletter/context_processors.py
from .models import List
import logging

logger = logging.getLogger(__name__)

def newsletter_lists(request):
    """
    Make newsletter lists available in templates
    """
    context = {
        'analytics_list_id': None,
        'analytics_list': None,
        'newsletter_lists': [],
    }
    
    try:
        # Get or create analytics list
        analytics_list, created = List.objects.get_or_create(
            slug='analytics-newsletter',
            defaults={
                'name': 'Analytics Newsletter',
                'description': 'Newsletter for analytics tips, features, and updates',
                'is_public': True,
            }
        )
        
        if created:
            logger.info("Created analytics newsletter list via context processor")
        
        context.update({
            'analytics_list_id': analytics_list.id,
            'analytics_list': analytics_list,
            'newsletter_lists': List.objects.filter(is_public=True),
        })
        
    except Exception as e:
        logger.error(f"Error in newsletter_lists context processor: {str(e)}")
    
    return context