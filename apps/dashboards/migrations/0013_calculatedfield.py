"""
Migration: add CalculatedField model.

CalculatedField stores reusable arithmetic expressions tied to a DataTable.
Expressions may reference column-level aggregations (sum, count, avg, min, max)
and are evaluated at query time via the CalculatedFieldEvaluator service.
"""
import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboards', '0012_datasource_google_sheets'),
    ]

    operations = [
        migrations.CreateModel(
            name='CalculatedField',
            fields=[
                ('id', models.UUIDField(
                    default=uuid.uuid4, editable=False, primary_key=True, serialize=False,
                )),
                ('table', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='calculated_fields',
                    to='dashboards.datatable',
                )),
                ('name', models.SlugField(
                    max_length=64,
                    help_text=(
                        'Identifier used to reference this field in expressions '
                        '(letters, digits, hyphens).'
                    ),
                )),
                ('display_name', models.CharField(max_length=128)),
                ('expression', models.TextField(
                    max_length=512,
                    help_text=(
                        'Arithmetic expression using sum(col), count(col), avg(col), '
                        'min(col), max(col). E.g.: sum(revenue) / count(user_id)'
                    ),
                )),
                ('format_type', models.CharField(
                    choices=[
                        ('number', 'Number'),
                        ('currency', 'Currency'),
                        ('percentage', 'Percentage'),
                        ('integer', 'Integer'),
                    ],
                    default='number',
                    max_length=20,
                )),
                ('description', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
                'unique_together': {('table', 'name')},
            },
        ),
    ]
