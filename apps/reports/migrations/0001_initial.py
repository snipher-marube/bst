import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('workspaces', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ReportJob',
            fields=[
                ('id',           models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('dashboard_id', models.UUIDField()),
                ('status',       models.CharField(choices=[('pending','Pending'),('running','Running'),('done','Done'),('failed','Failed')], default='pending', max_length=10)),
                ('error',        models.TextField(blank=True)),
                ('pdf_path',     models.CharField(blank=True, max_length=500)),
                ('share_token',  models.UUIDField(default=uuid.uuid4, unique=True)),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('workspace',    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='report_jobs', to='workspaces.workspace')),
                ('requested_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='reportjob',
            index=models.Index(fields=['workspace', 'dashboard_id', 'status'], name='rpt_ws_dash_status_idx'),
        ),
        migrations.AddIndex(
            model_name='reportjob',
            index=models.Index(fields=['share_token'], name='rpt_share_token_idx'),
        ),
    ]
