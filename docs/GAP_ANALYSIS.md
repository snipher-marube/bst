# AnalyticsMeta — Gap Analysis & Roadmap to Robust Adoption

> Last updated: 2026-04-14
> Codebase health score: **7.4 / 10** (production-ready MVP; gaps below block competitive positioning)

---

## How to Read This Document

Gaps are organized into three priority tiers:

| Tier | Label | Meaning |
|------|-------|---------|
| P0 | **Critical** | Blocks launch, introduces security risk, or makes a paid feature unusable |
| P1 | **Important** | Required for enterprise adoption or feature parity with Metabase / Redash / Looker |
| P2 | **Polish** | Quality-of-life improvements, market differentiation, accessibility |

Each gap has an **Impact** (user/business effect) and **Effort** (engineering complexity) rating.

---

## P0 — Critical (Fix Before GA Launch)

### 1. ~~Stripe Webhook Signature Verification is Commented Out~~ ✅ CLOSED 2026-04-14

**Fix applied:** `stripe` package added to requirements; `stripe.Webhook.construct_event()` now enforced
in production (when `STRIPE_WEBHOOK_SECRET` is set). Dev mode logs a warning and skips verification.
`STRIPE_WEBHOOK_SECRET` moved to `base.py` so it resolves in all environments. 4 new tests added.

---

### 2. PDF Report Rendering is a Stub

**File:** `apps/reports/tasks.py`
**Problem:** `ReportJob` model exists, PDF task is scaffolded but returns nothing. Users on paid plans who trigger PDF reports get a job that never completes.
**Fix:** Implement ReportLab / WeasyPrint rendering inside the Celery task; render dashboard widget data to a structured PDF layout
**Effort:** Medium (2–3 days) | **Impact:** High — paid feature unusable

---

### 3. ~~No Grace Period on Subscription Failure~~ ✅ CLOSED 2026-04-14

**Fix applied:**
- `grace_period_ends_at` + `dunning_stage` fields added to `Subscription` (migration 0004)
- `start_grace_period()` opens 7-day window; `clear_grace_period()` resets on payment recovery
- `_handle_invoice_failed` now calls `start_grace_period()` + fires day-0 email via `send_dunning_email_task.delay()`
- `_handle_invoice_paid` now calls `clear_grace_period()` to cancel the dunning sequence
- `apps/subscriptions/tasks.py`: `process_grace_periods` Celery Beat task (daily 08:00 UTC)
  advances dunning stage (day-3 → day-7) and downgrades expired workspaces to free
- `apps/subscriptions/emails.py`: `send_dunning_email()` direct transactional email helper
- `templates/notifications/email/billing_dunning.html`: branded dunning email template
- 10 new tests added (model methods, Stripe webhook integration, task sequence)

---

### 4. ~~Webhook Secret Stored in Plaintext~~ ✅ CLOSED 2026-04-14

**Fix applied:** Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) via `apps/dashboards/webhook_crypto.py`.
Key derived from `settings.SECRET_KEY` via SHA-256. Migration 0007 widened column + encrypted all existing rows.
`get_plaintext_secret()` decrypts on demand for HMAC verification. Plaintext returned only on creation/regeneration.
Serializer no longer exposes the `secret` field in list/detail responses. Factory updated. 13 tests passing.

---

### 5. ~~No File Upload Rate Limiting~~ ✅ ALREADY IMPLEMENTED (verified 2026-04-14)

`FileUploadThrottle(UserRateThrottle, scope='file_upload')` existed and was applied to `TableImportAPIView`.
`DEFAULT_THROTTLE_RATES['file_upload'] = '30/hour'` configured in settings. Also fixed a pre-existing
`NameError` (`settings` not imported in `api_v1.py`). 3 tests added verifying throttle class,
rate config, and 429 response behaviour.

---

### 6. ~~Celery Beat Scheduler Removed~~ ✅ NOT A GAP (2026-04-14)

