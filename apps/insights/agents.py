"""
apps/insights/agents.py
=======================
VisualizationAdvisor: uses Claude tool-use to ADVISE which columns to focus
on, which chart type best reveals the insight, and what human-readable titles
and axis labels to use.

The advisor never creates widgets itself — it returns a structured plan that
the engine (DataTable.build_widgets_from_plan) uses to create Widget records.
No real data reaches the LLM; only the privacy-safe shadow dataset produced
by PrivacySampler is sent.
"""

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# The advisor submits a single plan object covering the whole dashboard.
_PLAN_TOOL = {
    "name": "submit_visualization_plan",
    "description": (
        "Submit the complete visualization plan for this dataset. "
        "Call this ONCE with all recommendations — the chart engine will "
        "use your plan to build the actual widgets."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kpi_fields": {
                "type": "array",
                "description": "Fields to show as KPI metric cards at the top. Max 3.",
                "items": {
                    "type": "object",
                    "properties": {
                        "field":       {"type": "string", "description": "Exact field name from schema. Omit for a plain record count."},
                        "aggregation": {"type": "string", "enum": ["sum", "avg", "count", "min", "max"]},
                        "title":       {"type": "string", "description": "Card title, e.g. 'Total Revenue'"},
                        "prefix":      {"type": "string", "description": "Value prefix shown before the number, e.g. 'KES ' or '$'. Leave blank if none."},
                        "color":       {"type": "string", "enum": ["blue", "green", "orange", "purple", "teal", "red"]}
                    },
                    "required": ["aggregation", "title", "color"]
                }
            },
            "chart_plans": {
                "type": "array",
                "description": "Charts to place on the dashboard body. Include as many insightful charts as the data supports, typically 4–10. Cover different angles: trends, breakdowns, distributions, and correlations.",
                "items": {
                    "type": "object",
                    "properties": {
                        "chart_type": {
                            "type": "string",
                            "enum": [
                                "line_chart", "area_chart", "bar_chart",
                                "pie_chart", "donut", "scatter", "histogram",
                                "treemap", "funnel_chart", "waterfall", "box_plot", "bubble",
                            ],
                            "description": "Chart type that best reveals the insight"
                        },
                        "x_field":     {"type": "string", "description": "Exact field name for the X axis / grouping dimension"},
                        "y_field":     {"type": "string", "description": "Exact field name for the Y axis / value. Omit when aggregation is count with no specific numeric field."},
                        "aggregation": {"type": "string", "enum": ["sum", "avg", "count", "min", "max"]},
                        "title":       {"type": "string", "description": "Chart title in plain business English, e.g. 'Revenue by Product'"},
                        "x_label":     {"type": "string", "description": "Human-readable X axis label, e.g. 'Sale Date', 'Product Category'"},
                        "y_label":     {"type": "string", "description": "Human-readable Y axis label with units, e.g. 'Total Revenue (KES)', 'Number of Orders'"},
                        "color":       {"type": "string", "enum": ["blue", "green", "orange", "purple", "teal", "red"]}
                    },
                    "required": ["chart_type", "aggregation", "title", "x_label", "y_label", "color"]
                }
            },
            "skip_fields": {
                "type": "array",
                "description": "Field names to exclude from visualizations (IDs, emails, phone numbers, UUIDs, raw text blobs).",
                "items": {"type": "string"}
            }
        },
        "required": ["kpi_fields", "chart_plans"]
    }
}

_SYSTEM_PROMPT = """You are a data visualization advisor for a business intelligence platform used by East African SMEs.

You receive a table schema (field names + types) and up to 50 anonymised sample records.
Your ONLY job is to call submit_visualization_plan ONCE with a plan that tells the chart engine:
  • which fields are most analytically valuable as KPI cards
  • which chart type best reveals each insight
  • clear, business-readable titles and axis labels

You are an ADVISOR — you do not create charts yourself. The engine reads your plan and builds the actual widgets.

Field → chart type rules:
- date/datetime + numeric  → line_chart (trend over time) or area_chart (volume emphasis)
- category text + numeric  → bar_chart (many groups) or pie_chart / donut (≤ 7 groups)
- single headline number   → KPI field (metric card), not a chart
- numeric distribution     → histogram or box_plot
- two numeric fields       → scatter (correlation)
- ordered ranked items     → funnel_chart
- proportional categories  → treemap (better than pie when > 7 groups)
- cumulative changes       → waterfall

KPI card rules:
- Put the most important single number first (e.g. Total Revenue, Total Orders)
- Max 3 KPI cards
- Always include a record-count KPI (aggregation: count, no field) unless another count KPI is more meaningful

Axis label rules — critical for business readability:
- x_label: plain English describing what the x_field represents ("Sale Month", "Product Category")
- y_label: metric name + unit ("Total Revenue (KES)", "Number of Orders", "Avg Score (%)")
- For count: y_label = "Number of [things]" where [things] reflects the record type

Variety: use at least 4 different chart_type values across all chart_plans.
Depth: include as many charts as meaningfully reveal different insights — aim for 6–10 when the dataset has enough dimensions. Do not repeat the same chart type and field combination.
Order chart_plans: trends first, then breakdowns, then distributions, then correlations.
Skip PII fields (email, phone, national ID, password) and raw identifier fields (UUID, internal ID).

Call submit_visualization_plan exactly once."""


