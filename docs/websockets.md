# WebSocket API Reference

AnalyticsMeta uses **Django Channels** over Redis for real-time communication. Three WebSocket endpoints are available, each scoped to a different level of the data hierarchy.

---

## Connection

All WebSocket endpoints require an authenticated session. Unauthenticated connections are rejected with close code `4001`. Connections where the user has no access to the requested resource are rejected with `4003`.

```javascript
// Example connection (browser)
const socket = new WebSocket('wss://your-app.onrender.com/ws/dashboard/<dashboard_id>/');

socket.onopen = () => console.log('Connected');
socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    console.log(msg.type, msg);
};
socket.onclose = (event) => console.log('Closed', event.code);
```

---

## Endpoints

| Endpoint | Consumer | Purpose |
|---|---|---|
| `ws/dashboard/<dashboard_id>/` | `DashboardConsumer` | Real-time widget data, layout sync, collaboration |
| `ws/workspace/<workspace_id>/` | `WorkspaceConsumer` | Table change notifications across all dashboards |
| `ws/table/<table_id>/` | `TableConsumer` | Live record add/update/delete for a single table |

---

## Dashboard Consumer (`ws/dashboard/<dashboard_id>/`)

### On connect

The server immediately sends the full dashboard state:

```json
{
    "type": "dashboard_state",
    "data": {
        "id": "uuid",
        "name": "Sales Overview",
        "widgets": [
            {
                "id": "uuid",
                "widget_type": "line_chart",
                "title": "Revenue Over Time",
                "position": {"x": 0, "y": 0, "w": 6, "h": 4},
                "query_config": {},
                "viz_config": {}
            }
        ]
    }
}
```

### Messages you can send (client → server)

#### `ping` — keepalive
```json
{ "type": "ping", "timestamp": 1712345678 }
```
Response:
```json
{ "type": "pong", "timestamp": 1712345678 }
```

#### `refresh_widget` — force re-query a widget's data
```json
{ "type": "refresh_widget", "widget_id": "uuid" }
```
Broadcasts a `widget_update` to all clients on this dashboard.

#### `update_layout` — save drag-and-drop layout
```json
{
    "type": "update_layout",
    "layout": { "columns": 12, "rowHeight": 100 },
    "widgets": [
        { "id": "uuid", "position": { "x": 0, "y": 0, "w": 6, "h": 4 } }
    ]
}
```
Persists widget positions to the database and broadcasts `layout_updated` to all clients.

#### `add_widget` — create a new widget
```json
{
    "type": "add_widget",
    "widget": {
        "widget_type": "bar_chart",
        "title": "Sales by Region",
        "table_id": "uuid",
        "query_config": { "aggregations": [{ "type": "count", "group_by": "region", "name": "val" }] },
        "viz_config": { "x_axis": "region", "y_axis": "val" },
        "position": { "x": 0, "y": 0, "w": 6, "h": 4 }
    }
}
```
Broadcasts `widget_added` to all clients.

#### `delete_widget` — remove a widget
```json
{ "type": "delete_widget", "widget_id": "uuid" }
```
Broadcasts `widget_deleted` to all clients.

#### `request_insights` — trigger AI insight generation
```json
{ "type": "request_insights" }
```
Response:
```json
{ "type": "insights_generation_started", "task_id": "celery-task-id" }
```

### Messages you receive (server → client)

| `type` | When | Key fields |
|---|---|---|
| `dashboard_state` | Immediately on connect | `data` — full dashboard serialised object |
| `widget_update` | When data changes (record save/delete) | `widget_id`, `data` |
| `layout_updated` | After any client saves layout | `layout`, `widgets`, `updated_by` |
| `widget_added` | After a widget is created | `widget` |
| `widget_deleted` | After a widget is deleted | `widget_id` |
| `user_joined` | When another user connects | `user` (email) |
| `user_left` | When another user disconnects | `user` (email) |
| `error` | On any server-side error | `message` |

---

## Workspace Consumer (`ws/workspace/<workspace_id>/`)

Receives table-level change notifications across the entire workspace.

### Messages you receive

| `type` | When | Key fields |
|---|---|---|
| `table_update` | When records are saved or deleted in any table | `table_id`, `action` (`"saved"` or `"deleted"`) |

### Messages you can send

Only `ping` / `pong` keepalive is supported.

---

## Table Consumer (`ws/table/<table_id>/`)

Fine-grained record change notifications for a single table.

### Messages you can send

#### `record_created`
```json
{ "type": "record_created", "record": { "id": "uuid", "data": {} } }
```

#### `record_updated`
```json
{ "type": "record_updated", "record": { "id": "uuid", "data": {} } }
```

### Messages you receive

| `type` | Key fields |
|---|---|
| `record_update` | `record` (object), `action` (`"created"` or `"updated"`) |

---

## How automatic widget refresh works

When a `Record` is saved or deleted:

1. `Record.save()` / `Record.delete()` calls `_trigger_updates(action)`
2. This queues a `broadcast_widget_update` Celery task for every widget that uses the affected table
3. The Celery task fetches fresh widget data and sends a `widget_update` message to the `dashboard_<id>` channel group
4. All connected clients on that dashboard receive the update simultaneously

This means **widgets update in real time** whenever any team member adds or edits a record — no page refresh needed.

---

## Close codes

| Code | Meaning |
|---|---|
| `4001` | Unauthenticated — user is not logged in |
| `4003` | Forbidden — user is not a member of this workspace |
| `1000` | Normal closure |
