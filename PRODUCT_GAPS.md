# AnalyticsMeta — Product Gaps & Implementation Roadmap

**Audit Date:** 2026-04-06  
**Status:** Living document — check off items as completed

---

## How to Use This Document

Work through priorities top to bottom. Each item has:
- **Status** — Not Started / In Progress / Done
- **Impact** — why it matters
- **Effort** — rough complexity (Low / Medium / High)
- **Notes** — implementation hints

Mark status as `[x]` when done, `[~]` when in progress.

---

## P0 — Must Fix Before Launch

### [ ] 1. Onboarding Wizard + Sample Data
**Status:** Not Started  
**Impact:** #1 churn driver — users land on an empty workspace with no guidance  
**Effort:** Medium

**What to build:**
- Multi-step wizard on first login:
  1. Name your workspace
  2. Choose industry (Sales, Finance, HR, Operations, Other)
  3. Auto-import a pre-seeded demo dataset matching their industry
  4. Auto-generate first dashboard from that dataset
  5. "Invite a teammate" prompt
- Persistent "Get Started" checklist card on the home dashboard until all steps completed
- Sample datasets: `sales_2024.csv`, `inventory.csv`, `hr_headcount.csv` in `/fixtures/`

**Files to touch:**
- `apps/workspaces/views.py` — post-create redirect to wizard
- New template: `templates/workspaces/onboarding/wizard.html`
- New view: `WorkspaceOnboardingWizardView`
- New fixture files: `fixtures/sample_data/`

---

### [ ] 2. Real AI/LLM Integration for Insights
**Status:** Not Started  
**Impact:** The "AI insights" feature is currently statistical heuristics only — no LLM is called anywhere. This is the main differentiator vs. Metabase/Redash.  
**Effort:** Medium

**What to build:**
- Wire Anthropic Claude (`claude-sonnet-4-6`) to `analyze_workspace_tables()` Celery task
- Generate plain-English narrative per table: "Your sales grew 23% last month, driven mainly by Nairobi region..."
- Natural language query interface: user types "show me monthly revenue by region" → auto-generates widget config
- Anomaly explanations in natural language (currently just a flag in DB)
- Cost guardrails: token counting + per-workspace monthly LLM budget cap

**Files to touch:**
- `apps/insights/tasks.py` — replace heuristic blocks with LLM calls
- New: `apps/insights/llm.py` — Claude client wrapper, prompt templates
- `apps/insights/models.py` — add `llm_model`, `prompt_tokens`, `completion_tokens` fields
- `.env` — add `ANTHROPIC_API_KEY`

**API:** Use `anthropic` Python SDK, model `claude-sonnet-4-6`

---

### [ ] 3. Email Notifications Wired Up
**Status:** Not Started  
**Impact:** Notification model has 7 types but zero email delivery. Users never know when imports finish or insights are ready.  
**Effort:** Low

**What to build:**
- Wire email sending into `Notification.notify()` classmethod
- Templates for each notification type (import complete, insight ready, team invite, threshold alert)
- Respect user email preferences (opt-out per notification type)
- Use Django's email backend (configure SendGrid or Resend in production)

**Files to touch:**
- `apps/notifications/models.py` — add email send in `notify()` + `email_opt_out` field
- New: `apps/notifications/emails.py` — email template rendering helpers
- New: `templates/notifications/email/` — HTML email templates per type
- `config/settings/base.py` — `EMAIL_BACKEND`, `DEFAULT_FROM_EMAIL`

---

## P1 — Core Product Quality

### [ ] 4. Public Dashboard Viewer
**Status:** Not Started  
**Impact:** `public_uuid` field exists on Dashboard but there is no public-facing URL — sharing with stakeholders who don't have accounts is impossible.  
**Effort:** Low

**What to build:**
- Route: `GET /d/<public_uuid>/` → read-only dashboard view, no login required
- Show all widgets, respect grid layout
- "Powered by AnalyticsMeta" branding footer (virality)
- Optional: password-protect a public dashboard

**Files to touch:**
- `apps/dashboards/views.py` — new `PublicDashboardView`
- `apps/dashboards/urls.py` — add `path('d/<uuid:public_uuid>/', ...)`
- New template: `templates/dashboards/public_view.html`

---

### [ ] 5. Data Alerts & Threshold Notifications
**Status:** Not Started  
**Impact:** Core pain point — business owners don't want to check dashboards, they want to be notified when something goes wrong.  
**Effort:** Medium