Commit `365da45` only removed binary SQLite beat-schedule files from git tracking (correct hygiene).
`CELERY_BEAT_SCHEDULE` is defined in settings, `celery-beat` service is in both compose files, and
tasks are properly registered. No action required.

---

## P1 — Important for Adoption

### 7. Direct Database Connectors Missing

**Current state:** Only CSV / Excel file import + webhook JSON ingestion
**Gap:** Enterprise users want to connect directly to PostgreSQL, MySQL, BigQuery, or Snowflake
**Fix:** Build a `DataSource` model with a `connector_type` enum. Start with PostgreSQL (psycopg3 already installed). Execute user-scoped read-only queries via a connection pool.
**Effort:** High (1–2 weeks) | **Impact:** High — #1 enterprise blocker

---

### 8. Google Sheets Connector Missing

**Gap:** Most SME users manage data in Google Sheets. No native connector.
**Fix:** Google Sheets API v4; use service account credentials stored per workspace. Pull on demand or schedule sync.
**Effort:** Medium (3–4 days) | **Impact:** High — SME market differentiator

---

### 9. Dashboard-Level Filter Bar (Cross-Widget Filters)

**Problem:** Each widget is independently queried. No global time-range picker or dimension filter that applies to all widgets on a dashboard simultaneously.
**Fix:**
- Add `Dashboard.filter_config` JSONField (filter fields + default values)
- Pass active filter values to `Widget.execute_query()` as parameter overrides
- Render filter bar in dashboard detail view (Alpine.js + HTMX)
**Effort:** Medium (3–4 days) | **Impact:** High — core BI usability gap

---

### 10. Scheduled Report Delivery

**Problem:** Reports are generated on demand only. No automated email delivery.
**Fix:**
- Add `ReportSchedule` model (cron expression, recipients, format)
- Celery Beat task: evaluate schedules → generate PDF → email
- UI: schedule form on report detail page
**Effort:** Medium (2–3 days) | **Impact:** High — automation is table stakes for BI tools

---

### 11. Incremental / Delta Imports

**Problem:** Webhook ingestion replaces all records (full overwrite). Large datasets re-import entirely on every update.
**Fix:** Support `upsert` mode keyed on a user-defined `primary_key` field. Add `import_mode` param to import endpoint: `replace | append | upsert`.
**Effort:** Medium (2 days) | **Impact:** Medium-High — efficiency + data integrity

---

### 12. SSO / SAML Authentication

**Problem:** Enterprise clients require SSO via Okta, Azure AD, or Google Workspace SAML.
**Fix:** Integrate `django-allauth` SAML provider or `python3-saml`. Add per-workspace SSO config (metadata URL, entity ID).
**Effort:** High (1 week) | **Impact:** High — hard enterprise requirement

---

### 13. Batch Operations API

**Problem:** Inserting or updating records requires one HTTP call per record. Slow for large syncs.
**Fix:** Add `POST /api/v1/tables/<id>/records/batch/` that accepts an array of up to 500 records. Support `create`, `update`, `upsert`, `delete` operations.
**Effort:** Low (1 day) | **Impact:** Medium — developer experience

---

### 14. Custom Calculated Fields / Metrics

**Problem:** Only pre-defined aggregations (sum, avg, count). No way to define a calculated metric like `revenue_per_user = sum(revenue) / count(user_id)`.
**Fix:** Add `CalculatedField` model with a safe expression parser (e.g., `simpleeval` or a whitelist AST walker). Evaluate at query time.
**Effort:** High (1 week) | **Impact:** High — advanced analytics gap

---

### 15. Cohort, Funnel & Retention Analysis

**Problem:** No event-sequencing or time-bucketed user analysis. Competitors (Mixpanel, Amplitude) own this space.
**Fix:**
- Add `CohortQuery` widget type: segment users by first-event date, measure retention over N periods
- Add `FunnelQuery` widget type: define event sequence, compute drop-off at each step
- Implement via PostgreSQL window functions on `DataRecord`
**Effort:** High (2 weeks) | **Impact:** High — product analytics use case

