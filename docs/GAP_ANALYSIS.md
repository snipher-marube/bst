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

### 2. ~~PDF Report Rendering is a Stub~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `apps/reports/generators.py`: `build_dashboard_pdf()` — full ReportLab A4 layout: cover page, executive summary (KPI grid + top 3 insights), per-widget sections (chart PNG + data table ≤ 15 rows), AI Insights appendix, branded header/footer on every page.
- `apps/reports/charts.py`: matplotlib-based server-side chart rendering — `render_line_chart`, `render_bar_chart`, `render_pie_chart`, `render_kpi_card`, `extract_chart_data`. Non-interactive `Agg` backend; returns PNG bytes for embedding in PDF.
- `apps/reports/tasks.generate_pdf_report`: now calls `build_dashboard_pdf()`, persists PDF to `MEDIA_ROOT/reports/<workspace>/<job>.pdf`, sets `job.pdf_path` + `job.status = done`.
- `reportlab` and `matplotlib` already in `requirements.txt` (both pinned).

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

### 7. ~~Direct Database Connectors Missing~~ ✅ CLOSED 2026-04-15

**Fix applied (PostgreSQL):**
- `DataSource` model in `apps/dashboards/models.py`: UUID PK, `connector_type` (postgresql/mysql), `host/port/database/username`, Fernet-encrypted `_password` (same scheme as WebhookEndpoint secrets), `ssl_mode`, `extra_options`, `last_tested_at/ok/error`, soft-delete via `is_active`.
- `get_plaintext_password()` / `set_password()` / `get_dsn()` helpers on the model.
- `DataSourceQueryEngine` in `apps/dashboards/services.py`: routes widget queries against external PostgreSQL databases. Supports metric, table, chart widget types. Translates structured `query_config` to parameterised SQL — no raw SQL from user input possible. Identifier safety validated via `_SAFE_FIELD_RE`. Statement timeout enforced (30 s). `list_tables()` + `list_columns()` for schema discovery.
- `Widget.data_source` FK + `Widget.source_table_name` CharField — `QueryEngine.execute_widget_query` routes to `DataSourceQueryEngine` when these are set.
- REST API: `GET/POST /api/v1/workspaces/<id>/data-sources/`, `GET/PATCH/DELETE /api/v1/data-sources/<id>/`, `POST /api/v1/data-sources/<id>/test/`, `GET /api/v1/data-sources/<id>/schema/`.
- Migration 0010: `DataSource` table + `Widget` connector fields.
- 23 tests added (model encryption roundtrip, CRUD API, connection test OK/fail, schema endpoint, query engine metric/table/chart/injection-safety).
- **Note:** MySQL support is scaffolded (connector_type choice) but the driver is not yet wired. BigQuery/Snowflake deferred.

---

### 8. ~~Google Sheets Connector Missing~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `DataSource.CONNECTOR_GOOGLE_SHEETS = 'google_sheets'` added to `DataSource` model (migration 0012).
- Two new encrypted fields: `google_credentials_enc` (service-account JSON, Fernet) and `google_spreadsheet_id`.
- DB connector fields (`host`, `database`, `username`) made optional (`blank=True, default=''`) so they are not required for Sheets.
- `set_google_credentials()` / `get_google_credentials()` encrypt/decrypt with the same Fernet scheme as webhook secrets.
- `GoogleSheetsQueryEngine` in `services.py`: authenticates via `google-auth` service account, reads worksheets as pandas DataFrames with `gspread`, supports all aggregation types (count/sum/avg/min/max), group-by, filters, table mode, and column-type inference.
- `QueryEngine.execute_widget_query()`: routes to `GoogleSheetsQueryEngine` when `connector_type == 'google_sheets'`.
- `DataSourceSerializer`: updated — `google_credentials_json` (write-only), `has_google_credentials` (read-only), `google_spreadsheet_id`; `validate()` enforces the right required fields per connector type.
- `DataSourceTestAPIView` / `DataSourceSchemaAPIView`: route to `GoogleSheetsQueryEngine.test_connection()` / `list_tables()` / `list_columns()` for Sheets.
- `GoogleSheetsConnectView` at `/dashboard/integrations/google-sheets/` — step-by-step setup guide + connection form (Alpine.js, test-before-save flow).
- `settings.html`: "Data Connectors" card with direct link to the Google Sheets setup page.
- Dependencies added to `requirements.txt`: `gspread>=6.0.0`, `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0`.

---

### 9. ~~Dashboard-Level Filter Bar (Cross-Widget Filters)~~ ✅ CLOSED 2026-04-14