**What to build:**
- `Alert` model: workspace, widget, condition (`gt`/`lt`/`eq`/`pct_change`), threshold value, delivery channels (email, WhatsApp, in-app), last_triggered_at
- Celery beat task runs every 15 min, evaluates all active alerts
- Trigger `Notification.notify()` + email when threshold crossed
- UI: "Add Alert" button on each metric/number widget

**Files to touch:**
- New: `apps/alerts/` app (models, views, tasks, urls)
- `apps/dashboards/widgets.py` — add "Add Alert" action
- `config/settings/base.py` — register new app + Celery beat schedule

---

### [ ] 6. Test Suite (Target: 70% Coverage)
**Status:** Not Started  
**Impact:** All test files currently contain only `pass`. Every deployment is untested. Enterprise customers require this.  
**Effort:** High

**What to build:**
- Model tests: field validation, workspace scoping, soft-delete, `__str__`, properties
- API endpoint tests: auth, permissions, CRUD, pagination, filtering
- Celery task tests: import job, insight generation, alert evaluation
- M-Pesa callback tests: idempotency, IP whitelist, amount validation
- Use `pytest` + `factory_boy` (already installed)
- Add GitHub Actions workflow: run tests on every PR

**Files to touch:**
- `apps/*/tests.py` — fill in every empty test file
- New: `.github/workflows/tests.yml`
- `pytest.ini` or `setup.cfg` — pytest config + coverage threshold

---

## P2 — Enterprise & Integration Features

### [ ] 7. Webhook Data Ingestion Endpoint
**Status:** Not Started  
**Impact:** Users currently must manually upload CSV/Excel. A push endpoint enables real-time data from any system.  
**Effort:** Medium

**What to build:**
- `POST /api/v1/tables/<id>/ingest/` — accepts JSON array of records
- API key authentication (not session auth) so external systems can push data
- Schema validation against existing table columns
- Rate limited + payload size limited (max 1000 records per call)
- Zapier/Make webhook trigger documentation

**Files to touch:**
- `apps/dashboards/urls_api_v1.py` — new ingest endpoint
- `apps/dashboards/views_api.py` — `IngestRecordsAPIView`
- `apps/workspaces/models.py` — `WorkspaceAPIKey` model

---

### [ ] 8. PDF Report Renderer
**Status:** Not Started (model + job tracking exist, no renderer)  
**Impact:** Paying customers need to email PDF reports to their board/investors.  
**Effort:** Medium

**What to build:**
- Use `reportlab` (referenced in git history) or `weasyprint`
- Render each widget as a chart image → embed in PDF
- Cover page: workspace name, dashboard name, date range, generated by
- Async: Celery task renders PDF → uploads to Django media storage → notifies user via email
- UI: "Download as PDF" button on dashboard detail

**Files to touch:**
- `apps/reports/tasks.py` — PDF render Celery task
- `apps/reports/renderer.py` — reportlab/weasyprint renderer
- `apps/reports/views.py` — trigger render + download endpoint
- `templates/dashboards/detail.html` — add "Download PDF" button

---

### [ ] 9. Audit Log UI
**Status:** Not Started (immutable `AuditLog` model exists in DB)  
**Impact:** Enterprise and compliance buyers require visibility into who did what and when.  
**Effort:** Low

**What to build:**
- `/dashboard/workspace/activity/` — filterable, paginated audit log table
- Filters: date range, user, action type (create/update/delete/export/share)
- Export audit log as CSV
- Show in workspace settings sidebar

**Files to touch:**
- `apps/workspaces/views.py` — `AuditLogListView`
- `apps/workspaces/urls.py` — add activity URL
- New template: `templates/workspaces/activity.html`

---

### [ ] 10. Workspace Usage Analytics
**Status:** Not Started  
**Impact:** Users need to track their own usage against plan limits (tables used, records imported, team seats).  
**Effort:** Low

**What to build:**
- Usage summary card on billing page: tables X/20, records X/10,000, team members X/5
- Monthly usage chart: records imported per day (from `AuditLog`)
- Popular dashboards: view count from AuditLog VIEW events
- API endpoint: `GET /api/v1/workspaces/<id>/usage/`

**Files to touch:**
- `apps/subscriptions/views.py` — add usage stats to billing context
- `apps/dashboards/urls_api_v1.py` — new usage endpoint
- `templates/subscriptions/billing.html` — usage cards

---

## P3 — Polish & Developer Experience

