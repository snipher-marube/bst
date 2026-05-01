# Agentic AI Dashboard Feature — Implementation Plan

## Overview

Add an agentic AI layer to the dashboard that:
1. Reads a table's schema and samples up to 50 records
2. Masks or synthesizes sensitive data before sending anything to Claude
3. Uses Claude (tool-use agent) to pick the best chart types and **auto-create widgets**
4. Provides a **session-scoped Ask AI chat panel** for natural language Q&A about the data

---

## Architecture

```
DataTable (schema + records)
        │
        ▼
┌──────────────────────┐
│   Privacy Layer      │  ← masks/synthesizes field values by type
│   (PrivacySampler)   │    never sends raw PII or exact figures to Claude
└──────────┬───────────┘
           │  shadow dataset (schema + ≤50 anonymised records)
           ▼
┌──────────────────────┐       ┌──────────────────────┐
│  Visualization Agent │       │   Ask AI Panel        │
│  (Celery + Claude    │       │   (session-scoped     │
│   tool-use)          │       │    chat, WebSocket    │
│                      │       │    streamed)          │
│  Outputs: Widget     │       │                       │
│  configs →           │       │  Outputs: Insight     │
│  auto-created on     │       │  records + Redis      │
│  dashboard           │       │  session buffer       │
└──────────────────────┘       └──────────────────────┘
```

---

## Phase 1 — Privacy Layer (`PrivacySampler`)

**Goal:** Produce a shadow dataset that is statistically representative but contains no real PII or exact business figures.

### Field-type masking rules

| Field type   | Masking strategy |
|---|---|
| `email`      | Replace with `user_<hash[:6]>@example.com` |
| `phone`      | Random valid-format number (`+2547XXXXXXXX`) |
| `text`       | Replace with a label drawn from a small per-column pool (`Item A`, `Item B`, …) |
| `currency`   | Random value scaled within real [min, max] range (preserves order of magnitude) |
| `number`     | Same as currency — random within real range |
| `percentage` | Pass through as-is (no PII risk) |
| `boolean`    | Pass through as-is |
| `date`       | Shift each value by a random ±N days (preserves time patterns) |
| `datetime`   | Same as date, shift by random ±N hours |
| `url`        | Replace with `https://example.com/<slug>` |

### Implementation

```
apps/insights/privacy.py
```

```python
class PrivacySampler:
    def __init__(self, table: DataTable, sample_size: int = 50):
        ...

    def sample(self) -> dict:
        """Returns {"schema": [...], "records": [...]} — safe to send to Claude."""
        records = self._fetch_records()
        return {
            "schema": self.table.schema,
            "records": [self._mask_record(r) for r in records],
        }

    def _mask_record(self, record: dict) -> dict:
        ...
```

**No new models needed.** Pure Python, fully unit-testable with no DB.

---

## Phase 2 — Visualization Agent

**Goal:** Claude receives the shadow dataset and uses tool calls to declare the best widgets. The agent task auto-creates those widgets on the dashboard.

### Claude tool definitions (sent in the system prompt)

```python
tools = [
    {
        "name": "create_widget",
        "description": "Create a chart or metric widget on the dashboard.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "widget_type": {"type": "string", "enum": [
                    "line_chart", "bar_chart", "pie_chart", "scatter_chart",
                    "metric", "table", "gauge", "funnel"
                ]},
                "x_field":     {"type": "string"},
                "y_field":     {"type": "string"},
                "aggregation": {"type": "string", "enum": ["sum","avg","count","min","max"]},
                "color":       {"type": "string"},
                "rationale":   {"type": "string"},
                "position":    {
                    "type": "object",
                    "properties": {"x":{"type":"integer"},"y":{"type":"integer"},
                                   "w":{"type":"integer"},"h":{"type":"integer"}}
                }
            },
            "required": ["title","widget_type","y_field","aggregation","rationale"]
        }
    }
]
```

### Celery task

```
apps/insights/tasks.py  →  suggest_and_create_widgets(dashboard_id, table_id)
```

Flow:
1. `PrivacySampler(table).sample()` → shadow dataset
2. Build prompt: schema description + sample records + instructions
3. Call Claude with tool use (`max_tokens=2000`, `betas=[]`)
4. Parse tool calls → map each `create_widget` call to a `Widget` instance
5. Bulk-create widgets, auto-position using GridStack layout (pack left-to-right, 2 cols)
6. Decrement `WorkspaceLLMBudget`
7. Broadcast `widget_created` event via Django Channels → dashboard refreshes live

### API endpoint

```
POST /api/v1/dashboards/<id>/ai-suggest-widgets/
```

- Permission: workspace editor or above
- Response: `{"task_id": "...", "status": "queued"}`
- Frontend polls `/api/v1/tasks/<task_id>/status/` or listens on WebSocket

### UI trigger

Button on `dashboard_detail.html`:

```
[ ✨ Auto-generate charts ]
```

Shows a spinner while the Celery task runs, then newly created widgets appear on the GridStack canvas.

---

## Phase 3 — Ask AI Panel (Session-Scoped Chat)

### Design decision: session-scoped memory

