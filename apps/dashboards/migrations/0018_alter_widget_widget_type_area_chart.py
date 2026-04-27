from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboards", "0017_alter_widget_widget_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="widget",
            name="widget_type",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("line_chart", "Line Chart"),
                    ("area_chart", "Area Chart"),
                    ("bar_chart", "Bar Chart"),
                    ("pie_chart", "Pie Chart"),
                    ("histogram", "Histogram"),
                    ("box_plot", "Box Plot"),
                    ("table", "Data Table"),
                    ("metric", "Single Metric"),
                    ("number", "Number Card"),
                    ("gauge", "Gauge"),
                    ("heatmap", "Heatmap"),
                    ("scatter", "Scatter Plot"),
                    ("cohort", "Cohort Retention"),
                    ("funnel", "Funnel Analysis"),
                ],
            ),
        ),
    ]
