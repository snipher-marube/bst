# AnalyticsMeta

> A multi-tenant business intelligence SaaS platform for Kenyan SMEs.  
> Build custom data tables, get auto-generated dashboards, surface AI insights, and collaborate in real time — no code required.

![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-6.0-092E20?logo=django&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind-v4-06B6D4?logo=tailwindcss&logoColor=white)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)

---

## Table of Contents

- [Overview](#overview)
- [Documentation Index](#documentation-index)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Quick Start — Docker](#quick-start--docker-recommended)
- [Quick Start — Manual](#quick-start--manual)
- [Environment Variables](#environment-variables)
- [Project Structure](#project-structure)
- [REST API](#rest-api)
- [Payment — M-Pesa Daraja](#payment--m-pesa-daraja)
- [Contributing](#contributing)
  - [Prerequisites](#prerequisites)
  - [Branching Strategy](#branching-strategy)
  - [Commit Conventions](#commit-conventions)
  - [Development Workflow](#development-workflow)
  - [Code Style](#code-style)
  - [Writing Tests](#writing-tests)
  - [Submitting a Pull Request](#submitting-a-pull-request)
  - [Good First Issues](#good-first-issues)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [License](#license)
- [Contact](#contact)

---

## Overview

AnalyticsMeta lets any business owner define their own data schema — no SQL, no code. The platform handles:

| Capability | How |
|---|---|
| Custom data tables | JSON-schema columns, 10 typed field kinds |
| Live dashboards | Auto-generated on table creation (KPI cards, charts, gauges) |
| AI insights | Trend / anomaly / prediction / summary via Celery tasks |
| Real-time collaboration | Django Channels + Redis WebSocket push |
| Exports & reports | CSV, Excel, and PDF via ReportLab |
| Billing | M-Pesa STK Push (Safaricom Daraja) — KES-denominated plans |

---

## Documentation Index

Extended documentation lives in the [`docs/`](docs/) folder.

| Topic | File | Status |
|---|---|---|
| API authentication & token examples | [docs/api-authentication.md](docs/api-authentication.md) | Complete |
| WebSocket events reference | [docs/websockets.md](docs/websockets.md) | Complete |
| Widget `query_config` & `viz_config` | [docs/widget-configuration.md](docs/widget-configuration.md) | Complete |
| Google & LinkedIn OAuth setup | [docs/oauth-setup.md](docs/oauth-setup.md) | Complete |
| Render.com deployment guide | [docs/render-deployment.md](docs/render-deployment.md) | Complete |
| Dashboard performance bottlenecks | [docs/DASHBOARD_BOTTLENECKS.md](docs/DASHBOARD_BOTTLENECKS.md) | Complete |
| Open documentation gaps | [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md) | Living document |

> New contributors should read **[docs/api-authentication.md](docs/api-authentication.md)** and **[docs/websockets.md](docs/websockets.md)** before touching the backend, and **[docs/widget-configuration.md](docs/widget-configuration.md)** before touching dashboards.

---

## Tech Stack

### Backend

| Package | Version | Purpose |
|---|---|---|
| Django | 6.0.2 | Web framework |
| Django REST Framework | 3.16 | REST API |
| Django Channels + Daphne | latest | WebSockets / ASGI |
| Celery | 5.6 | Async tasks (insights, exports, email) |
| Redis | 7 | Message broker + cache + channel layer |
| PostgreSQL | 18 | Primary database (psycopg 3) |
| Django Allauth | 65.x | Auth + Google / LinkedIn OAuth2 |
| requests | 2.32+ | M-Pesa Daraja API calls |
| Pandas | latest | CSV / Excel import and export |
| ReportLab | latest | PDF report generation |
| WhiteNoise | latest | Static file serving |
| Gunicorn + Daphne | latest | WSGI / ASGI production servers |
| python-decouple | latest | 12-factor `.env` config |

### Frontend

| Library | Purpose |
|---|---|
| Tailwind CSS v4 | Utility-first CSS (compiled via `@tailwindcss/cli`) |
| Alpine.js | Reactive UI — modals, toggles, dropdowns |
| HTMX | Server-driven partial renders |
| Plotly.js | Charts and visualisations on dashboards |
| GridStack.js | Drag-and-drop dashboard layout |
| Font Awesome 6.5 | Icons |

### Infrastructure

| Layer | Choice |
|---|---|
| Containerisation | Docker + Docker Compose |
| Reverse proxy | Nginx (rate limiting, WebSocket proxy, security headers) |
| SSL | Let's Encrypt / Render managed TLS |
| Timezone | `Africa/Nairobi` |
| Cloud deploy | Render.com (see [docs/render-deployment.md](docs/render-deployment.md)) |

---

## Architecture

### Data Model Hierarchy

```
User
 └── WorkspaceMembership  (role: owner | admin | editor | viewer)
      └── Workspace  (tier: free | starter | professional | enterprise)
           ├── DataTable  (schema: JSONField — typed columns)
           │    └── Record  (data: JSONField)
           ├── Dashboard
           │    └── Widget  (type, query_config, viz_config, position)
           ├── Insight  (trend | anomaly | summary | prediction | comparison)
           ├── Subscription → Plan
           ├── MpesaTransaction
           ├── WorkspaceInvitation
           └── AuditLog
```

### Request and WebSocket Flow

```
Browser ──HTTP──► Nginx ──► Daphne (ASGI) ──► Django views / DRF
        ──WS───► Nginx ──► Daphne ──► Channels Consumer ──► Redis channel layer
                                                                └──► Celery worker
                                                                      (insights, exports, email)
```

### Auto-Dashboard Generation

When a `DataTable` is saved, `generate_default_dashboard()` introspects the schema and creates:

- One **KPI metric card** per numeric column (sum aggregation)
- A **total records** KPI card (always)
- One **line chart** per numeric column, grouped by date if a date field exists
- One **bar chart** per categorical (text/category) column
- A **data table** widget showing the last 10 records

See [`apps/dashboards/models.py`](apps/dashboards/models.py) — `DataTable.generate_default_dashboard()` and [`docs/widget-configuration.md`](docs/widget-configuration.md) for the full `query_config` / `viz_config` reference.

### Tier Limits

| Tier | Tables | Records / table | Members | Price |
|---|---|---|---|---|
| Free | 5 | 1,000 | 1 | KES 0 |
| Starter | 20 | 10,000 | 5 | KES 2,500 / mo |
| Professional | 100 | 100,000 | 20 | KES 6,500 / mo |
| Enterprise | 999 | 999,999 | 999 | KES 12,900 / mo |

Limits are applied in `Subscription.apply_plan_limits()` after a successful M-Pesa payment.

---

## Quick Start — Docker (Recommended)

### Prerequisites

- Docker 24.0+  
- Docker Compose 2.20+

### 1. Clone

```bash
git clone https://github.com/snipher-marube/analyticsmeta.git
cd analyticsmeta
```

### 2. Configure environment

```bash
cp .env.example .env
# Open .env and fill in the required values (see Environment Variables below)
```

### 3. Build and start

```bash
docker compose -f docker-compose.dev.yml up --build
```

Starts: **PostgreSQL 18** · **Redis 7** · **Django dev server (:8000)** · **Celery worker**

### 4. First-run setup

```bash
docker compose -f docker-compose.dev.yml exec web python manage.py migrate
docker compose -f docker-compose.dev.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.dev.yml exec web python manage.py collectstatic --noinput
```

### 5. Open

| URL | Description |
|---|---|
| <http://localhost:8000> | Marketing home |
| <http://localhost:8000/admin> | Django admin |
| <http://localhost:8000/api/v1/> | REST API root |
| <http://localhost:8000/dashboard/analytics/> | Dashboard home |

### Useful Docker commands

```bash
# Tail logs
docker compose -f docker-compose.dev.yml logs -f web

# Django shell
docker compose -f docker-compose.dev.yml exec web python manage.py shell

# Postgres CLI
docker compose -f docker-compose.dev.yml exec postgres psql -U postgres -d analyticsmeta

# Run tests
docker compose -f docker-compose.dev.yml exec web python manage.py test

# Stop everything
docker compose -f docker-compose.dev.yml down
```

---

## Quick Start — Manual

### Prerequisites

- Python 3.14+
- PostgreSQL 15+
- Redis 6+
- Node.js 18+ (Tailwind CSS build)

### Steps

```bash
# 1. Clone
git clone https://github.com/snipher-marube/analyticsmeta.git && cd analyticsmeta

# 2. Virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Node dependencies (Tailwind CSS)
npm install

# 4. Copy and configure env
cp .env.example .env   # then edit .env

# 5. Database
createdb analyticsmeta
python manage.py migrate
python manage.py createsuperuser

# 6. Run all services (four terminals)
python manage.py runserver              # Terminal 1 — Django dev server
celery -A config worker -l info         # Terminal 2 — Celery worker
celery -A config beat -l info           # Terminal 3 — Celery beat (scheduled tasks)
npm run dev                             # Terminal 4 — Tailwind CSS watch
```

---

## Environment Variables

Copy `.env.example` to `.env`. **Do not add inline comments on the same line as a value** — python-decouple treats them as part of the value.

```env
# ── Django ──────────────────────────────────────────────────────────────────
SECRET_KEY=your-super-secret-key-change-in-production
DEBUG=True
DJANGO_SETTINGS_MODULE=config.settings.development

# ── Database ─────────────────────────────────────────────────────────────────
PG_DATABASE_NAME_DEV=analyticsmeta
PG_DATABASE_USER_DEV=postgres
PG_DATABASE_PASSWORD_DEV=postgres
PG_DATABASE_HOST_DEV=postgres
# Use 'postgres' with Docker, 'localhost' for manual setup
PG_DATABASE_PORT_DEV=5432

# ── Redis / Celery ───────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0

# ── Email ────────────────────────────────────────────────────────────────────
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
SUPPORT_EMAIL=support@example.com

# ── OAuth (optional) ─────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
LINKEDIN_CLIENT_ID=
LINKEDIN_CLIENT_SECRET=

# ── Site ─────────────────────────────────────────────────────────────────────
SITE_URL=http://localhost:8000
SITE_NAME=AnalyticsMeta

# ── M-Pesa Daraja API ────────────────────────────────────────────────────────
MPESA_SANDBOX=True
MPESA_CONSUMER_KEY=your_daraja_consumer_key
MPESA_CONSUMER_SECRET=your_daraja_consumer_secret
MPESA_SHORTCODE=174379
MPESA_PASSKEY=bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919
# Must be a publicly reachable HTTPS URL — use ngrok for local testing
MPESA_CALLBACK_URL=https://your-ngrok-url.ngrok.io/subscriptions/mpesa/callback/

# ── Stripe (stubbed, optional) ───────────────────────────────────────────────
STRIPE_WEBHOOK_SECRET=
```

> Full variable reference including production-only variables is in [`.env.example`](.env.example).

---

## Project Structure

```
analyticsmeta/
├── apps/
│   ├── core/              # Marketing pages, site settings, context processors
│   ├── users/             # Custom user model and profile
│   ├── workspaces/        # Workspace CRUD, membership, invitations, roles
│   ├── dashboards/        # DataTable, Record, Dashboard, Widget, AuditLog,
│   │                      #   ImportJob — plus REST API v1 views
│   ├── exports/           # ExportJob — CSV, JSON, Excel async export
│   ├── reports/           # ReportJob — async PDF generation via ReportLab
│   ├── insights/          # Insight model, AI generation Celery tasks,
│   │                      #   WebSocket consumer for live dashboard push
│   ├── subscriptions/     # Plan, Subscription, MpesaTransaction,
│   │                      #   MpesaService (Daraja), Stripe webhook stub
│   └── newsletter/        # Subscription, Campaign, WebhookEvent
│
├── config/
│   ├── settings/
│   │   ├── base.py        # Shared settings (Channels, Celery, M-Pesa, email…)
│   │   ├── development.py # DEBUG=True, Django Debug Toolbar, relaxed hosts
│   │   └── production.py  # WhiteNoise, Sentry hook, secure cookies
│   ├── asgi.py            # Daphne / Channels ASGI entry point
│   ├── celery.py          # Celery app configuration
│   ├── urls.py            # Root URL configuration
│   └── wsgi.py
│
├── templates/
│   ├── base.html                       # Public marketing shell
│   ├── account/                        # django-allauth overrides
│   ├── includes/                       # header.html, footer.html, mobile-menu.html
│   └── dashboard/
│       ├── base_dashboard.html         # SaaS sidebar shell (Alpine.js)
│       ├── index.html                  # Analytics home — recent activity, quick stats
│       ├── tables.html                 # Table list
│       ├── table_detail.html           # Record list + import/export actions
│       ├── table_form.html             # Create / edit table schema
│       ├── table_import.html           # CSV / Excel import wizard
│       ├── record_form.html            # Create / edit a record
│       ├── dashboard_detail.html       # Live dashboard with GridStack + Plotly
│       ├── workspaces.html             # Workspace switcher
│       ├── members.html                # Team management + invitations
│       ├── billing.html                # Plans + M-Pesa payment modal
│       ├── settings.html               # Workspace settings + danger zone
│       ├── profile.html                # User profile management
│       └── activity.html              # Audit log timeline
│
├── static/                # Source static files (CSS input, JS, images, video)
├── staticfiles/           # Collected static (do not edit — gitignored)
├── docs/                  # Extended documentation (see Documentation Index above)
│
├── docker-compose.dev.yml
├── docker-compose.prod.yml
├── Dockerfile.dev
├── Dockerfile.prod
├── entrypoint.sh          # Docker entrypoint — waits for Postgres, migrates, collectstatic
├── manage.py
├── pyproject.toml
├── package.json           # Tailwind CSS CLI
└── .env.example
```

---

## REST API

**Base URL:** `/api/v1/`  
All endpoints require authentication — see [docs/api-authentication.md](docs/api-authentication.md) for token setup and request examples.

### Workspaces

```
GET    /api/v1/workspaces/
POST   /api/v1/workspaces/
GET    /api/v1/workspaces/{id}/
PUT    /api/v1/workspaces/{id}/
DELETE /api/v1/workspaces/{id}/
```

### Tables & Records

```
GET    /api/v1/workspaces/{id}/tables/
POST   /api/v1/workspaces/{id}/tables/
GET    /api/v1/tables/{id}/
PUT    /api/v1/tables/{id}/
DELETE /api/v1/tables/{id}/

GET    /api/v1/tables/{id}/records/     (paginated)
POST   /api/v1/tables/{id}/records/
GET    /api/v1/records/{id}/
PUT    /api/v1/records/{id}/
DELETE /api/v1/records/{id}/
```

### Dashboards

```
GET    /api/v1/workspaces/{id}/dashboards/
POST   /api/v1/workspaces/{id}/dashboards/
GET    /api/v1/dashboards/{id}/
PUT    /api/v1/dashboards/{id}/
DELETE /api/v1/dashboards/{id}/
```

### Auth

```
POST   /api/v1/auth/login/
POST   /api/v1/auth/logout/
POST   /api/v1/auth/register/
POST   /api/v1/auth/password/reset/
```

> **Note:** An OpenAPI / Swagger schema (`drf-spectacular`) is tracked as an open item in [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md).

---

## Payment — M-Pesa Daraja

Billing uses Safaricom's **Lipa Na M-Pesa Online** (STK Push).

### Flow

```
1. User clicks "Pay with M-Pesa" on a plan card
2. Alpine.js modal — user enters their Kenyan phone number
3. POST /subscriptions/mpesa/stk-push/ → MpesaService.stk_push()
4. Safaricom sends a push prompt to the user's phone
5. User enters their M-Pesa PIN
6. Frontend polls /subscriptions/mpesa/status/<checkout_id>/ every 4 s
7. On "completed" → workspace limits upgraded; receipt number shown
8. Safaricom also POSTs the result to /subscriptions/mpesa/callback/ (backup)
```

### Key files

| File | Purpose |
|---|---|
| [`apps/subscriptions/mpesa_service.py`](apps/subscriptions/mpesa_service.py) | `MpesaService` — OAuth token, STK Push, STK Query, phone normaliser |
| [`apps/subscriptions/views.py`](apps/subscriptions/views.py) | `mpesa_stk_push`, `mpesa_callback`, `mpesa_payment_status` |
| [`apps/subscriptions/models.py`](apps/subscriptions/models.py) | `MpesaTransaction`, `Subscription`, `Plan` |
| [`templates/dashboard/billing.html`](templates/dashboard/billing.html) | Plan cards + Alpine.js payment modal |

### Sandbox setup

1. Register at [developer.safaricom.co.ke](https://developer.safaricom.co.ke)
2. Create a Daraja app and copy **Consumer Key** and **Consumer Secret**
3. Set `MPESA_SANDBOX=True` in `.env`
4. Expose your local server: `ngrok http 8000`
5. Set `MPESA_CALLBACK_URL=https://<your-ngrok>.ngrok.io/subscriptions/mpesa/callback/`
6. Use the sandbox test phone number from the Daraja portal

### Going to production

```env
MPESA_SANDBOX=False
MPESA_CONSUMER_KEY=<production_key>
MPESA_CONSUMER_SECRET=<production_secret>
MPESA_SHORTCODE=<your_paybill_or_till>
MPESA_PASSKEY=<your_passkey>
MPESA_CALLBACK_URL=https://yourdomain.com/subscriptions/mpesa/callback/
```

---

## Contributing

Thank you for your interest in contributing to AnalyticsMeta. This section walks you through everything you need to submit your first PR confidently.

### Prerequisites

Before you start, make sure you have:

- [ ] Read the [Architecture](#architecture) section
- [ ] Read [docs/api-authentication.md](docs/api-authentication.md)
- [ ] Read [docs/websockets.md](docs/websockets.md) (if touching real-time features)
- [ ] Read [docs/widget-configuration.md](docs/widget-configuration.md) (if touching dashboards)
- [ ] A working local setup (Docker or manual — see [Quick Start](#quick-start--docker-recommended))

### Branching Strategy

We use a simple trunk-based workflow:

```
main              ← production-ready, protected
 ├── feat/<slug>  ← new features
 ├── fix/<slug>   ← bug fixes
 ├── docs/<slug>  ← documentation only
 └── chore/<slug> ← dependency bumps, tooling
```

Always branch from `main`:

```bash
git checkout main && git pull
git checkout -b feat/your-feature-name
```

Branch names must be lowercase, hyphen-separated, and describe the change — not the ticket number.

### Commit Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/). Every commit message must start with a type prefix:

| Prefix | When to use |
|---|---|
| `feat:` | A new feature visible to users |
| `fix:` | A bug fix |
| `docs:` | Documentation only — no code change |
| `refactor:` | Code change that neither adds a feature nor fixes a bug |
| `perf:` | Performance improvement |
| `test:` | Adding or correcting tests |
| `chore:` | Build tooling, deps, CI config |
| `style:` | Formatting — no logic change |

**Examples:**

```bash
git commit -m "feat: add PDF export button to table detail page"
git commit -m "fix: normalise Kenyan phone numbers with leading 0"
git commit -m "docs: document widget viz_config for gauge type"
git commit -m "refactor: extract MpesaService into its own module"
```

Breaking changes append `!` after the type:

```bash
git commit -m "feat!: change records API response shape to match JSON:API spec"
```

### Development Workflow

#### 1. Create your branch

```bash
git checkout main && git pull origin main
git checkout -b feat/my-feature
```

#### 2. Make your changes

- Keep each commit focused on one logical change
- Add or update tests for any behaviour you modify (see [Writing Tests](#writing-tests))
- If you change a template, run the Tailwind watcher: `npm run dev`
- If you add a new model, create and apply a migration:

```bash
python manage.py makemigrations <app_name>
python manage.py migrate
```

#### 3. Run the test suite

```bash
# All tests
python manage.py test

# Specific app
python manage.py test apps.dashboards

# With Docker
docker compose -f docker-compose.dev.yml exec web python manage.py test
```

#### 4. Check for issues

```bash
# Python — basic syntax / import check
python manage.py check

# Tailwind CSS rebuild
npm run build
```

#### 5. Push and open a PR

```bash
git push origin feat/my-feature
# Then open a PR on GitHub targeting main
```

### Code Style

#### Python

- Follow **PEP 8** — 4-space indentation, 120-character line limit
- Use Django conventions: class-based views, ORM querysets, `get_object_or_404`
- Keep views thin — business logic belongs in service modules or model methods
- Avoid raw SQL unless there is a genuine performance reason and the query is documented

#### Templates

- Tailwind CSS v4 utility classes only — no custom CSS files unless adding a new page section
- Alpine.js for interactivity — keep `x-data` scopes small and co-located
- Use HTMX for partial page updates that the server should own
- Smart (curly) apostrophes inside `{% trans '...' %}` will break the template parser — always use double-quote delimiters: `{% trans "We'll..." %}`

#### JavaScript

- Vanilla JS and Alpine.js only — no jQuery, no new npm packages without discussion
- All JS lives in `{% block extra_scripts %}` at the bottom of the template or in `static/js/`

### Writing Tests

Tests live in each app's `tests.py`. We use Django's built-in `TestCase`.

```python
# apps/dashboards/tests.py
from django.test import TestCase
from django.contrib.auth import get_user_model
from apps.workspaces.models import Workspace
from apps.dashboards.models import DataTable

User = get_user_model()

class DataTableTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="test@example.com", password="pass")
        self.workspace = Workspace.objects.create(name="Test WS", owner=self.user)

    def test_create_table_generates_dashboard(self):
        table = DataTable.objects.create(
            workspace=self.workspace,
            name="Sales",
            schema=[{"name": "amount", "type": "currency", "required": True}],
            created_by=self.user,
        )
        table.generate_default_dashboard()
        self.assertEqual(table.workspace.dashboards.count(), 1)
```

**Guidelines:**

- Every new view needs at least one happy-path and one permission-denied test
- Every new model method needs a unit test
- Use `self.client.force_login(user)` for authenticated view tests
- Do not mock the database — use real SQLite in tests (the default Django test runner handles this)

> Testing documentation is an open item — see [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md).

### Submitting a Pull Request

1. **Title** — follow the same Conventional Commits format as commit messages
2. **Description** — fill in:
   - What problem does this PR solve?
   - What approach did you take and why?
   - Screenshots or screen recordings for UI changes
   - Any migrations included?
3. **Checklist before marking ready for review:**
   - [ ] Tests pass locally (`python manage.py test`)
   - [ ] `python manage.py check` reports no issues
   - [ ] Migrations are included if the model changed
   - [ ] Tailwind CSS output is rebuilt if templates changed
   - [ ] Docs updated if you changed behaviour described in [`docs/`](docs/)
4. **Keep PRs small** — one concern per PR makes review faster. If you're unsure whether something is too large, open a draft PR and ask.
5. **Don't force-push** to a PR branch after review has started — it makes diff tracking harder.

### Good First Issues

New to the codebase? These areas are well-documented and self-contained:

| Area | What to do | Relevant files |
|---|---|---|
| OpenAPI schema | Add `drf-spectacular`, expose `/api/v1/schema/` and Swagger UI | `config/urls.py`, `pyproject.toml` |
| Template tag docs | Write reference for `dashboard_filters.py` custom filter | `apps/dashboards/templatetags/` |
| Test coverage | Add `TestCase` classes for `apps/workspaces/` views | `apps/workspaces/tests.py` |
| Newsletter webhooks | Document bounce/complaint → `WebhookEvent` → Celery task flow | `apps/newsletter/`, `DOCUMENTATION_GAPS.md` |
| Frontend patterns doc | Write `docs/frontend-patterns.md` covering Alpine.js store + HTMX conventions | any template |

> All open items are tracked in [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md).

---

## Security

- CSRF protection on all state-changing requests
- `mpesa_callback` is `@csrf_exempt` — Safaricom posts without a token; payload is validated by receipt structure
- SQL injection prevention via Django ORM (no raw SQL in application code)
- XSS protection via Django template auto-escaping
- Secure password hashing — PBKDF2, 600,000 iterations
- OAuth2 via django-allauth (Google + LinkedIn)
- DRF Token authentication for the REST API
- Rate limiting on authentication endpoints
- Email verification required on signup
- Audit log (`AuditLog` model) for all record-level mutations

**Found a vulnerability?** Please report it privately to **sniphermarube@gmail.com** rather than opening a public issue.

---

## Troubleshooting

**`TemplateSyntaxError: Could not parse the remainder: 'll' from ''We'll'`**  
A curly/smart apostrophe (`'`) appeared inside a single-quoted `{% trans '...' %}` tag. Fix: switch to double quotes: `{% trans "We'll..." %}`.

**M-Pesa STK Push not reaching phone**  
- Verify `MPESA_CONSUMER_KEY` and `MPESA_CONSUMER_SECRET` are correct  
- The callback URL must be HTTPS and publicly reachable — use `ngrok http 8000` locally  
- Check sandbox test credentials in the Daraja portal

**Database connection error with Docker**  
Inline comments in `.env` break python-decouple — the comment text becomes part of the value:

```env
# Wrong — comment becomes part of the value
PG_DATABASE_HOST_DEV=postgres  # Docker service name

# Correct
# Docker service name
PG_DATABASE_HOST_DEV=postgres
```

**Celery tasks not executing**

```bash
docker compose -f docker-compose.dev.yml exec redis redis-cli ping   # expect PONG
docker compose -f docker-compose.dev.yml logs celery-worker
```

**Static files missing (404 on CSS / JS)**

```bash
python manage.py collectstatic --noinput
```

**Migration conflicts**

```bash
python manage.py showmigrations
python manage.py migrate --run-syncdb
```

**WebSocket connection refused**  
Ensure Daphne (not Gunicorn) is handling the ASGI connection. Check `config/asgi.py` and that Redis is running.

---

## Roadmap

Planned features and open documentation tasks are tracked in two places:

- **[DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md)** — documentation work remaining
- **[GitHub Issues](https://github.com/snipher-marube/analyticsmeta/issues)** — feature requests and bugs

Highest-priority items right now:

1. OpenAPI / Swagger schema via `drf-spectacular`
2. Test coverage for `apps/workspaces/` and `apps/subscriptions/`
3. Newsletter webhook (bounce/complaint) documentation
4. Frontend patterns documentation (Alpine.js + HTMX conventions)

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Contact

| | |
|---|---|
| **Maintainer** | Snipher Marube |
| **Email** | sniphermarube@gmail.com |
| **Issues** | [github.com/snipher-marube/analyticsmeta/issues](https://github.com/snipher-marube/analyticsmeta/issues) |

---

*Built with Django · Tailwind CSS v4 · Alpine.js · HTMX · Plotly.js · M-Pesa Daraja API*
