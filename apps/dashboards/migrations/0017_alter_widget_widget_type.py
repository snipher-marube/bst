from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboards', '0016_widget_table_cascade_delete'),
    ]

    operations = [
        migrations.AlterField(
            model_name='widget',
            name='widget_type',
            field=models.CharField(
                choices=[
                    ('line_chart', 'Line Chart'),
                    ('bar_chart', 'Bar Chart'),
                    ('pie_chart', 'Pie Chart'),
                    ('histogram', 'Histogram'),
                    ('box_plot', 'Box Plot'),
                    ('table', 'Data Table'),
                    ('metric', 'Single Metric'),
                    ('number', 'Number Card'),
                    ('gauge', 'Gauge'),
                    ('heatmap', 'Heatmap'),
                    ('scatter', 'Scatter Plot'),
                    ('cohort', 'Cohort Retention'),
                    ('funnel', 'Funnel Analysis'),
                ],
                max_length=20,
            ),
        ),
    ]
