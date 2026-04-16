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

### [x] 1. Onboarding Wizard + Sample Data
**Status:** Done (2026-04-06)  
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

### [x] 2. Real AI/LLM Integration for Insights
**Status:** Done (2026-04-11)  
**Impact:** The "AI insights" feature now calls Claude (`claude-sonnet-4-6`) to produce plain-English narratives alongside statistical analysis. This is the main differentiator vs. Metabase/Redash.  
**Effort:** Medium

**What was built:**
- `apps/insights/llm.py` — `ClaudeInsightGenerator` wraps the Anthropic SDK; gracefully disabled when `ANTHROPIC_API_KEY` is empty (falls back to heuristic descriptions)
- `WorkspaceLLMBudget` — per-workspace monthly token cap enforced before each API call; configurable via `LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET` env var (default: 100,000 tokens)
- `apps/insights/tasks.py` — statistical analysis runs first; LLM narrative generated from the stats dict; both stored on `Insight`
- `apps/insights/models.py` — migration `0002_llm_fields` adds `llm_model`, `prompt_tokens`, `completion_tokens` fields
- `config/settings/base.py` — `ANTHROPIC_API_KEY` and `LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET` settings
- 455-line test suite in `apps/insights/tests.py` covers LLM path, fallback path, and budget enforcement

**Docs:** [docs/ai-insights.md](docs/ai-insights.md)

---

### [x] 3. Email Notifications Wired Up
**Status:** Done (2026-04-06)  
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

### [x] 4. Public Dashboard Viewer
**Status:** Done (2026-04-11)  
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

### [x] 5. Data Alerts & Threshold Notifications
**Status:** Done (2026-04-12)  
**Impact:** Core pain point — business owners don't want to check dashboards, they want to be notified when something goes wrong.  
**Effort:** Medium

**What was built:**
- `DataAlert` model in `apps/dashboards/models.py`: workspace-scoped, targets a field + aggregate + operator + threshold with configurable cooldown
- `check_data_alerts` Celery beat task in `apps/dashboards/tasks.py` runs every 15 min, fires `Notification` for all workspace members when triggered, respects `cooldown_minutes`
- `DataAlertSerializer` + full CRUD API: `GET/POST /api/v1/workspaces/<uuid>/alerts/`, `GET/PATCH/DELETE /api/v1/alerts/<uuid>/`
- Migration `0006_dataalert_webhookendpoint` creates the table
- 13 tests covering model logic and API endpoints

**Docs:** [docs/data-alerts.md](docs/data-alerts.md)

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

### [x] 7. Webhook Data Ingestion Endpoint
**Status:** Done (2026-04-12)  
**Impact:** Users can now push data from any external system (Zapier, Make, custom scripts) in real time without uploading CSV.  
**Effort:** Medium

**What was built:**
- `WebhookEndpoint` model in `apps/dashboards/models.py`: per-workspace, per-table, with a 64-char random token and HMAC-SHA256 signing secret
- Public `WebhookIngestView` at `POST /webhook/ingest/<token>/` — no auth required; verified via `X-Hub-Signature-256` header; accepts JSON object (single record) or JSON array (up to 1000 records); updates `total_requests` and `last_request_at` atomically
- Full CRUD API: `GET/POST /api/v1/workspaces/<uuid>/webhooks/`, `GET/PATCH/DELETE /api/v1/webhooks/<uuid>/`, `POST /api/v1/webhooks/<uuid>/regenerate-secret/`
- Inactive endpoints return HTTP 404; bad/missing HMAC returns HTTP 401
- Migration `0006_dataalert_webhookendpoint` creates the table
- 10 tests covering HMAC validation, array payloads, telemetry, and lifecycle

**Docs:** [docs/webhooks.md](docs/webhooks.md)

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

### [x] 9. Audit Log API
**Status:** Done (2026-04-12) — REST API layer complete; HTML UI still pending  
**Impact:** Enterprise and compliance buyers require visibility into who did what and when.  
**Effort:** Low

**What was built:**
- `AuditLogListAPIView` at `GET /api/v1/workspaces/<uuid>/audit-logs/`
- Access restricted to workspace owners and admins (`CanManageWorkspace` permission)
- Filterable by `?action=create|update|delete|view|export|share` and `?content_type=DataTable|Dashboard|...`
- Paginated (default 50, max 200 per page)
- Returns `user_email`, `action`, `content_type`, `object_repr`, `changes`, `ip_address`, `timestamp`
- 4 tests covering list, filtering, and role-based access

**Still outstanding:** HTML template at `/dashboard/workspace/activity/` — see [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md)

---

### [x] 10. Workspace Usage Analytics
**Status:** Done (2026-04-12) — REST API complete; billing page UI still pending  
**Impact:** Users can track their own usage against plan limits via the API.  
**Effort:** Low

**What was built:**
- `WorkspaceUsageAPIView` at `GET /api/v1/workspaces/<uuid>/usage/`
- Returns: `total_tables`, `total_records`, `estimated_storage_mb`, `member_count`, `insight_count`, `recent_import`
- Accessible to all workspace members
- 3 tests covering aggregation correctness

**Still outstanding:** Usage cards on the billing page template — see [DOCUMENTATION_GAPS.md](DOCUMENTATION_GAPS.md)

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
| 1 | Onboarding wizard + sample data | P0 | Done | Medium |
| 2 | Real AI/LLM integration | P0 | Done | Medium |
| 3 | Email notifications | P0 | Done | Low |
| 4 | Public dashboard viewer | P1 | Done | Low |
| 5 | Data alerts & thresholds | P1 | Done | Medium |
| 6 | Test suite (70% coverage) | P1 | Done | High |
| 7 | Webhook data ingestion | P2 | Done | Medium |
| 8 | PDF report renderer | P2 | Not Started | Medium |
| 9 | Audit log API (UI pending) | P2 | In Progress | Low |
| 10 | Workspace usage analytics (UI pending) | P2 | In Progress | Low |
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
