# Documentation Gaps — AnalyticsMeta

Tracks what was missing and what has been written to close each gap.
Last updated: 2026-04-08

---

## Status key

- ✅ **Closed** — documentation written
- ⚠️ **Partial** — documented but needs expansion over time
- ❌ **Open** — not yet documented

---

## 1. Environment Configuration

| Item | Status | Location |
|---|---|---|
| `.env.example` was empty | ✅ Closed | `.env.example` — all variables with inline guidance |
| Dev vs prod variable naming (`_DEV` suffix) | ✅ Closed | `.env.example` — clearly split into dev/prod sections |
| New `ADMIN_NAME` / `ADMIN_EMAIL` vars added to production.py | ✅ Closed | `.env.example` |
| Google OAuth redirect URIs | ✅ Closed | `docs/oauth-setup.md` |
| LinkedIn OAuth redirect URIs | ✅ Closed | `docs/oauth-setup.md` |
| Django admin social app setup (sites framework) | ✅ Closed | `docs/oauth-setup.md` |

---

## 2. API Documentation

| Item | Status | Location |
|---|---|---|
| README said "JWT" but API uses DRF Token auth | ✅ Closed | `docs/api-authentication.md` |
| How to obtain and use an API token | ✅ Closed | `docs/api-authentication.md` |
| Request/response examples for common endpoints | ✅ Closed | `docs/api-authentication.md` |
| Full endpoint list | ⚠️ Partial | `README.md` has routes; no OpenAPI/Swagger schema yet |
| OpenAPI / Swagger / drf-spectacular schema | ❌ Open | Not yet set up |

---

## 3. Real-Time / WebSocket

| Item | Status | Location |
|---|---|---|
| WebSocket endpoints undocumented | ✅ Closed | `docs/websockets.md` |
| Message types (client → server) | ✅ Closed | `docs/websockets.md` |
| Message types (server → client) | ✅ Closed | `docs/websockets.md` |
| Close codes (4001 unauthenticated, 4003 forbidden) | ✅ Closed | `docs/websockets.md` |
| How automatic widget refresh is triggered | ✅ Closed | `docs/websockets.md` |

---

## 4. Deployment

| Item | Status | Location |
|---|---|---|
| Docker dev setup | ✅ Closed | `README.md` — Docker Setup section |
| Production Dockerfile | ✅ Closed | `Dockerfile.prod` — multi-stage, non-root user |
| docker-compose.prod.yml | ✅ Closed | `docker-compose.prod.yml` — 6 services with health checks |
| entrypoint.sh | ✅ Closed | `entrypoint.sh` — waits for Postgres, migrates, collects static |
| Nginx config | ✅ Closed | `docker/nginx/nginx.conf` — rate limiting, security headers, WebSocket proxy |
| Render.com deployment | ✅ Closed | `docs/render-deployment.md` + `render.yaml` |
| Celery & Redis production config | ✅ Closed | `config/settings/production.py` — Upstash TLS, channel layers |
| Production environment variables for Render | ✅ Closed | `docs/render-deployment.md` — table with every var and where to find it |

---

## 5. Widget & Dashboard Configuration

| Item | Status | Location |
|---|---|---|
| `query_config` schema (aggregations, filters, limit) | ✅ Closed | `docs/widget-configuration.md` |
| `viz_config` schema per widget type | ✅ Closed | `docs/widget-configuration.md` |
| `created_at_date` virtual group-by field | ✅ Closed | `docs/widget-configuration.md` |
| Auto-generated dashboard logic | ⚠️ Partial | Covered at high level in `README.md` architecture section |

---

## 6. Development Guide

| Item | Status | Location |
|---|---|---|
| Debug toolbar setup | ✅ Closed | Added to `development.py` + `urls.py`; access at `/__debug__/` |
| Testing strategy and how to add tests | ✅ Closed | `docs/testing.md` — pytest setup, factories, view/API/Celery patterns, coverage |
| Frontend architecture (Alpine.js + HTMX patterns) | ✅ Closed | `docs/frontend-patterns.md` — Alpine components, state machines, CSRF, polling, Tailwind |
| Custom template tags (`dashboard_filters`) | ✅ Closed | `apps/dashboards/templatetags/dashboard_filters.py` — module docstring + full `get_item` docstring |

---

## 7. Newsletter System

| Item | Status | Location |
|---|---|---|
| Newsletter subscription flow | ⚠️ Partial | `NEWSLETTER.md` exists — review for completeness |
| Campaign creation and sending | ⚠️ Partial | `NEWSLETTER.md` |
| Webhook event processing (bounce/complaint handling) | ❌ Open | Not documented |
| A/B test campaign setup | ❌ Open | Not documented |

---

## Remaining open items (priority order)

1. **OpenAPI schema** — Add `drf-spectacular` to generate `/api/v1/schema/` and a Swagger UI at `/api/v1/docs/`
2. **Newsletter webhook processing** — Document how bounce/complaint webhooks map to `WebhookEvent` → `process_webhook_event` Celery task
3. **A/B test campaign setup** — Document how to create and track A/B newsletter campaigns