---

### 16. Monitoring & Observability

**Problem:** Health check endpoints exist but no metrics exported. No alerting on task queue depth or slow queries.
**Fix:**
- Add `django-prometheus` → expose `/metrics` endpoint
- Integrate Sentry for error tracking (`SENTRY_DSN` env var)
- Add Celery task duration + failure rate metrics
- Ship logs to structured format (structlog) for ELK/Loki ingestion
**Effort:** Low-Medium (2 days) | **Impact:** High — operations blind without this

---

### 17. Database Backup Strategy

**Problem:** No backup policy documented or automated. Single PostgreSQL instance = single point of failure.
**Fix:**
- Document pg_dump cron job (daily full + WAL archiving)
- For Render.com: enable automated backups in render.yaml
- For self-hosted: provide backup script + restoration runbook in docs
**Effort:** Low (1 day) | **Impact:** High — data loss risk

---

### 18. Anomaly Detection — Auto-Triggered

**Problem:** Anomaly detection logic exists in `apps/insights/tasks.py` but is never auto-triggered. Users must manually request insights.
**Fix:** Wire anomaly detection into the Celery Beat schedule — evaluate all active workspace tables daily. Create an `Insight` record automatically if anomaly score exceeds threshold. Send a notification.
**Effort:** Low (1 day) | **Impact:** Medium — proactive intelligence is a product differentiator

---

### 19. Dark Mode

**Problem:** Dark mode toggle exists in user settings (template + field) but the CSS is not wired up to a Tailwind `dark:` class strategy.
**Fix:** Add `dark` class to `<html>` tag based on user preference. Ensure all Tailwind utility classes have `dark:` variants. Persist preference via Alpine.js + cookie.
**Effort:** Low-Medium (1–2 days) | **Impact:** Medium — increasingly a baseline expectation

---

### 20. Accessibility Audit (WCAG 2.1 AA)

**Problem:** No accessibility audit has been performed. Keyboard navigation, ARIA labels, color contrast, and focus states are likely incomplete.
**Fix:** Run `axe-core` scan on all main pages. Fix critical violations (contrast, missing labels, focus management). Add `prefers-reduced-motion` support.
**Effort:** Medium (3 days) | **Impact:** Medium — required for government/public sector clients

---

## P2 — Polish & Differentiation

### 21. WhatsApp Alerts (Africa Market Differentiator)

**Problem:** Alerts only go via email. In East Africa, WhatsApp has higher engagement than email.
**Fix:** Integrate Twilio WhatsApp API. Add `whatsapp_number` field to user profile. Add WhatsApp as a notification channel option in `NotificationPreference`.
**Effort:** Medium (2 days) | **Impact:** Medium — strong differentiation for target market

---

### 22. Slack / Teams Notifications

**Problem:** No chat integration. Users who live in Slack miss alerts unless they check email.
**Fix:** Add `SlackIntegration` model per workspace (bot token + channel). Deliver alert notifications + report completions to Slack. Add Teams webhook support.
**Effort:** Medium (2–3 days) | **Impact:** Medium — standard SaaS expectation

---

### 23. Dashboard Templates / Starter Packs

**Problem:** New users face a blank canvas. Onboarding friction is high.
**Fix:** Ship 5 pre-built dashboard templates: E-commerce, SaaS Metrics, HR Dashboard, Finance Overview, Marketing Funnel. Auto-populate with sample data on first import.
**Effort:** Medium (2 days) | **Impact:** Medium — time-to-value for new users

---

### 24. Annual Billing (is_yearly already scaffolded)

