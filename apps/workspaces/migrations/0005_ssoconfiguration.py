from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0004_workspace_onboarding"),
    ]

    operations = [
        migrations.CreateModel(
            name="SSOConfiguration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idp_entity_id",      models.CharField(max_length=500, help_text="IdP Entity ID / Issuer URI")),
                ("idp_sso_url",        models.URLField(max_length=500,  help_text="IdP SSO URL (HTTP-Redirect binding)")),
                ("idp_slo_url",        models.URLField(max_length=500, blank=True, default="", help_text="IdP SLO URL (optional)")),
                ("idp_x509_cert",      models.TextField(help_text="IdP X.509 certificate — base64 body only, no PEM headers")),
                ("sp_entity_id",       models.CharField(max_length=500, blank=True, default="", help_text="SP Entity ID (auto-generated if blank)")),
                ("attribute_email",      models.CharField(max_length=200, default="email")),
                ("attribute_first_name", models.CharField(max_length=200, default="first_name", blank=True)),
                ("attribute_last_name",  models.CharField(max_length=200, default="last_name",  blank=True)),
                ("is_active",      models.BooleanField(default=False)),
                ("require_sso",    models.BooleanField(default=False)),
                ("auto_provision", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "workspace",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sso_config",
                        to="workspaces.workspace",
                    ),
                ),
            ],
            options={"verbose_name": "SSO Configuration", "verbose_name_plural": "SSO Configurations"},
        ),
    ]