### [ ] 11. OpenAPI / Swagger Documentation
**Status:** Not Started  
**Impact:** No machine-readable API spec. Blocks integration partnerships and developer adoption.  
**Effort:** Low

**What to build:**
- Install `drf-spectacular`
- Expose `/api/schema/` (YAML), `/api/docs/` (Swagger UI), `/api/redoc/`
- Add `@extend_schema` decorators on all API views
- Add to README

**Files to touch:**
- `requirements.txt` — add `drf-spectacular`
- `config/settings/base.py` — add to `INSTALLED_APPS`, configure
- `config/urls.py` — add schema/docs URLs
- `apps/dashboards/urls_api_v1.py` — add schema view

---

### [ ] 12. WhatsApp Alerts (Africa Market Fit)
**Status:** Not Started  
**Impact:** Your market uses WhatsApp, not Slack or email. This is a genuine differentiator vs. all Western BI tools.  
**Effort:** Medium

**What to build:**
- Integrate WhatsApp Business API (Twilio or Meta direct)
- Add WhatsApp as a delivery channel in the `Alert` model
- User profile: add WhatsApp number field
- Notification settings: enable WhatsApp per notification type
- Format: concise template messages ("AnalyticsMeta Alert: Sales dropped below KES 50,000. Current: KES 42,300. View dashboard →")

**Files to touch:**
- `apps/users/models.py` — `whatsapp_number` field on profile
- `apps/notifications/models.py` — add `whatsapp` channel
- New: `apps/notifications/channels/whatsapp.py`
- `.env` — `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`

---

### [ ] 13. Complete Stripe Integration
**Status:** Scaffolded (fields exist, webhook handler is a mock)  
**Impact:** Global billing. M-Pesa handles Kenya, Stripe handles international.  
**Effort:** High

**What to build:**
- Real Stripe Checkout session creation
- Stripe webhook handler: `invoice.paid`, `customer.subscription.deleted`, `payment_intent.failed`
- Stripe customer portal link for self-service billing management
- Enable Stripe signature verification (currently commented out)
- Handle failed payments gracefully (grace period, dunning emails)

**Files to touch:**
- `apps/subscriptions/views.py` — replace mock `upgrade_plan` with Stripe Checkout
- `apps/subscriptions/webhooks.py` — real event handling
- `config/settings/production.py` — `STRIPE_WEBHOOK_SECRET`

---

### [ ] 14. Direct Data Source Connectors
**Status:** Not Started  
**Impact:** CSV upload is high friction. Direct DB and Google Sheets connections unlock enterprise use cases.  
**Effort:** High

**What to build (phase 1):**
- Google Sheets connector (OAuth → pull sheet as DataTable, schedule refresh)
- PostgreSQL read-only connector (connection string → query → DataTable)

**What to build (phase 2):**
- MySQL, Airtable, REST API as data source

**Files to touch:**
- New: `apps/connectors/` app
- New: `apps/connectors/sources/google_sheets.py`, `postgres.py`
- `config/settings/base.py` — register app

---

## Tracking Summary

| # | Feature | Priority | Status | Effort |
|---|---------|----------|--------|--------|
| 1 | Onboarding wizard + sample data | P0 | Not Started | Medium |
| 2 | Real AI/LLM integration | P0 | Not Started | Medium |
| 3 | Email notifications | P0 | Not Started | Low |
| 4 | Public dashboard viewer | P1 | Not Started | Low |
| 5 | Data alerts & thresholds | P1 | Not Started | Medium |
| 6 | Test suite (70% coverage) | P1 | Not Started | High |
| 7 | Webhook data ingestion | P2 | Not Started | Medium |
| 8 | PDF report renderer | P2 | Not Started | Medium |
| 9 | Audit log UI | P2 | Not Started | Low |
| 10 | Workspace usage analytics | P2 | Not Started | Low |
| 11 | OpenAPI / Swagger docs | P3 | Not Started | Low |
| 12 | WhatsApp alerts | P3 | Not Started | Medium |
| 13 | Complete Stripe integration | P3 | Not Started | High |
| 14 | Direct data source connectors | P3 | Not Started | High |

---

## Strategic Positioning Note

> **The unique angle:** AnalyticsMeta is the only zero-code BI platform built for African SMEs —
> M-Pesa billing, WhatsApp alerts, AI insights that work on messy/incomplete data (not clean warehouses),
> and a mobile-first experience that works on slow connections.
>
> Every feature decision should reinforce this. When in doubt, ask: does this serve a business owner
> in Nairobi checking their sales on a phone? If yes, prioritize it.
