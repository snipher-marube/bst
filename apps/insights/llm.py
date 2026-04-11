"""
apps/insights/llm.py
====================
LLM-powered narrative generation for AnalyticsMeta insights.

Usage
-----
::

    from apps.insights.llm import ClaudeInsightGenerator

    gen = ClaudeInsightGenerator()
    if gen.is_available():
        result = gen.narrate_insight(insight_type, stats_context)
        # result = {'narrative': str, 'model': str, 'prompt_tokens': int, 'completion_tokens': int}

Architecture
------------
* Wraps the ``anthropic`` SDK (optional dependency — gracefully disabled when
  the package is absent or ``ANTHROPIC_API_KEY`` is empty).
* Accepts a plain-dict ``stats_context`` produced by the statistical analysis
  in ``tasks.py``.  This keeps the prompt construction separate from DB logic.
* Budget enforcement is the caller's responsibility (see
  ``WorkspaceLLMBudget.has_capacity()``).

Prompt design
-------------
The system prompt instructs Claude to act as a concise BI analyst — 1-3
sentence narratives that reference the specific numbers from ``stats_context``
without hallucinating extra facts.

Error handling
--------------
All API errors bubble up as ``LLMError`` so callers can decide whether to
fall back to the heuristic description or surface an error to the user.
"""

import logging
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when a Claude API call fails."""


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class ClaudeInsightGenerator:
    """Thin wrapper around the Anthropic Messages API for insight narratives."""

    SYSTEM_PROMPT = (
        "You are a concise business intelligence analyst. "
        "Given structured statistics about a data table, write a 1-3 sentence plain-English "
        "narrative suitable for a business dashboard. "
        "Be specific — reference the exact numbers provided. "
        "Do NOT invent facts not present in the statistics. "
        "Do NOT use markdown, bullet points, or headings. "
        "Output only the narrative text."
    )

    def __init__(self):
        self._client = None
        self._model  = getattr(settings, 'CLAUDE_INSIGHT_MODEL', 'claude-sonnet-4-6')
        self._api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True only when the SDK is installed and an API key is configured."""
        if not self._api_key:
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Lazy client initialisation
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMError(
                    "anthropic package is not installed. "
                    "Run: pip install anthropic"
                ) from exc
            if not self._api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set. "
                    "Configure it in your .env file."
                )
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def narrate_insight(
        self,
        insight_type: str,
        stats_context: dict,
        max_tokens: int = 256,
    ) -> dict:
        """
        Generate a narrative for one insight.

        Parameters
        ----------
        insight_type : str
            One of: 'summary', 'trend', 'anomaly', 'comparison'
        stats_context : dict
            Plain-dict statistics (produced by ``tasks._analyse_table``).
            Example for a trend insight::

                {
                    'table': 'Orders',
                    'field': 'revenue',
                    'direction': 'upward',
                    'pct_change': 14.3,
                    'first_avg': 4200.0,
                    'last_avg': 4801.0,
                    'record_count': 800,
                }

        max_tokens : int
            Upper bound on completion tokens.  Keep low — narratives are short.

        Returns
        -------
        dict with keys: ``narrative``, ``model``, ``prompt_tokens``, ``completion_tokens``

        Raises
        ------
        LLMError
            On any API or configuration failure.
        """
        client = self._get_client()
        user_message = self._build_user_message(insight_type, stats_context)

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            raise LLMError(f"Claude API call failed: {exc}") from exc

        narrative = response.content[0].text.strip() if response.content else ""
        return {
            "narrative":         narrative,
            "model":             response.model,
            "prompt_tokens":     response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
        }

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_user_message(self, insight_type: str, ctx: dict) -> str:
        """Convert structured stats into a descriptive user message for Claude."""
        if insight_type == 'summary':
            return (
                f"Table: {ctx.get('table', 'Unknown')}\n"
                f"Total records: {ctx.get('record_count', 'N/A')}\n"
                f"Total fields: {ctx.get('field_count', 'N/A')}\n"
                f"Numeric fields: {', '.join(ctx.get('numeric_fields', [])) or 'none'}\n"
                f"Date fields: {', '.join(ctx.get('date_fields', [])) or 'none'}\n"
                f"\nWrite a concise summary insight for this table."
            )
        elif insight_type == 'trend':
            return (
                f"Table: {ctx.get('table', 'Unknown')}\n"
                f"Field: {ctx.get('field', 'Unknown')}\n"
                f"Trend direction: {ctx.get('direction', 'unknown')}\n"
                f"Average at start of period: {ctx.get('first_avg', 'N/A')}\n"
                f"Average at end of period: {ctx.get('last_avg', 'N/A')}\n"
                f"Percentage change: {ctx.get('pct_change', 'N/A')}%\n"
                f"Records analysed: {ctx.get('record_count', 'N/A')}\n"
                f"\nWrite a concise trend insight for this field."
            )
        elif insight_type == 'anomaly':
            return (
                f"Table: {ctx.get('table', 'Unknown')}\n"
                f"Field: {ctx.get('field', 'Unknown')}\n"
                f"Mean value: {ctx.get('mean', 'N/A')}\n"
                f"Standard deviation: {ctx.get('std', 'N/A')}\n"
                f"Outlier records (|z-score| ≥ 3): {ctx.get('outlier_count', 'N/A')}\n"
                f"Outlier percentage: {ctx.get('pct_outliers', 'N/A')}%\n"
                f"\nWrite a concise anomaly insight flagging the outliers."
            )
        elif insight_type == 'comparison':
            table_avgs = ctx.get('table_averages', {})
            rows = '\n'.join(f"  {t}: avg={v}" for t, v in table_avgs.items())
            return (
                f"Shared numeric field: {ctx.get('field', 'Unknown')}\n"
                f"Average values per table:\n{rows}\n"
                f"\nWrite a concise comparison insight across these tables."
            )
        else:
            return (
                f"Insight type: {insight_type}\n"
                f"Statistics: {ctx}\n"
                f"\nWrite a concise insight narrative based on these statistics."
            )
