# Data Alerts

Data Alerts let workspace members define threshold conditions on any numeric field in a DataTable. When the condition is met, all workspace members receive an in-app notification. Alerts include a configurable cooldown so the same condition cannot fire repeatedly within a short window.

---

## How It Works

```
Every 15 minutes (Celery beat):
  1. check_data_alerts task fetches all active DataAlert records.
  2. For each alert, it aggregates the target field from the DataTable's records.
  3. It evaluates:  aggregate_value <operator> threshold
  4. If true AND the alert is not in its cooldown window:
       → Create a Notification for every workspace member
       → Update last_triggered and last_value on the alert
```

Alerts are evaluated against **live data** — they re-run the aggregate query on the full `Record` table each cycle. No snapshots.

---

## Key Files

| File | Purpose |
|---|---|
| `apps/dashboards/models.py` | `DataAlert` model — fields, `evaluate()`, `is_in_cooldown()` |
| `apps/dashboards/tasks.py` | `check_data_alerts` Celery task + `_compute_aggregate()` helper |
| `apps/dashboards/serializers.py` | `DataAlertSerializer` |
| `apps/dashboards/api_v1.py` | `DataAlertListCreateAPIView`, `DataAlertDetailAPIView` |
| `apps/dashboards/urls_api_v1.py` | URL patterns |
| `config/settings/base.py` | Celery beat schedule (`check-data-alerts` every 15 min) |

---

## DataAlert Model

```python
class DataAlert(models.Model):
    workspace    # ForeignKey(Workspace)
    table        # ForeignKey(DataTable) — which table to query
    created_by   # ForeignKey(User)

    name         # CharField — human-readable label
    field_name   # CharField — column in the DataTable to aggregate
    aggregate    # sum | avg | count | min | max
    operator     # gt | gte | lt | lte | eq
    threshold    # FloatField — the value to compare against

    is_active    # BooleanField — disable without deleting
    cooldown_minutes  # PositiveIntegerField (default: 60)

    # Populated by the check task
    last_triggered    # DateTimeField (null)
    last_value        # FloatField (null)
```

### Supported aggregates

| Value | Meaning |
|---|---|
| `sum` | Sum of all non-null values in `field_name` |
| `avg` | Mean of all non-null values |
| `count` | Number of records where `field_name` is present (non-null) |
| `min` | Minimum value |
| `max` | Maximum value |

### Supported operators

| Value | Condition fires when… |
|---|---|
| `gt` | `value > threshold` |
| `gte` | `value >= threshold` |
| `lt` | `value < threshold` |
| `lte` | `value <= threshold` |
| `eq` | `abs(value - threshold) < 1e-9` |

---

## REST API

All endpoints require authentication (`Authorization: Token <token>`) and the active workspace must be identifiable via `X-Workspace-ID` header.

### List alerts in a workspace

```http
GET /api/v1/workspaces/<workspace_id>/alerts/
```

**Response 200:**
```json
[
    {
        "id": "uuid",
        "workspace": "uuid",
        "table": "uuid",
        "name": "Revenue Alert",
        "field_name": "amount",
        "aggregate": "sum",
        "operator": "gt",
        "threshold": 50000.0,
        "is_active": true,
        "cooldown_minutes": 60,
        "last_triggered": null,
        "last_value": null,
        "created_at": "2026-04-12T10:00:00Z",
        "updated_at": "2026-04-12T10:00:00Z"
    }
]
```

### Create an alert

```http
POST /api/v1/workspaces/<workspace_id>/alerts/
Content-Type: application/json

{
    "table": "<table_uuid>",
    "name": "Low Inventory Warning",
    "field_name": "stock_qty",
    "aggregate": "min",
    "operator": "lt",
    "threshold": 10.0,
    "cooldown_minutes": 120
}
```

**Response 201:** the created alert object.

The `table` must belong to the same workspace — a cross-workspace table UUID returns HTTP 404.

### Get / update / delete a single alert

```http
GET    /api/v1/alerts/<alert_id>/
PATCH  /api/v1/alerts/<alert_id>/   # partial update
DELETE /api/v1/alerts/<alert_id>/
```

**PATCH example** — disable an alert:
```json
{ "is_active": false }
```

---

## Cooldown

Once an alert fires, it will not fire again until `cooldown_minutes` have elapsed since `last_triggered`. This prevents notification storms when a condition remains true across multiple evaluation cycles.

To force an immediate re-evaluation after manual data correction, reset the `last_triggered` field:

```python
DataAlert.objects.filter(pk=alert_id).update(last_triggered=None)
```

---

## Notification content

When an alert fires, each workspace member receives a `Notification` with:

- **Title:** `Data Alert: <alert.name>`
- **Message:** `Alert '<name>': <aggregate> of '<field>' is <value> (<operator> <threshold>)`
- **Type:** `warning`
- **Action URL:** `/dashboard/alerts/`
- **Metadata:** `alert_id`, `current_value`, `threshold`

---

## Celery beat schedule

The `check_data_alerts` task is registered in `CELERY_BEAT_SCHEDULE` in `config/settings/base.py`:

```python
'check-data-alerts': {
    'task':     'dashboards.check_data_alerts',
    'schedule': crontab(minute='*/15'),
},
```

To change the interval, edit the crontab expression. To disable the task entirely, set `is_active = False` on individual alerts rather than removing the schedule.

---

## Testing

```bash
python manage.py test apps.dashboards.tests.TestDataAlertModel \
                      apps.dashboards.tests.TestDataAlertAPI \
                      apps.dashboards.tests.TestCheckDataAlertsTask \
                      --settings=config.settings.test
```

Tests cover:
- `evaluate()` for all five operators
- `is_in_cooldown()` — no trigger, in window, past window
- CRUD API: create, list, get, patch, delete
- Cross-workspace table rejection
- Celery task: fires when condition met, skips when not, respects cooldown
