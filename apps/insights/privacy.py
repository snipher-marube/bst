"""
apps/insights/privacy.py
========================
PrivacySampler: produces a privacy-safe shadow dataset from a DataTable.

Field values are masked by type so that no real PII or exact business
figures reach the LLM. The shadow dataset is statistically representative
(preserves order-of-magnitude, time patterns, cardinality) but contains
no reconstructable sensitive values.
"""

import hashlib
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_SIZE = 50


class PrivacySampler:
    """
    Pull up to `sample_size` records from a DataTable and mask each field.

    Usage::

        sampler = PrivacySampler(table)
        shadow = sampler.sample()
        # shadow = {"schema": [...], "records": [...], "record_count": N, "table_name": "..."}
    """

    def __init__(self, table, sample_size: int = SAMPLE_SIZE):
        self.table = table
        self.sample_size = sample_size
        self._text_pools: dict[str, list[str]] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    def sample(self) -> dict:
        """Return a privacy-safe representation of the table."""
        from apps.dashboards.models import Record

        records = list(
            Record.objects.filter(table=self.table, is_active=True)
            .values_list('data', flat=True)
            .order_by('?')[:self.sample_size]
        )

        schema = self.table.schema or []
        field_map = {f['name']: f['type'] for f in schema}
        masked = [self._mask_record(r, field_map) for r in records if r]

        return {
            'schema': schema,
            'records': masked,
            'record_count': self.table.record_count,
            'table_name': self.table.name,
        }

    # ── Masking logic ──────────────────────────────────────────────────────

    def _mask_record(self, data: dict, field_map: dict) -> dict:
        return {
            key: self._mask_value(key, value, field_map.get(key, 'text'))
            for key, value in data.items()
        }

    def _mask_value(self, field: str, value: Any, field_type: str) -> Any:
        if value is None or value == '':
            return value

        if field_type == 'email':
            h = hashlib.sha256(str(value).encode()).hexdigest()[:6]
            return f'user_{h}@example.com'

        if field_type == 'phone':
            suffix = random.randint(10_000_000, 99_999_999)
            return f'+2547{suffix}'

        if field_type in ('currency', 'number', 'percentage'):
            try:
                v = float(value)
            except (TypeError, ValueError):
                return 0
            if v == 0:
                return 0
            magnitude = max(1, 10 ** (len(str(int(abs(v)))) - 1))
            return round(random.uniform(magnitude * 0.5, magnitude * 2.5), 2)

        if field_type in ('date', 'datetime'):
            return self._shift_date(value, field_type)

        if field_type == 'url':
            h = hashlib.sha256(str(value).encode()).hexdigest()[:8]
            return f'https://example.com/{h}'

        if field_type == 'boolean':
            return value

        if field_type == 'text':
            return random.choice(self._text_pool(field))

        return value

    def _shift_date(self, value: Any, field_type: str) -> Any:
        """Shift a date/datetime by a random ±45-day offset."""
        import datetime
        try:
            shift = datetime.timedelta(days=random.randint(-45, 45))
            if field_type == 'date':
                from django.utils.dateparse import parse_date
                parsed = parse_date(str(value))
                if parsed:
                    return (parsed + shift).isoformat()
            else:
                from django.utils.dateparse import parse_datetime
                parsed = parse_datetime(str(value))
                if parsed:
                    return (parsed + shift).isoformat()
        except Exception:
            pass
        return value

    def _text_pool(self, field: str) -> list[str]:
        """Return a stable 8-item pool of synthetic labels for a field."""
        if field not in self._text_pools:
            self._text_pools[field] = [
                f'{field.replace("_", " ").title()} {chr(65 + i)}' for i in range(8)
            ]
        return self._text_pools[field]
