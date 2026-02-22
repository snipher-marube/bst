# newsletter/management/commands/send_campaigns.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from newsletter.models import Campaign
from newsletter.tasks import send_campaign
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Send scheduled campaigns'
    
    def handle(self, *args, **options):
        now = timezone.now()
        
        # Get campaigns scheduled for now or past
        campaigns = Campaign.objects.filter(
            status='scheduled',
            scheduled_for__lte=now
        )
        
        for campaign in campaigns:
            self.stdout.write(f"Sending campaign: {campaign.name}")
            send_campaign.enqueue(campaign.id)
        
        self.stdout.write(f"Queued {campaigns.count()} campaigns")