**Fix applied:**
- `Dashboard.filter_config` JSONField added (migration 0008). Schema: `{"filters": [{"field", "label", "type", "options"?}]}`
- `QueryEngine.execute_widget_query()` now accepts `extra_filters=None`; merged with widget-level filters before DB query. Cache key includes extra_filters; baseline cache bypassed when extra_filters present.
- `_execute_metric_query`, `_execute_table_query`, `_execute_chart_query` all accept and apply `extra_filters`.
- `WidgetDataAPIView.get()`: reads `?filters=<json>` query param; validates it is a JSON array; returns 400 otherwise.
- `DashboardFilterConfigAPIView` added: `PATCH /api/dashboards/<id>/filter-config/`; saves filter_config on dashboard.
- `DashboardDetailView.get_context_data()`: includes `filter_config` in `dashboard_data` JSON.
- `dashboard_detail.html`: Alpine.js `filterBar()` component renders select/date/text controls from `filter_config.filters`; calls `refreshWidget()` for all widgets with active filters on change; debounced text inputs; "Clear" button resets all.
- 12 tests added (model field, PATCH endpoint, QueryEngine extra_filters, WidgetDataAPIView filter param validation).

---

### 10. ~~Scheduled Report Delivery~~ ✅ CLOSED 2026-04-14

**Fix applied:**
- `ReportSchedule` model added to `apps/reports/models.py` (migration 0003): `cron_expression`, `recipients` (JSONField), `report_format`, `is_active`, `last_sent_at`, `next_send_at`, `created_by`.
- `compute_next_send(after=None)` method on `ReportSchedule`: uses `croniter` to advance `next_send_at`; returns `None` and logs warning if cron is invalid or library missing. `croniter>=6.0.0` added to `requirements.txt`.
- `process_report_schedules` Celery task (name: `apps.reports.tasks.process_report_schedules`): queries schedules with `next_send_at__lte=now`, creates a `ReportJob` per due schedule, fires `deliver_scheduled_report.delay()`, advances `next_send_at`. Registered in `CELERY_BEAT_SCHEDULE` at `*/15` minute cadence.
- `deliver_scheduled_report` task: triggers `generate_pdf_report.delay()` if job is pending, retries (max 3×60s) until done/failed, then emails PDF to all recipients via `django.core.mail.EmailMessage`. Skips if no recipients or job failed.
- 8 tests added covering: `compute_next_send` valid/invalid, Beat task fire/skip/inactive logic, email delivery and skip paths.

---

### 11. ~~Incremental / Delta Imports~~ ✅ CLOSED 2026-04-14

**Fix applied:**
- `ImportJob` model: added `import_mode` (replace/append/upsert, default=append) and `primary_key_field` fields (migration 0009).
- `DataImportService.import_data()`: now accepts `import_mode` and `primary_key_field`. Replace mode soft-deletes all existing active records before inserting. Upsert mode uses row-by-row `update/create` keyed on `primary_key_field`; rows missing the PK value fall back to insert. Append and replace modes use the fast bulk_create path.
- `TableImportAPIView`: reads `import_mode` and `primary_key` from form data; validates; passes to `ImportJob`. Returns 400 if `upsert` mode specified without `primary_key`.
- `WebhookIngestView`: reads `?import_mode=` and `?primary_key=` query params. All three modes supported. Wraps all record writes in `transaction.atomic()`.
- `run_async_import` Celery task: passes `import_mode` / `primary_key_field` from `ImportJob` to `DataImportService`.
- 8 tests: append preserves existing, replace soft-deletes, upsert update/insert/mixed, webhook mode validation.

---

### 12. ~~SSO / SAML Authentication~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `SSOConfiguration` OneToOneField on `Workspace` (migration `workspaces/0005`): stores `idp_entity_id`, `idp_sso_url`, `idp_slo_url`, `idp_x509_cert`, `sp_entity_id`, attribute mappings, `is_active`, `require_sso`, `auto_provision`.
- `apps/core/sso.py`: full SAML 2.0 flow via `python3-saml` — `_build_saml_settings()`, `sso_metadata` (SP metadata XML), `sso_login` (AuthnRequest initiation), `sso_acs` (CSRF-exempt ACS: validates signature, extracts email, finds/creates user, provisions workspace membership, sets session), `sso_slo` (SLO initiation).
- `apps/core/sso_detect.py`: email-based IdP detection at `/sso/login/` — maps email domain to workspace SSO config, redirects to correct IdP or shows workspace picker for multi-workspace domains.
- `apps/core/sso_urls.py`: URL namespace `sso` with 5 patterns — `/sso/login/`, `/sso/<workspace_id>/metadata/`, `/sso/<workspace_id>/login/`, `/sso/<workspace_id>/acs/`, `/sso/<workspace_id>/slo/`.
- `SSOSettingsView` at `/dashboard/sso/` — admin-only CRUD form (owner/admin role required). Displays SP metadata URL, ACS URL, and Entity ID for IdP registration. Setup guide accordion for Okta, Azure AD, Google Workspace.
- Templates: `sso/login.html`, `sso/error.html`, `sso/pick_workspace.html`, `dashboard/sso_settings.html`.
- `settings.html` Data Connectors card: added SSO link.
- `config/settings/base.py`: `SAML_SP_CERT` / `SAML_SP_KEY` env vars (optional; enables AuthnRequest signing when set).
- `python3-saml>=1.16.0` added to `requirements.txt`.

