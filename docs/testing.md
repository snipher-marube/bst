# Testing Guide

AnalyticsMeta uses **pytest** with **pytest-django** and **factory-boy** for its test suite.
All tests live under `apps/<app_name>/tests.py` (or `test_api.py` for REST API tests).

---

## Quick start

```bash
# Run the full suite (uses config.settings.test automatically via pytest.ini)
pytest

# Run with coverage report
pytest --cov=apps --cov-report=term-missing

# Run a single app
pytest apps/dashboards/

# Run a single test class
pytest apps/dashboards/tests.py::TestDashboardHTMLViews

# Run a single test
pytest apps/dashboards/tests.py::TestDashboardHTMLViews::test_table_soft_delete

# Stop after the first failure (-x) and show local variables (--tb=long)
pytest -x --tb=long
```

---

## Settings

`pytest.ini` sets `DJANGO_SETTINGS_MODULE = config.settings.test`.  The test settings
(`config/settings/test.py`) extend `development.py` with these overrides:

| Setting | Test value | Why |
|---|---|---|
| `DATABASES` | SQLite in-file | No Postgres credentials required |
| `CACHES` | `LocMemCache` | Isolated per-process, no Redis needed |
| `CELERY_TASK_ALWAYS_EAGER` | `True` | Tasks run synchronously in the same process |
| `EMAIL_BACKEND` | `locmem` | Outbox captured in `django.core.mail.outbox` |
| `CHANNEL_LAYERS` | `InMemoryChannelLayer` | WebSocket tests don't need Redis |
| `PASSWORD_HASHERS` | `MD5PasswordHasher` | User creation is instant |
| `ACCOUNT_EMAIL_VERIFICATION` | `'none'` | No email confirm required in tests |
| `debug_toolbar` | removed | Not included in `INSTALLED_APPS` or `MIDDLEWARE` |

---

## Factories

All model factories live in `apps/dashboards/factories.py` and are shared across apps.

```python
from apps.dashboards.factories import (
    UserFactory,
    WorkspaceFactory,
    WorkspaceMembershipFactory,
    DataTableFactory,
    DataTableWithSchemaFactory,  # pre-loaded with a sales schema
    RecordFactory,
    DashboardFactory,
    WidgetFactory,
)
```

### Key factories

#### `UserFactory`
Creates an active `auth.User` with a unique email and a hashed password.

```python
user = UserFactory()
user = UserFactory(email="alice@example.com")
```

#### `WorkspaceFactory`
Creates a `Workspace` and automatically adds the owner as a member with role
`'owner'` via the `add_owner_membership` post-generation hook.

```python
workspace = WorkspaceFactory(owner=user)
```

#### `DataTableWithSchemaFactory`
Creates a `DataTable` with a realistic four-field sales schema
(`customer`, `amount`, `region`, `closed`).  Use this when your test
exercises schema validation or type coercion:

```python
table = DataTableWithSchemaFactory(workspace=workspace)
Record.objects.create(
    table=table,
    data={'customer': 'Alice', 'amount': 100, 'region': 'North', 'closed': True},
)
```

#### `RecordFactory`
Creates a `Record` with an empty `data` dict by default.  Pass ``data``
explicitly when your test needs specific field values:

```python
record = RecordFactory(table=table, data={'customer': 'Bob', 'amount': 50})
```

---

## Writing tests

### HTML view tests

Use `django.test.Client` (aliased as `DjangoClient`) and log in via
`force_login`.  Set the workspace in the session so
`CurrentWorkspaceMiddleware` can resolve it:

```python
from django.test import TestCase, Client as DjangoClient
from apps.dashboards.factories import UserFactory, WorkspaceFactory

class TestMyView(TestCase):
    def setUp(self):
        self.client = DjangoClient()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        session = self.client.session
        session['current_workspace_id'] = str(self.workspace.id)
        session.save()

    def test_something(self):
        resp = self.client.get('/dashboard/tables/')
        self.assertEqual(resp.status_code, 200)
```

### REST API tests

Use DRF's `APIClient` and authenticate with `force_authenticate`.  Pass
the workspace UUID in the `X-Workspace-ID` header (required by
`HasWorkspaceAccess`):

