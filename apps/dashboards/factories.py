"""
Shared factory-boy factories for all AnalyticsMeta models.
Import from here in any test module.

Usage:
    from apps.dashboards.factories import UserFactory, WorkspaceFactory, DataTableFactory
"""
import factory
from factory.django import DjangoModelFactory
from django.conf import settings


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserFactory(DjangoModelFactory):
    class Meta:
        model = settings.AUTH_USER_MODEL

    username   = factory.Sequence(lambda n: f'user{n}')
    email      = factory.LazyAttribute(lambda obj: f'{obj.username}@test.com')
    first_name = factory.Faker('first_name')
    last_name  = factory.Faker('last_name')
    password   = factory.PostGenerationMethodCall('set_password', 'testpass123')


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

class WorkspaceFactory(DjangoModelFactory):
    class Meta:
        model = 'workspaces.Workspace'

    name  = factory.Sequence(lambda n: f'Workspace {n}')
    owner = factory.SubFactory(UserFactory)

    @factory.post_generation
    def add_owner_membership(self, create, extracted, **kwargs):
        """Every workspace needs an owner membership row."""
        if not create:
            return
        from apps.workspaces.models import WorkspaceMembership
        WorkspaceMembership.objects.get_or_create(
            workspace=self, user=self.owner, defaults={'role': 'owner'}
        )


class WorkspaceMembershipFactory(DjangoModelFactory):
    class Meta:
        model = 'workspaces.WorkspaceMembership'

    workspace = factory.SubFactory(WorkspaceFactory)
    user      = factory.SubFactory(UserFactory)
    role      = 'editor'


class WorkspaceInvitationFactory(DjangoModelFactory):
    class Meta:
        model = 'workspaces.WorkspaceInvitation'

    workspace  = factory.SubFactory(WorkspaceFactory)
    email      = factory.Faker('email')
    role       = 'editor'
    invited_by = factory.LazyAttribute(lambda obj: obj.workspace.owner)


# ---------------------------------------------------------------------------
# Dashboards / Tables / Records
# ---------------------------------------------------------------------------

class DataTableFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.DataTable'

    workspace  = factory.SubFactory(WorkspaceFactory)
    name       = factory.Sequence(lambda n: f'Table {n}')
    created_by = factory.LazyAttribute(lambda obj: obj.workspace.owner)
    schema     = factory.LazyFunction(list)   # empty list default


class DataTableWithSchemaFactory(DataTableFactory):
    """DataTable pre-loaded with a realistic sales schema."""
    name   = factory.Sequence(lambda n: f'Sales {n}')
    schema = [
        {'name': 'customer', 'type': 'text',     'required': True},
        {'name': 'amount',   'type': 'currency',  'required': True},
        {'name': 'region',   'type': 'text',      'required': False},
        {'name': 'closed',   'type': 'boolean',   'required': False},
    ]


class RecordFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.Record'

    table      = factory.SubFactory(DataTableFactory)
    data       = factory.LazyFunction(dict)
    created_by = factory.LazyAttribute(lambda obj: obj.table.workspace.owner)


class DashboardFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.Dashboard'

    workspace  = factory.SubFactory(WorkspaceFactory)
    name       = factory.Sequence(lambda n: f'Dashboard {n}')
    slug       = factory.Sequence(lambda n: f'dashboard-{n}')
    created_by = factory.LazyAttribute(lambda obj: obj.workspace.owner)


class WidgetFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.Widget'

    dashboard   = factory.SubFactory(DashboardFactory)
    widget_type = 'metric'
    title       = factory.Sequence(lambda n: f'Widget {n}')
    position    = factory.LazyFunction(lambda: {'x': 0, 'y': 0, 'w': 3, 'h': 2})
    query_config = factory.LazyFunction(dict)
    viz_config   = factory.LazyFunction(dict)


class ImportJobFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.ImportJob'

    workspace  = factory.SubFactory(WorkspaceFactory)
    table      = factory.SubFactory(DataTableFactory)
    created_by = factory.LazyAttribute(lambda obj: obj.workspace.owner)
    file_name  = 'test_import.csv'
    status     = 'pending'
    total_rows = 0


class DataAlertFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.DataAlert'

    workspace   = factory.SubFactory(WorkspaceFactory)
    table       = factory.SubFactory(DataTableFactory, workspace=factory.SelfAttribute('..workspace'))
    created_by  = factory.LazyAttribute(lambda obj: obj.workspace.owner)
    name        = factory.Sequence(lambda n: f'Alert {n}')
    field_name  = 'amount'
    aggregate   = 'sum'
    operator    = 'gt'
    threshold   = 1000.0
    is_active   = True


class WebhookEndpointFactory(DjangoModelFactory):
    class Meta:
        model = 'dashboards.WebhookEndpoint'

    workspace   = factory.SubFactory(WorkspaceFactory)
    table       = factory.SubFactory(DataTableFactory, workspace=factory.SelfAttribute('..workspace'))
    created_by  = factory.LazyAttribute(lambda obj: obj.workspace.owner)
    name        = factory.Sequence(lambda n: f'Webhook {n}')
    token       = factory.LazyFunction(lambda: __import__('secrets').token_hex(32))
    secret      = factory.LazyFunction(
        lambda: __import__(
            'apps.dashboards.webhook_crypto', fromlist=['encrypt_secret']
        ).encrypt_secret(__import__('secrets').token_urlsafe(32))
    )
    is_active   = True


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

class PlanFactory(DjangoModelFactory):
    class Meta:
        model = 'subscriptions.Plan'

    name                  = factory.Sequence(lambda n: f'Plan {n}')
    tier                  = 'free'
    price_monthly         = factory.Faker('pydecimal', left_digits=4, right_digits=2, positive=True)
    max_tables            = 5
    max_records_per_table = 1_000
    max_team_members      = 1
    is_active             = True


class SubscriptionFactory(DjangoModelFactory):
    class Meta:
        model = 'subscriptions.Subscription'

    workspace = factory.SubFactory(WorkspaceFactory)
    plan      = factory.SubFactory(PlanFactory)
    status    = 'active'


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationFactory(DjangoModelFactory):
    class Meta:
        model = 'notifications.Notification'

    user        = factory.SubFactory(UserFactory)
    title       = factory.Faker('sentence', nb_words=5)
    message     = factory.Faker('paragraph', nb_sentences=2)
    notif_type  = 'info'


class NotificationPreferenceFactory(DjangoModelFactory):
    class Meta:
        model = 'notifications.NotificationPreference'

    user = factory.SubFactory(UserFactory)
