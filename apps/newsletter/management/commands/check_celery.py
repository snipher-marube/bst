from django.core.management.base import BaseCommand
from celery import current_app
from apps.newsletter.tasks import ping
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Check Celery worker status and task queue'

    def handle(self, *args, **options):
        self.stdout.write("🔍 Checking Celery status...")
        
        # Test task
        try:
            result = ping.delay()
            self.stdout.write(f"✅ Ping task sent: {result.id}")
            self.stdout.write(f"   Task ID: {result.id}")
            
            # Optional: Wait a moment and check result
            import time
            time.sleep(2)
            
            if result.successful():
                self.stdout.write(f"✅ Task completed successfully: {result.result}")
            elif result.failed():
                self.stdout.write(f"❌ Task failed: {result.traceback}")
            else:
                self.stdout.write(f"⏳ Task still pending...")
                
        except Exception as e:
            self.stdout.write(f"❌ Failed to send ping: {str(e)}")
        
        # Check workers - FIXED VERSION
        try:
            # Get inspect instance from current app
            i = current_app.control.inspect()
            
            # Get stats from all workers
            workers = i.stats()
            
            if workers:
                self.stdout.write(f"✅ Active workers found:")
                for worker_name, stats in workers.items():
                    self.stdout.write(f"   • {worker_name}")
                    self.stdout.write(f"     - Concurrency: {stats.get('pool', {}).get('max-concurrency', 'N/A')}")
                    self.stdout.write(f"     - Queues: {', '.join(stats.get('queues', []))}")
            else:
                self.stdout.write("❌ No active workers found")
                
            # Also check registered tasks
            registered = i.registered()
            if registered:
                self.stdout.write(f"\n📋 Registered tasks:")
                for worker_name, tasks in registered.items():
                    self.stdout.write(f"   • {worker_name}: {len(tasks)} tasks")
                    # Show first 5 tasks
                    for task in list(tasks)[:5]:
                        self.stdout.write(f"     - {task}")
                    if len(tasks) > 5:
                        self.stdout.write(f"     ... and {len(tasks) - 5} more")
                        
        except Exception as e:
            self.stdout.write(f"❌ Error checking workers: {str(e)}")
            self.stdout.write(f"   Make sure Celery worker is running with: celery -A config worker --loglevel=info")