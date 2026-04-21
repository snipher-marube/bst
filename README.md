# AnalyticsMeta

AnalyticsMeta is a multi-tenant business intelligence platform built with Django for SMEs and teams that need structured data capture, dashboards, insights, and workspace-level governance.

## Highlights

- Multi-tenant workspaces with role-based access control.
- Dynamic data tables with typed schema and record management.
- Auto-generated dashboards and configurable widgets.
- AI-generated narrative insights with budget controls.
- Public read-only dashboard sharing via UUID links.
- Asynchronous exports, reports, notifications, and insight generation.
- M-Pesa STK Push billing flow for subscription upgrades.

## Technology Stack

### Backend

- Python 3.14+
- Django 6.x
- Django REST Framework
- Django Channels + Daphne
- Celery + Redis
- PostgreSQL

### Frontend

- Tailwind CSS v4
- Alpine.js
- HTMX
- Plotly.js

### Deployment

- Docker + Docker Compose
- Nginx
- Gunicorn/Daphne

## Quick Start (Docker)

### Prerequisites

- Docker 24+
- Docker Compose 2.20+

### Setup

```bash
git clone https://github.com/snipher-marube/analyticsmeta.git
cd analyticsmeta
cp .env.example .env
docker compose -f docker-compose.dev.yml up --build
```

### First Run

```bash
docker compose -f docker-compose.dev.yml exec web python manage.py migrate
docker compose -f docker-compose.dev.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.dev.yml exec web python manage.py collectstatic --noinput
```

### Local URLs

- App: http://localhost:8000
- Admin: http://localhost:8000/admin
- API root: http://localhost:8000/api/v1/
- Swagger: http://localhost:8000/api/v1/docs/

## Quick Start (Manual)

### Prerequisites

- Python 3.14+
- PostgreSQL
- Redis
- Node.js 18+

### Setup

```bash
git clone https://github.com/snipher-marube/analyticsmeta.git
cd analyticsmeta

python -m venv .venv
source .venv/bin/activate
pip install -e .

npm install
cp .env.example .env

createdb analyticsmeta
python manage.py migrate
python manage.py createsuperuser
```

### Run Services

```bash
python manage.py runserver
celery -A config worker -l info
celery -A config beat -l info
npm run dev
```

## Environment Configuration

Copy `.env.example` to `.env` and set required values for:

- Django settings (`SECRET_KEY`, `DEBUG`, `DJANGO_SETTINGS_MODULE`)
- Database settings (`PG_DATABASE_*`)
- Redis/Celery settings (`REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`)
- Email settings
- Optional integrations: OAuth, Anthropic, M-Pesa, Stripe webhook

Important: avoid inline comments on the same line as `.env` values.

## Architecture

Core model flow:

```text
User
 -> WorkspaceMembership
   -> Workspace
     -> DataTable -> Record
     -> Dashboard -> Widget
     -> Insight
     -> Subscription -> Plan
     -> MpesaTransaction
     -> AuditLog
```

Runtime flow:

```text
Browser -> Nginx -> Daphne/Django -> PostgreSQL
                          |
                          -> Redis -> Celery workers/beat
```

## REST API Overview

Base path: `/api/v1/`

Primary resources:

- Workspaces
- Tables and Records
- Dashboards and Widgets
- Insights
- Authentication

Reference docs:

- OpenAPI schema: `/api/v1/schema/`
- Swagger UI: `/api/v1/docs/`
- Detailed auth/API docs: `docs/api-authentication.md`

## Domain Templates

The platform supports starter workspace patterns, including:

- M-Pesa float and transaction tracking
- Chama/savings group ledger workflows
- Blank custom workspace for general use cases

## Project Structure

```text
apps/
  core/            # Site pages and shared app concerns
  users/           # User model and account logic
  workspaces/      # Workspace lifecycle, roles, invitations
  dashboards/      # Tables, records, dashboards, widgets, API v1
  insights/        # Insight generation, LLM integration, websocket events
  subscriptions/   # Plans, billing, M-Pesa transactions
  exports/         # Export jobs
  reports/         # PDF report jobs
  newsletter/      # Newsletter subscriptions and campaigns

config/
  settings/        # base, development, production settings
  asgi.py          # ASGI entrypoint
  celery.py        # Celery app config
```

## Testing

Run all tests:

```bash
python manage.py test
```

Run a specific app test suite:

```bash
python manage.py test apps.dashboards
```

Recommended pre-PR checks:

```bash
python manage.py check
npm run build
```

## Contributing

- Branch from `main`.
- Use focused commits and include tests for behavioral changes.
- Follow Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, etc.).
- Update documentation when API or behavior changes.

For deeper contribution standards and test guidance, see `docs/testing.md`.

## Security

- Standard Django CSRF/XSS protections.
- ORM-first database access.
- Audit logging for key mutations.
- Private vulnerability reporting: sniphermarube@gmail.com

## Troubleshooting

- Celery jobs not running: verify Redis connectivity and worker logs.
- M-Pesa callbacks failing: ensure callback URL is HTTPS and publicly reachable.
- Static asset 404s: run `python manage.py collectstatic --noinput`.
- Migration conflicts: run `python manage.py showmigrations` and reconcile before deploy.

## Documentation Index

- docs/ai-insights.md
- docs/api-authentication.md
- docs/data-alerts.md
- docs/frontend-patterns.md
- docs/marketing.md
- docs/oauth-setup.md
- docs/openapi-schema.md
- docs/render-deployment.md
- docs/testing.md
- docs/webhooks.md
- docs/websockets.md
- docs/widget-configuration.md
- DOCUMENTATION_GAPS.md

## License

MIT. See `LICENSE`.

## Contact

- Maintainer: Snipher Marube
- Email: sniphermarube@gmail.com
- Issues: https://github.com/snipher-marube/analyticsmeta/issues
