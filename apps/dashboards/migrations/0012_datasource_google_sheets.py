"""
Migration: add Google Sheets connector support to DataSource.

Changes:
  1. AlterField host / database / username  → add blank=True, default=''
     (they are not required for Google Sheets).
  2. AlterField connector_type              → expand choices list.
  3. AddField  google_credentials_enc       → Fernet-encrypted service-account JSON.
  4. AddField  google_spreadsheet_id        → sheet ID from the URL.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboards", "0011_rename_password_enc_datasource__password_and_more"),
    ]

    operations = [
        # Make DB-specific fields optional so Google Sheets rows can be blank.
        migrations.AlterField(
            model_name="datasource",
            name="host",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="datasource",
            name="database",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="datasource",
            name="username",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        # Expand the connector_type choices to include Google Sheets.
        migrations.AlterField(
            model_name="datasource",
            name="connector_type",
            field=models.CharField(
                choices=[
                    ("postgresql",    "PostgreSQL"),
                    ("mysql",         "MySQL"),
                    ("google_sheets", "Google Sheets"),
                ],
                default="postgresql",
                max_length=20,
            ),
        ),
        # New fields for Google Sheets.
        migrations.AddField(
            model_name="datasource",
            name="google_credentials_enc",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="datasource",
            name="google_spreadsheet_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
