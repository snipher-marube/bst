from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0003_alter_workspace_members_alter_workspace_owner'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspace',
            name='industry',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='workspace',
            name='onboarding_completed',
            field=models.BooleanField(default=False),
        ),
    ]
