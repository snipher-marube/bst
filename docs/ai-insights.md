# AI Insights — LLM Integration

AnalyticsMeta uses Anthropic's **Claude** (`claude-sonnet-4-6`) to generate plain-English narratives that explain what a workspace's data actually means — trend explanations, anomaly descriptions, summaries, and predictions — alongside the underlying statistical analysis.

---

## How It Works

```
1. Celery task `analyze_workspace_tables()` runs on a schedule (or on-demand).
2. Statistical analysis (DB-side aggregation) produces a stats_context dict.
3. ClaudeInsightGenerator.narrate_insight() sends stats_context to Claude.
4. The returned narrative is stored on the Insight model alongside the heuristic description.
5. Django Channels pushes the new insight to all open dashboard WebSocket connections.
```

The LLM call is **additive** — if `ANTHROPIC_API_KEY` is not set or the workspace has exhausted its monthly token budget, the task falls back gracefully to the statistical description. No errors surface to the end user.

---

## Key Files

| File | Purpose |
|---|---|
| `apps/insights/llm.py` | `ClaudeInsightGenerator` — Anthropic SDK wrapper, prompt templates, `LLMError` |
| `apps/insights/tasks.py` | Celery tasks — statistical analysis + LLM narrative generation |
| `apps/insights/models.py` | `Insight` model with `llm_model`, `prompt_tokens`, `completion_tokens` fields |
| `apps/insights/tests.py` | 455-line test suite covering LLM path, fallback, and budget enforcement |

---

## Configuration

Add the following to your `.env` file:

```env
# Required — leave empty to disable LLM and use statistical-only mode
ANTHROPIC_API_KEY=sk-ant-...

# Optional — per-workspace monthly token cap (default: 100,000)
LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET=100000
```

These map to `settings.ANTHROPIC_API_KEY` and `settings.LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET` in `config/settings/base.py`.

---

## ClaudeInsightGenerator

```python
from apps.insights.llm import ClaudeInsightGenerator

gen = ClaudeInsightGenerator()

# Check if the API key is configured
if gen.is_available():
    result = gen.narrate_insight(insight_type="trend", stats_context={
        "table_name": "Sales",
        "column": "amount",
        "period": "last 30 days",
        "change_pct": 23.4,
        "current_value": 142000,
        "prior_value": 115000,
    })
    # result = {
    #     "narrative": "Sales grew 23% over the last 30 days...",
    #     "model": "claude-sonnet-4-6",
    #     "prompt_tokens": 312,
    #     "completion_tokens": 87,
    # }
```

### `narrate_insight(insight_type, stats_context)`

| Parameter | Type | Description |
|---|---|---|
| `insight_type` | `str` | One of `trend`, `anomaly`, `summary`, `prediction`, `comparison` |
| `stats_context` | `dict` | Raw numbers from statistical analysis — passed verbatim into the prompt |

Returns a `dict` with keys `narrative`, `model`, `prompt_tokens`, `completion_tokens`.

Raises `LLMError` on API failure — callers should catch this and fall back to the heuristic description.

---

## Token Budget Enforcement

`WorkspaceLLMBudget` tracks cumulative token usage per workspace per calendar month using the `prompt_tokens` and `completion_tokens` fields on the `Insight` model.

```python
from apps.insights.llm import WorkspaceLLMBudget

budget = WorkspaceLLMBudget(workspace)
if budget.has_capacity(estimated_tokens=500):
    # safe to call Claude
    ...
```

- Budget resets automatically on the 1st of each month.
- Set `LLM_WORKSPACE_MONTHLY_TOKEN_BUDGET=0` to disable LLM for all workspaces.
- Individual workspace overrides are not yet implemented — all workspaces share the same configured cap.

---

## Prompt Design

The system prompt instructs Claude to act as a concise BI analyst:

- Narratives are **1–3 sentences maximum**.
- Claude must reference the specific numbers from `stats_context` — no hallucinated figures.
- Output must be plain English suitable for a non-technical business owner.
- No markdown, bullet points, or headers in the response.

The prompt templates live in `ClaudeInsightGenerator` inside `apps/insights/llm.py`. Edit them there if you need to adjust tone or output format.

---

## Insight Types

| Type | What Claude explains |
|---|---|
| `trend` | Direction and magnitude of change over the selected period |
| `anomaly` | Why a data point is unusual relative to the baseline |
| `summary` | High-level overview of a table's key metrics |
| `prediction` | Forward-looking estimate based on recent trajectory |
| `comparison` | How two periods or segments differ |

---

## Disabling LLM

To run in statistical-only mode:

```env
ANTHROPIC_API_KEY=
```

The task will generate heuristic descriptions as before. The `llm_model`, `prompt_tokens`, and `completion_tokens` fields on `Insight` will remain at their defaults (`""`, `0`, `0`).

---

## Testing

```bash
# Run the full insights test suite
python manage.py test apps.insights

# Run only LLM-related tests
python manage.py test apps.insights.tests.InsightLLMTestCase
```

Tests mock the Anthropic client — no real API calls are made. The test suite covers:

- Happy path: LLM narrative stored on Insight
- Fallback path: `ANTHROPIC_API_KEY` empty → statistical description used
- Budget enforcement: workspace over cap → LLM skipped
- `LLMError` raised → task catches and stores heuristic description instead