---

### 13. ~~Batch Operations API~~ ✅ CLOSED 2026-04-14

**Fix applied:**
- `RecordBatchAPIView` added to `apps/dashboards/api_v1.py`.
- Route: `POST /api/v1/tables/<table_id>/records/batch/`
- Accepts a JSON array of up to 500 operation objects, each with an `op` field (`create | update | upsert | delete`).
- All operations run inside a single `transaction.atomic()` block so partial failures do not leave dirty data.
- Response: `{created, updated, deleted, errors[]}`. Errors include `{index, op, error}` for debugging.
- URL registered in `urls_api_v1.py`.
- Fixed latent bug in `permissions.py`: `HasWorkspaceAccess`, `CanEditData`, `CanManageWorkspace` all called `request.data.get()` which fails when the request body is a JSON array; guarded with `isinstance(request.data, dict)`.
- 11 tests covering all four ops, validation, and mixed success/error batch.

---

### ~~14. Custom Calculated Fields / Metrics~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `CalculatedField` model (`dashboards.0013_calculatedfield`) — UUID PK, ForeignKey to `DataTable`,
  `name` (slug), `display_name`, `expression`, `format_type` (number/currency/percentage/integer),
  `description`.  `unique_together = ['table', 'name']`.
- `CalculatedFieldEvaluator` service class (in `services.py`):
  - `evaluate_against_context(expr, names)` — widget-level: expression references other aggregation
    result names; evaluated with `simpleeval` (blocks `import`, attribute access, etc.).
  - `evaluate_against_records(expr, records)` — table-level: two-pass regex pre-computes
    `sum(col)`, `count(col)`, `avg(col)`, `min(col)`, `max(col)` from the record list, then
    evaluates the resulting arithmetic.  `count()` correctly counts non-null values regardless
    of type.
  - `validate_expression(expr)` — syntax-only check without real data; `ZeroDivisionError` is
    treated as valid (will only occur at evaluation time with real zeros).
- `Widget.ALLOWED_AGG_TYPES` extended with `'calculated'`; `clean()` validates that
  `calculated` aggregations provide an `expression` (max 512 chars) instead of a `field`.
- `QueryEngine._execute_metric_query` and `_execute_chart_query` resolve `type: "calculated"`
  aggregations in a second pass after base aggregations are computed.
- REST API: `GET/POST /api/v1/tables/<id>/calculated-fields/`,
  `PATCH/DELETE /api/v1/calculated-fields/<id>/`,
  `POST /api/v1/calculated-fields/<id>/preview/`,
  `POST /api/v1/tables/<id>/calculated-fields/validate/`.
- UI: `/dashboard/tables/<id>/calculated-fields/` — Alpine.js CRUD manager with expression
  validation, column-chip quick-insert, live result preview modal.
- "Calc Fields" button added to `table_detail.html` action bar.
- `simpleeval>=1.0.0` added to `requirements.txt` (prior session).

**Effort:** High (1 week) | **Impact:** High — advanced analytics gap

---

### ~~15. Cohort, Funnel & Retention Analysis~~ ✅ CLOSED 2026-04-16

**Fix applied:**
- `Widget.WIDGET_TYPES` extended with `('cohort', 'Cohort Retention')` and `('funnel', 'Funnel Analysis')`.
  `Widget.clean()` skips aggregation validation for these two types (they use their own `query_config` schema).
- `CohortAnalysisEngine` (in `services.py`): pandas-based retention matrix. Groups users by first-seen period
  (day/week/month), computes period offsets via `pd.Period` arithmetic, counts distinct users per
  (cohort × offset) bucket. Returns `cohort_labels`, `period_labels`, `matrix` (absolute counts + percentages),
  `total_users`. Supports optional pre-filter. Max 50 k records.
- `FunnelAnalysisEngine` (in `services.py`): multi-step sequential funnel. Each step defines filter predicates;
  engine resolves distinct users per step. When `ordered=True` (default), a user must pass step N before
  counting at step N+1. Annotates `conversion_rate` (step-over-step), `overall_rate` (vs. step 1),
  `dropped`. Max 50 k records.
- `QueryEngine.execute_widget_query` routes `cohort` → `CohortAnalysisEngine.execute()` and
  `funnel` → `FunnelAnalysisEngine.execute()`.
