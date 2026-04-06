from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email_invites',  models.BooleanField(default=True,  help_text='Team invitation emails')),
                ('email_imports',  models.BooleanField(default=True,  help_text='Import job completion emails')),
                ('email_insights', models.BooleanField(default=False, help_text='New AI insight emails (can be frequent)')),
                ('email_system',   models.BooleanField(default=True,  help_text='Important system / account emails')),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='notification_prefs',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
