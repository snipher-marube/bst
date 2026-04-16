# OpenAPI Schema — AnalyticsMeta REST API

AnalyticsMeta exposes a machine-readable OpenAPI 3.0 schema and two interactive
documentation UIs, generated automatically by
[drf-spectacular](https://drf-spectacular.readthedocs.io/).

---

## Endpoints

| URL | Purpose |
|---|---|
| `GET /api/v1/schema/` | Raw OpenAPI 3.0 JSON/YAML schema (downloadable) |
| `GET /api/v1/docs/` | Swagger UI — interactive browser for the API |
| `GET /api/v1/redoc/` | ReDoc — read-only, printable API reference |

All three endpoints are public (no authentication required to view the schema
itself). Individual API operations still require a token — see
[docs/api-authentication.md](api-authentication.md).

---

## Quick start

```bash
# Install (already in requirements.txt)
pip install drf-spectacular

# Dump the raw schema to a file
python manage.py spectacular --color --file schema.yml
```

Then open `http://localhost:8000/api/v1/docs/` while the dev server is running.

---

## How it works

`drf-spectacular` inspects every DRF view registered under `/api/v1/` and
generates an OpenAPI 3.0 document automatically. It reads:

- **Serializer fields** → request/response body schemas
- **URL parameters** — path converters and query params
- **Permission classes** → security requirements
- **`@extend_schema` decorator** → manual overrides where auto-detection falls short

The schema is served live at runtime — it always reflects the current code.

---

## Customising a view's schema

When the auto-generated schema is wrong or incomplete, use the
`@extend_schema` decorator from `drf_spectacular.utils`:

```python
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

@extend_schema(
    summary="Export table records",
    description="Export all records from a DataTable as CSV, JSON, or Excel.",
    parameters=[
        OpenApiParameter(
            name='format',
            type=OpenApiTypes.STR,
            enum=['csv', 'json', 'xlsx'],
            location=OpenApiParameter.QUERY,
            description="Output format (default: csv)",
            required=False,
        ),
    ],
    responses={200: None},   # binary download — no response serializer
)
def export_table(request, table_id):
    ...
```

### Marking an endpoint as requiring token auth

Token auth is the project default (`DEFAULT_AUTHENTICATION_CLASSES`), so most
endpoints are already marked. For endpoints that are explicitly public (e.g.
the newsletter subscribe form), opt out with:

```python
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny

@extend_schema(auth=[])
class PublicEndpointView(APIView):
    permission_classes = [AllowAny]
    ...
```

---

## Schema generation settings

Configured in `SPECTACULAR_SETTINGS` in `config/settings/base.py`:

```python
SPECTACULAR_SETTINGS = {
    'TITLE': 'AnalyticsMeta API',
    'DESCRIPTION': '...',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,        # hides /schema/ from its own schema
    'SWAGGER_UI_SETTINGS': {
        'persistAuthorization': True,     # auth token survives page refresh
        'displayRequestDuration': True,   # shows response time in Swagger UI
    },
}
```

Full option reference: https://drf-spectacular.readthedocs.io/en/latest/settings.html

---

## Downloading the schema for external tools

```bash
# YAML (default)
python manage.py spectacular --color --file schema.yml

# JSON
python manage.py spectacular --color --format json --file schema.json
```

Import `schema.yml` into Postman, Insomnia, or any OpenAPI-compatible client
to get auto-generated request collections.

---

## CI check (prevent schema drift)

Add this to your CI pipeline to fail if a developer changes a serializer
without regenerating the committed schema:

```bash
python manage.py spectacular --color --validate --fail-on-warn
```

Or to diff against a committed reference:

```bash
python manage.py spectacular --color --file /tmp/schema.yml
diff schema.yml /tmp/schema.yml
```