**Problem:** `Subscription.is_yearly` field exists but annual pricing is not offered in the UI and no discount is applied.
**Fix:** Add annual price tiers to `Plan.annual_price`. Show toggle in pricing page. Apply 20% discount. Handle proration on mid-year upgrades.
**Effort:** Low (1 day) | **Impact:** Medium — cash flow + retention benefit

---

### 25. In-Dashboard Collaboration (Comments)

**Problem:** No way for team members to annotate data or leave context on a widget.
**Fix:** Add `DashboardComment` model (dashboard FK, widget FK optional, user FK, body, created_at). Render as sidebar thread with @mentions. Send notification on mention.
**Effort:** Medium (3 days) | **Impact:** Low-Medium — collaboration feature

---

### 26. CDN for Static Files

**Problem:** Static files served via WhiteNoise through Nginx. Globally distributed teams see high latency.
**Fix:** Push `collectstatic` output to S3 or Cloudflare R2. Set `STATICFILES_STORAGE` to S3Boto3Storage. Configure cache headers.
**Effort:** Low (1 day) | **Impact:** Low — performance at scale

---

### 27. Keyboard Shortcuts

**Problem:** No keyboard navigation shortcuts in the dashboard editor.
**Fix:** Add global keybinding layer (Alpine.js): `?` = show shortcut help, `n` = new widget, `e` = edit selected, `Esc` = close modal.
**Effort:** Low (1 day) | **Impact:** Low — power user experience

---

## Current State Snapshot

| Category | Score | Key Strength | Key Gap |
|----------|-------|--------------|---------|
| Code Quality | 8/10 | Clean architecture, UUID PKs | Minor security issues |
| Security | 7/10 | Multi-tenancy solid | Stripe sig commented out, secrets in plaintext |
| Testing | 8/10 | 81% coverage, 485 tests | Missing E2E, WebSocket, PDF tests |
| API Design | 8/10 | RESTful, OpenAPI docs, rate-limited | No batch ops, no field-level filter |
| Frontend | 7/10 | PWA, responsive, HTMX | No dark mode, no accessibility |
| Data Pipeline | 6/10 | CSV import, webhook ingestion | No DB connectors, no delta sync |
| Analytics | 7/10 | 9 chart types, AI insights, alerts | No cohort/funnel, no scheduled reports |
| Billing | 6/10 | M-Pesa full impl | Stripe incomplete, no dunning |
| DevOps | 7/10 | Docker, health checks, Render | No monitoring, no backups |
| Multi-tenancy | 9/10 | Strong isolation, RBAC, limits | No SSO/SAML |

**Overall: 7.4 / 10**

---

## Recommended Sequence to Close Gaps

Work top-to-bottom. Each tier should be complete before the next begins.

```
Phase 1 — Harden (P0, ~1 week)
  ├── Fix Stripe webhook verification
  ├── Restore Celery Beat
  ├── Hash webhook secrets in DB
  ├── Add upload rate limiting
  ├── Implement PDF report rendering
  └── Add subscription grace period + dunning

Phase 2 — Complete Core BI (P1a, ~3 weeks)
  ├── Dashboard filter bar (cross-widget params)
  ├── Scheduled report delivery
  ├── Incremental import (upsert mode)
  ├── Batch record operations API
  ├── Auto-triggered anomaly detection
  └── Dark mode + accessibility baseline

Phase 3 — Enterprise-Ready (P1b, ~4 weeks)
  ├── PostgreSQL direct connector
  ├── Google Sheets connector
  ├── SSO / SAML authentication
  ├── Cohort + funnel analysis widgets
  ├── Custom calculated fields
  └── Monitoring (Prometheus + Sentry)

Phase 4 — Market Differentiation (P2, ~2 weeks)
  ├── WhatsApp alerts
  ├── Slack / Teams integration
  ├── Dashboard templates / starter packs
  ├── Annual billing
  ├── CDN for static files
  └── In-dashboard collaboration
```

---

*This document is the source of truth for gap prioritization. Update it as items are closed.*
