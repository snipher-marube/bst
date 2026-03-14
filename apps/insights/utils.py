# apps/insights/utils.py
import json
from uuid import UUID
from datetime import datetime, date
from decimal import Decimal

class InsightJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles UUID, datetime, date, and Decimal objects
    """
    def default(self, obj):
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, 'isoformat'):  # Handle other datetime-like objects
            return obj.isoformat()
        # Let the base class default method raise the TypeError
        return super().default(obj)