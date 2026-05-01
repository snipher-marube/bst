from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insights", "0002_llm_fields"),
    ]

    operations = [
        # session_id links Ask AI turns together in the history panel
        migrations.AddField(
            model_name="insight",
            name="session_id",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        # question stores the raw user question for Ask AI insights
        migrations.AddField(
            model_name="insight",
            name="question",
            field=models.TextField(blank=True),
        ),
        # Index on session_id for fast history lookups
        migrations.AddIndex(
            model_name="insight",
            index=models.Index(fields=["session_id"], name="insights_session_id_idx"),
        ),
    ]