class VisualizationAdvisor:
    """
    Calls Claude with a single tool to get a visualization plan.
    The plan is returned to the caller; the engine creates widgets from it.

    Usage::

        advisor = VisualizationAdvisor()
        plan, tokens = advisor.analyze(shadow_dataset)
        # plan = {"kpi_fields": [...], "chart_plans": [...], "skip_fields": [...]}
    """

    def __init__(self):
        self._api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
        self._model   = getattr(settings, 'CLAUDE_INSIGHT_MODEL', 'claude-sonnet-4-6')

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def analyze(self, shadow_dataset: dict) -> tuple[dict, dict]:
        """
        Run the advisor against *shadow_dataset* and return:
            (plan, {"input": N, "output": N})

        plan = {"kpi_fields": [...], "chart_plans": [...], "skip_fields": [...]}
        Returns ({}, {}) on any error.
        """
        if not self.is_available():
            logger.warning('VisualizationAdvisor: unavailable — no API key or SDK')
            return {}, {}

        import anthropic
        client = anthropic.Anthropic(api_key=self._api_key)
        user_msg = self._build_user_message(shadow_dataset)
        tokens = {'input': 0, 'output': 0}

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=3000,
                system=_SYSTEM_PROMPT,
                tools=[_PLAN_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": user_msg}],
            )
            tokens['input']  = response.usage.input_tokens
            tokens['output'] = response.usage.output_tokens

            for block in response.content:
                if block.type == 'tool_use' and block.name == 'submit_visualization_plan':
                    plan = self._validate_plan(block.input)
                    logger.info(
                        'VisualizationAdvisor: plan has %d KPIs, %d charts tokens_in=%d tokens_out=%d',
                        len(plan.get('kpi_fields', [])),
                        len(plan.get('chart_plans', [])),
                        tokens['input'], tokens['output'],
                    )
                    return plan, tokens

        except Exception:
            logger.exception('VisualizationAdvisor: Claude API call failed')
            return {}, {}

        logger.warning('VisualizationAdvisor: no plan tool call in response')
        return {}, {}

    # ── Private helpers ────────────────────────────────────────────────────

    def _build_user_message(self, dataset: dict) -> str:
        schema       = dataset.get('schema', [])
        records      = dataset.get('records', [])
        table_name   = dataset.get('table_name', 'data')
        record_count = dataset.get('record_count', len(records))

        schema_lines = '\n'.join(
            f'  - {f["name"]} (type: {f["type"]}{"  ← required" if f.get("required") else ""})'
            for f in schema
        ) or '  (no schema defined)'

        sample_json = json.dumps(records[:10], indent=2)

        return (
            f"Table name: {table_name}\n"
            f"Total records: {record_count}\n\n"
            f"Schema:\n{schema_lines}\n\n"
            f"Sample records (values are anonymised — field names and types are real):\n"
            f"{sample_json}\n\n"
            f"Please call submit_visualization_plan once with the best visualization plan "
            f"for a business owner using this data."
        )

    def _validate_plan(self, raw: dict) -> dict:
        """Return a clean plan dict, dropping malformed entries."""
        valid_chart_types = {
            'line_chart', 'area_chart', 'bar_chart', 'pie_chart', 'donut',
            'scatter', 'histogram', 'treemap', 'funnel_chart', 'waterfall',
            'box_plot', 'bubble',
        }
        valid_aggs   = {'sum', 'avg', 'count', 'min', 'max'}
        valid_colors = {'blue', 'green', 'orange', 'purple', 'teal', 'red'}

        kpi_fields = []
        for k in raw.get('kpi_fields', [])[:3]:
            if not isinstance(k, dict):
                continue
            if k.get('aggregation') not in valid_aggs:
                continue
            kpi_fields.append({
                'field':       k.get('field', ''),
                'aggregation': k['aggregation'],
                'title':       str(k.get('title', 'KPI'))[:80],
                'prefix':      str(k.get('prefix', '')),
                'color':       k.get('color', 'blue') if k.get('color') in valid_colors else 'blue',
            })

        chart_plans = []
        for c in raw.get('chart_plans', [])[:10]:
            if not isinstance(c, dict):
                continue
            if c.get('chart_type') not in valid_chart_types:
                continue
            if c.get('aggregation') not in valid_aggs:
                continue
            chart_plans.append({
                'chart_type':  c['chart_type'],
                'x_field':     str(c.get('x_field', '')),
                'y_field':     str(c.get('y_field', '')),
                'aggregation': c['aggregation'],
                'title':       str(c.get('title', 'Chart'))[:100],
                'x_label':     str(c.get('x_label', c.get('x_field', 'Category'))),
                'y_label':     str(c.get('y_label', 'Value')),
                'color':       c.get('color', 'blue') if c.get('color') in valid_colors else 'blue',
            })

        return {
            'kpi_fields':  kpi_fields,
            'chart_plans': chart_plans,
            'skip_fields': [str(f) for f in raw.get('skip_fields', []) if isinstance(f, str)],
        }
