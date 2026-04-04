# Widget Configuration Reference

Each `Widget` has two JSON config fields: `query_config` (what data to fetch) and `viz_config` (how to render it). This document covers both.

---

## Widget types

| `widget_type` | Description |
|---|---|
| `metric` | Single KPI number (e.g. "Total Revenue: KES 1.2M") |
| `number` | Same as metric, simpler card layout |
| `line_chart` | Time-series or trend line |
| `bar_chart` | Vertical or horizontal bar chart |
| `pie_chart` | Pie / donut chart |
| `scatter` | Scatter plot |
| `gauge` | Gauge / speedometer for a single value |
| `heatmap` | Two-dimensional colour density map |
| `table` | Paginated data table |

---

## `query_config`

Controls what data is fetched from the `DataTable`.

### Common structure

```json
{
    "aggregations": [ ... ],
    "filters": [ ... ],
    "limit": 1000
}
```

### `aggregations` — array

Each item defines a computed value:

| Field | Type | Description |
|---|---|---|
| `type` | string | `count`, `sum`, `avg`, `min`, `max` |
| `field` | string | Column name from the table schema (omit for `count`) |
| `name` | string | Output key name in the result (e.g. `"val"`) |
| `group_by` | string | Column name to group by (creates a series) |

**Examples:**

```json
// Count all records
{ "type": "count", "name": "val" }

// Sum a currency field
{ "type": "sum", "field": "amount", "name": "val" }

// Average per category
{ "type": "avg", "field": "score", "group_by": "region", "name": "val" }

// Count over time
{ "type": "count", "group_by": "created_at_date", "name": "val" }
```

> **Tip**: Use `created_at_date` as `group_by` to group by record creation date even when the table has no explicit date field.

### `filters` — array

Each item narrows which records are included:

| Field | Description |
|---|---|
| `field` | Column name |
| `operator` | `eq`, `neq`, `contains`, `gt`, `lt` |
| `value` | Value to compare against |

```json
{
    "filters": [
        { "field": "status",  "operator": "eq",       "value": "completed" },
        { "field": "amount",  "operator": "gt",        "value": 1000 },
        { "field": "product", "operator": "contains",  "value": "Widget" }
    ]
}
```

### `limit`

Maximum number of records returned (default `1000`). Relevant for the `table` widget type.

```json
{ "limit": 50 }
```

---

## `viz_config`

Controls rendering. Fields depend on `widget_type`.

### `metric` / `number`

```json
{
    "icon":   "fa-coins",
    "color":  "green",
    "prefix": "KES ",
    "suffix": ""
}
```

| Field | Values |
|---|---|
| `color` | `blue`, `green`, `orange`, `purple`, `red`, `teal` |
| `icon` | Any Font Awesome 6 icon class, e.g. `fa-chart-bar` |
| `prefix` | String shown before the value, e.g. `"KES "` or `"$"` |
| `suffix` | String shown after the value, e.g. `"%"` |

### `line_chart` / `bar_chart` / `scatter`

```json
{
    "x_axis": "created_at_date",
    "y_axis": "val",
    "title":  "Revenue Over Time",
    "color":  "blue"
}
```

| Field | Description |
|---|---|
| `x_axis` | Key from the aggregation result to use as the X axis |
| `y_axis` | Key from the aggregation result to use as the Y axis (`"val"` by default) |
| `color` | Chart colour theme |

### `pie_chart`

```json
{
    "label_field": "region",
    "value_field": "val"
}
```

### `table`

```json
{
    "page_size":   20,
    "show_search": true
}
```

### `gauge`

```json
{
    "min":    0,
    "max":    100,
    "suffix": "%"
}
```

---

## Complete examples

### KPI card — total sales
```json
{
    "widget_type": "metric",
    "title": "Total Sales",
    "query_config": {
        "aggregations": [
            { "type": "sum", "field": "amount", "name": "val" }
        ]
    },
    "viz_config": {
        "icon": "fa-coins",
        "color": "green",
        "prefix": "KES "
    }
}
```

### Line chart — revenue over time
```json
{
    "widget_type": "line_chart",
    "title": "Revenue Over Time",
    "query_config": {
        "aggregations": [
            { "type": "sum", "field": "amount", "group_by": "sale_date", "name": "val" }
        ]
    },
    "viz_config": {
        "x_axis": "sale_date",
        "y_axis": "val",
        "color": "blue"
    }
}
```

### Bar chart — sales by region (filtered to completed)
```json
{
    "widget_type": "bar_chart",
    "title": "Completed Sales by Region",
    "query_config": {
        "aggregations": [
            { "type": "count", "group_by": "region", "name": "val" }
        ],
        "filters": [
            { "field": "status", "operator": "eq", "value": "completed" }
        ]
    },
    "viz_config": {
        "x_axis": "region",
        "y_axis": "val",
        "color": "orange"
    }
}
```

### Paginated data table
```json
{
    "widget_type": "table",
    "title": "Recent Orders",
    "query_config": {
        "limit": 20
    },
    "viz_config": {
        "page_size": 20,
        "show_search": true
    }
}
```