| Approach | Verdict |
|---|---|
| Stateless (one question, no history) | Too frustrating — follow-up questions break |
| Full persistent threads (stored in DB) | Over-engineered for v1; stale context risk when data changes |
| **Session-scoped (recommended)** | Follow-ups work naturally; context auto-clears on session end; always fetches fresh data |

### How session memory works

- A `session_id` (UUID) is generated when the user opens the Ask AI panel and stored in the browser (`sessionStorage`)
- The last **6 conversation turns** are buffered in **Redis** with a 30-minute sliding TTL key: `ask_ai:<session_id>`
- Every new question fetches a **fresh shadow dataset** — answers are always based on current data
- The buffer (last 6 turns) is prepended to the Claude messages array for context
- When the session expires or the user closes the tab, Redis auto-clears the buffer

### Data model change

Add two fields to the existing `Insight` model:

```python
# apps/insights/models.py
session_id   = models.CharField(max_length=64, blank=True, db_index=True)
question     = models.TextField(blank=True)   # the user's raw question
insight_type = ...  # extend choices to include 'ask_ai'
```

Each Q&A exchange → one `Insight` record (stored permanently as a read-only log).

### Django Channels consumer

```
apps/insights/consumers.py  →  AskAIConsumer
```

WebSocket path: `ws/dashboard/<dashboard_id>/ask-ai/`

Flow per message:
1. Receive `{question, session_id}` from client
2. Load session buffer from Redis (`LRANGE ask_ai:<session_id> 0 5`)
3. `PrivacySampler(table).sample()` → fresh shadow dataset
4. Build messages array: system prompt + buffer + new user message
5. Stream Claude response (`stream=True`) → push chunks via WebSocket as `{type:"chunk", text:"..."}`
6. On stream end: save full response as `Insight`; push turn into Redis buffer (`LPUSH` + `LTRIM`)
7. Send `{type:"done", insight_id: ...}`

### UI — Ask AI panel

Location: collapsible right sidebar on `dashboard_detail.html`

```
┌─────────────────────────────────────┐
│  Ask AI                         [×] │
├─────────────────────────────────────┤
│  Previous insights (read-only log)  │
│  ─────────────────────────────────  │
│  Q: Why did sales drop in March?    │
│  A: Based on the data, March saw…   │
│                                     │
│  Q: Compare to February?            │
│  A: February recorded…              │
│                                     │
├─────────────────────────────────────┤
│  [Ask about your data...        ]   │
│                            [Send]   │
└─────────────────────────────────────┘
```

- Streamed responses render word-by-word (no waiting for full answer)
- History log loaded from `Insight` records (read-only, does not feed back to Claude)
- Active session buffer (last 6 turns) lives only in Redis

---

## File Changes Summary

### New files

| File | Purpose |
|---|---|
| `apps/insights/privacy.py` | `PrivacySampler` class — field-type masker |
| `apps/insights/agents.py` | Visualization agent — Claude tool-use orchestration |
| `apps/insights/consumers.py` (extend) | `AskAIConsumer` WebSocket handler |
| `apps/insights/tests/test_privacy.py` | Unit tests for masking logic |
| `apps/insights/tests/test_agents.py` | Agent task tests (mocked Claude) |

### Modified files

| File | Change |
|---|---|
| `apps/insights/models.py` | Add `session_id`, `question` fields to `Insight` |
| `apps/insights/tasks.py` | Add `suggest_and_create_widgets` Celery task |
| `apps/insights/urls.py` | Add `POST ai-suggest-widgets/` endpoint |
| `apps/insights/views.py` | Add view for the new endpoint |
| `config/routing.py` | Register `AskAIConsumer` WebSocket route |
| `templates/dashboard/dashboard_detail.html` | Add "Auto-generate charts" button + Ask AI sidebar |
| `apps/dashboards/migrations/` | Migration for new `Insight` fields |

---

## Build Order

```
Phase 1  PrivacySampler          — pure Python, no deps, fully testable first
Phase 2  Visualization agent     — Celery task + Claude tool use + Widget auto-creation
Phase 3  Ask AI panel            — WebSocket consumer + Redis session buffer + UI
```

Each phase is independently shippable. Phase 1 is a hard dependency for both Phase 2 and 3.

---

## Token Budget & Cost Controls

- Visualization agent: ~1,500–2,500 tokens per call (schema + 50 records + tool definitions)
- Ask AI: ~500–1,000 tokens per turn (question + buffer + shadow dataset)
- Both paths go through the existing `WorkspaceLLMBudget` guard — no extra work needed
- Add a per-dashboard daily call limit (e.g. 20 Ask AI calls/day on free tier) as a plan limit

---

## What Claude never sees

- Real email addresses or phone numbers
- Exact currency or revenue figures
- Real names in text fields
- Exact dates (shifted by random offset)

Claude only sees: field names, field types, anonymised/scaled values, and the user's question.

---

## Open Questions (decide before Phase 2)

1. Should the visualization agent suggest widgets for the **whole dashboard** (all tables) or **one table at a time**? Recommend: one table at a time — simpler scope, easier to explain to user.
2. Should auto-created widgets be **locked** (protected from accidental delete) or treated the same as manually created ones? Recommend: same as manual — user owns them after creation.
3. Max widgets per AI suggestion run? Recommend: cap at **6** to avoid overwhelming the dashboard canvas.
