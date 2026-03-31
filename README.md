# AnalyticsMeta

A multi-tenant business intelligence SaaS platform built with Django. Teams can create custom data tables, build interactive dashboards, gain AI-powered insights from their data, and collaborate in real time — all from a clean, professional workspace UI.

## Table of Contents
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Docker Setup](#docker-setup-recommended)
- [Manual Installation](#manual-installation)
- [Environment Variables](#environment-variables)
- [Project Structure](#project-structure)
- [Architecture](#architecture)
- [Payment — M-Pesa](#payment--m-pesa-daraja)
- [REST API](#rest-api)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Features

### Workspaces & Plans
- Multi-tenant architecture — each workspace is isolated
- Four tiers: **Free / Starter / Professional / Enterprise**
- Per-workspace limits on tables, records, and team members
- Workspace switcher in the sidebar for users who belong to multiple workspaces

### Data Tables
- Schema-based tables with typed columns (text, number, date, boolean, email, URL, phone, currency, percentage)
- Paginated record list with inline add/edit/delete
- CSV and Excel import with auto-schema detection and column mapping
- Export to CSV, JSON, or Excel

### Dashboards & Widgets
- Drag-and-drop dashboard builder (GridStack.js)
- Widget types: bar chart, line chart, pie chart, scatter plot, KPI card, data table (Plotly.js)
- Real-time widget updates via Django Channels WebSockets
- AI-powered insight generation (Celery + OpenAI/custom)

### Team Collaboration
- Role-based access control: **Owner / Admin / Editor / Viewer**
- Email invitations with expiry and revoke
- Member limit enforced per plan tier
- Real-time notifications (WebSocket channel layer)

### Billing & Payments
- Plan cards with usage progress bars
- **M-Pesa Daraja STK Push** — Lipa Na M-Pesa Online (sandbox + production)
- Payment history with M-Pesa receipt numbers
- Stripe webhook handler (stubbed, ready to activate)

### Dashboard UI
- Professional SaaS sidebar layout (collapsible, responsive)
- Notification bell with unread badge
- Activity timeline with filterable action log
- Profile management, password change via allauth

---

## Tech Stack

### Backend
| Package | Version | Purpose |
|---|---|---|
| Django | 6.0.2 | Web framework |
| Django REST Framework | 3.16 | REST API |
| Django Channels + Daphne | latest | WebSockets / ASGI |
| Celery | 5.6 | Async tasks |
| Redis | 7 | Message broker + cache |
| PostgreSQL | 18 | Primary database (psycopg 3) |
| Django Allauth | 65.14 | Auth + Google/LinkedIn OAuth2 |
| requests | 2.32+ | M-Pesa Daraja API calls |
| Pandas | latest | Data manipulation (import/export) |
| WhiteNoise | latest | Static file serving |
| Gunicorn | latest | Production WSGI server |
| python-decouple | latest | 12-factor config |

### Frontend
| Library | Purpose |
|---|---|
| Tailwind CSS 4 | Utility-first CSS |
| Alpine.js | Reactive UI components, modals, toggles |
| HTMX | Server-side partial rendering |
| Plotly.js | Charts and visualizations |
| GridStack.js | Drag-and-drop dashboard layout |
| Font Awesome 6.5 | Icons |

### Infrastructure
- Docker + Docker Compose (dev)
- Nginx (production reverse proxy)
- Let's Encrypt / SSL
- `TIME_ZONE = 'Africa/Nairobi'`

---

## Docker Setup (Recommended)

### Prerequisites
- Docker 24.0+
- Docker Compose 2.20+

### 1. Clone
```bash
git clone https://github.com/snipher-marube/bst
cd bst
```

### 2. Create `.env` (see [Environment Variables](#environment-variables))

### 3. Build and start
```bash
docker compose -f docker-compose.dev.yml up --build
# Or detached:
docker compose -f docker-compose.dev.yml up -d --build
```

Starts: PostgreSQL 18 · Redis 7 · Django dev server (:8000) · Celery worker

### 4. First-run setup
```bash
docker compose -f docker-compose.dev.yml exec web python manage.py migrate
docker compose -f docker-compose.dev.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.dev.yml exec web python manage.py collectstatic --noinput
```

### 5. Open
| URL | Description |
|---|---|
| http://localhost:8000 | Main application |
| http://localhost:8000/admin | Django admin |
| http://localhost:8000/api/v1/ | REST API |
| http://localhost:8000/dashboard/analytics/ | Dashboard home |

### Useful Docker commands
```bash
# Logs
docker compose -f docker-compose.dev.yml logs -f web

# Django shell
docker compose -f docker-compose.dev.yml exec web python manage.py shell

# Postgres CLI
docker compose -f docker-compose.dev.yml exec postgres psql -U postgres -d analyticsmeta

# Stop
docker compose -f docker-compose.dev.yml down
```

---

## Manual Installation

### Prerequisites
- Python 3.14+
- PostgreSQL 15+
- Redis 6+
- Node.js 18+ (Tailwind CSS build)

### Steps
```bash
# 1. Clone
git clone https://github.com/snipher-marube/bst && cd bst

# 2. Virtual environment
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Node dependencies
npm install

# 4. Copy and configure env
cp .env.example .env   # then edit .env

# 5. Database
createdb analyticsmeta
python manage.py migrate
python manage.py createsuperuser

# 6. Run services (four terminals)
python manage.py runserver                  # Terminal 1
celery -A config worker -l info             # Terminal 2
celery -A config beat -l info               # Terminal 3
npm run dev                                 # Terminal 4 (Tailwind watch)
```

---

## Environment Variables

Create a `.env` file in the project root. Do **not** add inline comments on the same line as a value.

```env
# Django
SECRET_KEY=your-super-secret-key-change-this-in-production
DEBUG=True
DJANGO_SETTINGS_MODULE=config.settings.development

# Database
PG_DATABASE_NAME_DEV=analyticsmeta
PG_DATABASE_USER_DEV=postgres
PG_DATABASE_PASSWORD_DEV=postgres
PG_DATABASE_HOST_DEV=postgres        # Use 'postgres' with Docker, 'localhost' for manual
PG_DATABASE_PORT_DEV=5432

# Redis / Celery
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0

# Email
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
SUPPORT_EMAIL=support@example.com

# OAuth (optional)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
LINKEDIN_CLIENT_ID=
LINKEDIN_CLIENT_SECRET=

# Site
SITE_URL=http://localhost:8000
SITE_NAME=AnalyticsMeta

# M-Pesa Daraja API
# Set MPESA_SANDBOX=True for sandbox, False for production
MPESA_SANDBOX=True
MPESA_CONSUMER_KEY=your_daraja_consumer_key
MPESA_CONSUMER_SECRET=your_daraja_consumer_secret
# Sandbox defaults are pre-filled in settings; override here for production:
MPESA_SHORTCODE=174379
MPESA_PASSKEY=bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919
# Must be a publicly reachable HTTPS URL (use ngrok for local testing)
MPESA_CALLBACK_URL=https://your-ngrok-url.ngrok.io/subscriptions/mpesa/callback/

# Stripe (optional — webhook handler is present but stubbed)
STRIPE_WEBHOOK_SECRET=
```

---

## Project Structure

```
analyticsmeta/
├── apps/
│   ├── core/              # Context processors, site settings, base views
│   ├── users/             # User model, profile
│   ├── dashboards/        # Workspaces, DataTables, Records, Dashboards,
│   │                      #   Widgets, middleware, REST API v1 views
│   ├── exports/           # CSV / JSON / Excel export
│   ├── insights/          # AI insight generation (Celery tasks)
│   ├── newsletter/        # Email newsletter management
│   ├── notifications/     # Real-time notification model + context
│   ├── subscriptions/     # Plans, Subscriptions, MpesaTransaction,
│   │                      #   Daraja service, Stripe webhook stub
│   └── workspaces/        # Workspace CRUD, member invitations, roles
├── config/
│   ├── settings/
│   │   ├── base.py        # Shared settings (M-Pesa, Celery, Channels…)
│   │   ├── development.py
│   │   └── production.py
│   ├── asgi.py            # Daphne / Channels ASGI entry point
│   ├── celery.py
│   ├── urls.py
│   └── wsgi.py
├── templates/
│   ├── base.html                      # Public marketing base
│   ├── includes/                      # header, footer, mobile-menu
│   └── dashboard/
│       ├── base_dashboard.html        # SaaS sidebar shell
│       ├── index.html                 # Analytics home
│       ├── tables.html / table_detail.html / table_form.html
│       ├── table_import.html / table_confirm_delete.html
│       ├── record_form.html / record_list.html / record_confirm_delete.html
│       ├── dashboard_detail.html / dashboard_form.html / dashboard_confirm_delete.html
│       ├── workspaces.html / workspace_form.html
│       ├── members.html               # Team management + invitations
│       ├── billing.html               # Plans + M-Pesa payment modal
│       ├── settings.html              # Workspace settings + danger zone
│       ├── profile.html               # User profile
│       └── activity.html             # Audit log timeline
├── static/
├── staticfiles/
├── docker-compose.dev.yml
├── Dockerfile.dev
├── manage.py
├── pyproject.toml
└── package.json
```

---

## Architecture

### Data Model Hierarchy
```
User
 └── WorkspaceMembership (role: owner/admin/editor/viewer)
      └── Workspace (tier, limits)
           ├── DataTable (schema: JSONField)
           │    └── Record (data: JSONField)
           ├── Dashboard
           │    └── Widget (type, config, position)
           ├── Subscription → Plan
           └── MpesaTransaction
```

### Request / WebSocket Flow
```
Browser ──HTTP──► Daphne (ASGI) ──► Django views / DRF
        ──WS───► Channels Consumer ──► Redis channel layer
                                         ──► Celery worker (insights, exports)
```

### Tier Limits
| Tier | Tables | Records/table | Members | Price |
|---|---|---|---|---|
| Free | 5 | 1,000 | 1 | KES 0 |
| Starter | 20 | 10,000 | 5 | KES 2,500/mo |
| Professional | 100 | 100,000 | 20 | KES 6,500/mo |
| Enterprise | 999 | 999,999 | 999 | KES 12,900/mo |

---

## Payment — M-Pesa Daraja

The billing page uses Safaricom's **Lipa Na M-Pesa Online** (STK Push) to process subscription payments.

### Flow
1. User clicks **Pay with M-Pesa** on a plan card
2. Alpine.js modal opens — user enters their Kenyan phone number
3. Frontend POSTs to `/subscriptions/mpesa/stk-push/` → backend calls Daraja STK Push API
4. Safaricom sends a push notification to the user's phone
5. User enters their M-Pesa PIN
6. Frontend polls `/subscriptions/mpesa/status/<checkout_request_id>/` every 4 seconds
7. On `completed` → workspace limits are upgraded immediately; receipt number displayed
8. Safaricom also POSTs the callback to `/subscriptions/mpesa/callback/` (backup confirmation)

### Relevant files
| File | Purpose |
|---|---|
| `apps/subscriptions/mpesa_service.py` | `MpesaService` — OAuth token, STK Push, STK Query, phone normaliser |
| `apps/subscriptions/views.py` | `mpesa_stk_push`, `mpesa_callback`, `mpesa_payment_status` |
| `apps/subscriptions/models.py` | `MpesaTransaction` model |
| `templates/dashboard/billing.html` | Plan cards + Alpine.js payment modal |

### Sandbox testing
1. Get sandbox credentials from [Safaricom Developer Portal](https://developer.safaricom.co.ke)
2. Set `MPESA_CONSUMER_KEY` and `MPESA_CONSUMER_SECRET` in `.env`
3. Expose your local server publicly: `ngrok http 8000`
4. Set `MPESA_CALLBACK_URL=https://<your-ngrok>.ngrok.io/subscriptions/mpesa/callback/`
5. Use the sandbox test phone number provided in the Daraja portal

### Going to production
```env
MPESA_SANDBOX=False
MPESA_CONSUMER_KEY=<production_key>
MPESA_CONSUMER_SECRET=<production_secret>
MPESA_SHORTCODE=<your_paybill>
MPESA_PASSKEY=<your_passkey>
MPESA_CALLBACK_URL=https://yourdomain.com/subscriptions/mpesa/callback/
```

---

## REST API

Base URL: `/api/v1/`

All endpoints require authentication (session or Bearer JWT).

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

GET    /api/v1/tables/{id}/records/   (paginated)
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

---

## Security

- CSRF protection on all state-changing requests
- `mpesa_callback` is `@csrf_exempt` (Safaricom posts without a CSRF token) — payload is validated by receipt structure
- SQL injection prevention via Django ORM
- XSS protection with Django's template auto-escaping
- Rate limiting on authentication endpoints
- Secure password hashing (PBKDF2, 600,000 iterations)
- OAuth2 — Google and LinkedIn via django-allauth
- JWT Bearer tokens for API
- Audit log (`apps.notifications`) for all actions
- Email verification required for new accounts

---

## Troubleshooting

**`TemplateSyntaxError: Invalid filter`**
Django does not have a built-in `split` filter. If you see this, a template is using `|split:","` which was removed in the codebase cleanup.

**M-Pesa STK Push not reaching phone**
- Verify `MPESA_CONSUMER_KEY` and `MPESA_CONSUMER_SECRET` are set
- The callback URL must be HTTPS and publicly reachable (use ngrok locally)
- Check sandbox phone numbers in the Daraja test credentials section

**Database connection error with Docker**
Inline comments in `.env` break python-decouple:
```env
# Wrong  — the comment becomes part of the value
PG_DATABASE_HOST_DEV=postgres  # Docker service name

# Correct
# Docker service name
PG_DATABASE_HOST_DEV=postgres
```

**Celery tasks not executing**
```bash
docker compose -f docker-compose.dev.yml exec redis redis-cli ping   # should return PONG
docker compose -f docker-compose.dev.yml logs celery-worker
```

**Static files missing**
```bash
python manage.py collectstatic --noinput
```

**Migration conflicts**
```bash
python manage.py showmigrations
python manage.py migrate --run-syncdb
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit using [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` new feature
   - `fix:` bug fix
   - `docs:` documentation only
   - `refactor:` no behaviour change
   - `chore:` maintenance
4. Push and open a Pull Request

---

## License

[Add your license here]

## Contact

- **Maintainer**: sniphermarube@gmail.com
- **Issues**: https://github.com/snipher-marube/bst/issues

---

Built with Django · Tailwind CSS · Alpine.js · M-Pesa Daraja API