- REST API:
  - `POST /api/v1/tables/<id>/cohort-analysis/` — ad-hoc cohort query
  - `POST /api/v1/tables/<id>/funnel-analysis/` — ad-hoc funnel query
- UI: `templates/dashboard/cohort_funnel.html` — Alpine.js two-tab interface:
  - Cohort tab: configures `user_field`, `event_date_field`, `period`, `periods`; renders a colour-coded
    heatmap retention matrix with absolute/percentage toggle and legend.
  - Funnel tab: step builder with per-step filter rows; renders horizontal drop-off bar chart and
    summary table with conversion rates.
- Navigation: "Cohort & Funnel" entry added to table_detail.html action bar "More" dropdown.
  View: `CohortFunnelView` at `/dashboard/tables/<uuid>/cohort-funnel/`.
- 20 tests added (CohortAnalysisEngine: 6; FunnelAnalysisEngine: 6; API: 7; Widget type validation: 4).

**Effort:** High (2 weeks) | **Impact:** High — product analytics use case

---

### 16. ~~Monitoring & Observability~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `apps/core/metrics.py`: custom Prometheus counters/histograms via `prometheus_client` (HTTP requests, HTTP latency, Celery task counts by state, Celery task duration, import job counts, insight generation counts, anomaly counts). Celery task lifecycle signals wired at app startup.
- `apps/core/middleware.py`: `PrometheusMiddleware` — records `http_requests_total` and `http_request_duration_seconds`; converts concrete paths to URL templates to prevent cardinality explosion. Added as first middleware in `MIDDLEWARE`.
- `apps/core/views.py`: `metrics_view` at `GET /metrics/` — serves `text/plain; version=0.0.4` Prometheus scrape format. Restrict at reverse-proxy in production.
- `apps/core/apps.py`: `CoreConfig.ready()` imports metrics module to register signals at startup.
- `sentry-sdk[django]` + `prometheus-client` added to `requirements.txt`.
- Sentry init gated on `SENTRY_DSN` env var: Django, Celery, Redis, and Logging integrations wired. `SENTRY_TRACES_SAMPLE_RATE` defaults to 5%. PII disabled.
- `structlog` configured project-wide for JSON structured logging (was installed but not configured).
- Note: `django-prometheus` (pinned to Django <6) is incompatible with our Django 6 stack — custom implementation used instead.

---

### 17. ~~Database Backup Strategy~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `docs/BACKUP_RUNBOOK.md` created: daily pg_dump cron job, WAL archiving via `archive_command`, point-in-time recovery procedure, retention policy (7 daily + 4 weekly), restoration test checklist.
- Render.com: `disk` service in `render.yaml` with `backupRetentionDays: 7`; documented manual snapshot procedure via Render dashboard.
- Media files: `MEDIA_ROOT` backup script targets PDF reports directory; rsync to S3-compatible object store.
- Runbook covers: backup verification, full restore walkthrough, partial restore from WAL, contact escalation path.

**How to apply:** See `docs/BACKUP_RUNBOOK.md` for the complete runbook.

---

### 18. ~~Anomaly Detection — Auto-Triggered~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `auto_trigger_anomaly_detection` Celery task added to `apps/insights/tasks.py`. Fan-out task: queries all active workspaces with at least one active table + records; fires `analyze_workspace_tables.delay()` for each.
- `_notify_anomaly_insights` helper added: creates `Notification` records for all workspace members when anomaly-type insights are found during the daily run. Notification links to `/insights/` with metadata (anomaly count, source tables).
- `CELERY_BEAT_SCHEDULE`: `auto-anomaly-detection-daily` registered at `crontab(hour=3, minute=0)` UTC.
- 8 tests added (fan-out dispatch, workspace filtering, notification creation, member-scoped single notification).

---

### 19. ~~Dark Mode~~ ✅ CLOSED 2026-04-15

**Fix applied:**
- `static/css/dark.css`: comprehensive CSS override approach — body/page, dashboard shell, topbar, cards, border colours, text palette (gray-500→gray-900), form inputs/selects, tables (header/rows/hover), shadows, badges, scrollbars, smooth 0.2 s transitions. No Tailwind rebuild required.
- `templates/base.html`: `<html x-data="themeManager()" :class="{ dark: isDark }">` + pre-paint inline script reads `localStorage['am-theme']` and applies `.dark` before first paint (prevents flash-of-wrong-theme). `AMTheme` vanilla JS object handles apply/toggle; `themeManager()` Alpine component bridges to reactive bindings.
- `templates/includes/header.html` + `templates/dashboard/base_dashboard.html`: toggle button (`#theme-toggle-btn`) + icon (`#theme-icon`) wired to `AMTheme.toggle()`. Icon flips moon↔sun. Preference persisted to `localStorage['am-theme']`. OS `prefers-color-scheme` respected when no stored preference.

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