```python
from rest_framework.test import APITestCase, APIClient
from apps.dashboards.factories import UserFactory, WorkspaceFactory

class TestMyAPI(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = UserFactory()
        self.workspace = WorkspaceFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        self.ws_header = {'HTTP_X_WORKSPACE_ID': str(self.workspace.id)}

    def test_list_tables(self):
        resp = self.client.get('/api/v1/tables/', **self.ws_header)
        self.assertEqual(resp.status_code, 200)
```

### Mocking Celery tasks

Celery tasks run eagerly in tests (`CELERY_TASK_ALWAYS_EAGER = True`), but
tasks that call external services (M-Pesa, email) should be patched to
avoid network calls:

```python
from unittest.mock import patch

with patch('apps.dashboards.models.broadcast_widget_update.delay'), \
     patch('apps.dashboards.models.notify_table_change.delay'):
    record = RecordFactory(table=table)
```

### Testing emails

```python
from django.core import mail

self.client.post('/newsletter/subscribe/', {...})
self.assertEqual(len(mail.outbox), 1)
self.assertIn('Confirm', mail.outbox[0].subject)
```

### Soft-delete assertions

Most delete views **soft-delete** (set `is_active=False`) rather than
hard-delete.  The default queryset filters `is_active=True`, so use
`filter()` instead of `get()` when asserting post-delete state:

```python
# ✅ Correct — bypasses the is_active=True filter
table = DataTable.objects.filter(id=table.id).first()
self.assertIsNotNone(table)
self.assertFalse(table.is_active)

# ❌ Wrong — raises DoesNotExist after soft delete
table = DataTable.objects.get(id=table.id)
```

---

## Test file map

| File | What it tests |
|---|---|
| `apps/core/tests.py` | Health-check endpoints, public marketing pages |
| `apps/dashboards/tests.py` | Models, services (QueryEngine, DataImportService), HTML views, permissions |
| `apps/dashboards/test_api.py` | Full REST API v1 CRUD for all resources |
| `apps/exports/tests.py` | CSV / JSON / Excel export views, auth, workspace isolation |
| `apps/insights/tests.py` | Widget and Dashboard serializers, JSON serialisation helpers |
| `apps/newsletter/tests.py` | Subscription forms, subscribe/confirm/unsubscribe views, webhook |
| `apps/notifications/tests.py` | Notification model, preference rules, email dispatch |
| `apps/reports/tests.py` | Report generation API views |
| `apps/subscriptions/tests.py` | Plan/Subscription models, M-Pesa callback, Stripe webhook handlers |
| `apps/workspaces/tests.py` | Workspace model, membership, invitations, onboarding, workspace views |

---

## Coverage

The CI pipeline enforces **70 % minimum coverage** via:

```
pytest --cov=apps --cov-fail-under=70
```

Coverage configuration is in `pyproject.toml` under `[tool.coverage.run]`.
Excluded paths (not expected to have unit tests):

- `*/migrations/*` — auto-generated schema migrations
- `*/management/commands/*` — CLI commands
- `apps/insights/consumers.py` — WebSocket consumers (need channels test client)
- `apps/insights/routing.py` — WebSocket URL routing
- `apps/insights/generators/*` — AI insight generator pipeline
- `apps/reports/charts.py` / `apps/reports/generators.py` — PDF/chart rendering

To check coverage locally:

```bash
pytest --cov=apps --cov-report=html
open htmlcov/index.html
```

---

## CI

Tests run automatically on every push to `main`/`develop` and on every pull
request to `main` via `.github/workflows/tests.yml`.  The workflow:

1. Spins up Postgres 18 and Redis 7 as service containers.
2. Runs `python manage.py migrate`.
3. Runs `pytest --cov=apps --cov-report=term-missing --cov-fail-under=70 -v`.
4. Uploads `.coverage` as an artifact.

The workflow uses `DJANGO_SETTINGS_MODULE=config.settings.test` so all
infrastructure-dependent settings (debug toolbar, Postgres dev credentials)
are swapped for their test equivalents automatically.

