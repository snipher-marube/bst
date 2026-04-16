# Webhook Data Ingestion

Webhook endpoints let any external system — Zapier, Make (formerly Integromat), a custom script, or another service — push JSON records directly into a DataTable without uploading a CSV file.

Each endpoint is scoped to a single DataTable and protected with an HMAC-SHA256 signature so only authorised senders can write data.

---

## How It Works

```
1. A workspace admin creates a WebhookEndpoint via the API.
   The server generates a unique token (URL routing) and secret (HMAC signing).

2. The external service POSTs a JSON payload to:
       POST /webhook/ingest/<token>/

3. The view verifies the X-Hub-Signature-256 header.
   If invalid → HTTP 401.  If the endpoint is inactive → HTTP 404.

4. The payload is parsed:
   - JSON object  → one Record created
   - JSON array   → one Record per element (max 1000 per request)

5. Records are saved into the target DataTable.
   Real-time widget refresh triggers automatically (same path as CSV import).

6. Telemetry updated: total_requests += 1, last_request_at = now().
```

---

## Key Files

| File | Purpose |
|---|---|
| `apps/dashboards/models.py` | `WebhookEndpoint` model |
| `apps/dashboards/api_v1.py` | `WebhookEndpointListCreateAPIView`, `WebhookEndpointDetailAPIView`, `WebhookEndpointRegenerateSecretAPIView`, `WebhookIngestView` |
| `apps/dashboards/serializers.py` | `WebhookEndpointSerializer` |
| `apps/dashboards/urls_api_v1.py` | Management API URL patterns |
| `config/urls.py` | Public ingest URL (`/webhook/ingest/<token>/`) |

---

## WebhookEndpoint Model

```python
class WebhookEndpoint(models.Model):
    workspace       # ForeignKey(Workspace)
    table           # ForeignKey(DataTable) — records are written here
    created_by      # ForeignKey(User)

    name            # CharField — human-readable label
    token           # CharField(64) — random hex, embedded in the URL
    secret          # CharField(128) — HMAC signing key, shown once on creation

    is_active       # BooleanField — set False to stop accepting without deleting

    # Telemetry (read-only, updated by the ingest view)
    total_requests  # PositiveIntegerField
    last_request_at # DateTimeField (null)
```

`token` and `secret` are generated automatically on creation via `WebhookEndpoint.generate_token()` and `WebhookEndpoint.generate_secret()`. They are never derived from user input.

---

## Management REST API

All endpoints require authentication (`Authorization: Token <token>`) and `X-Workspace-ID` header.

### List webhook endpoints

```http
GET /api/v1/workspaces/<workspace_id>/webhooks/
```

**Response 200:**
```json
[
    {
        "id": "uuid",
        "workspace": "uuid",
        "table": "uuid",
        "name": "Stripe Payments",
        "token": "a3f9...",
        "secret": "Xk2p...",
        "is_active": true,
        "total_requests": 142,
        "last_request_at": "2026-04-12T09:55:00Z",
        "created_at": "2026-04-01T08:00:00Z",
        "updated_at": "2026-04-01T08:00:00Z"
    }
]
```

> **Security note:** `secret` is returned in list and detail responses so that workspace admins can view and rotate it. Only share the secret with the intended sending service — treat it like a password.

### Create an endpoint

```http
POST /api/v1/workspaces/<workspace_id>/webhooks/
Content-Type: application/json

{
    "table": "<table_uuid>",
    "name": "Stripe Payments"
}
```

**Response 201:** the created endpoint object including the generated `token` and `secret`. Store the `secret` now — it is visible in subsequent GET requests, but you should treat rotation as the recovery path if it is ever leaked.

### Get / update / delete an endpoint

```http
GET    /api/v1/webhooks/<endpoint_id>/
PATCH  /api/v1/webhooks/<endpoint_id>/    # partial update (name, is_active)
DELETE /api/v1/webhooks/<endpoint_id>/
```

### Rotate the signing secret

```http
POST /api/v1/webhooks/<endpoint_id>/regenerate-secret/
```

**Response 200:**
```json
{ "secret": "new-secret-value" }
```

Requires owner or admin role (`CanManageWorkspace`). The old secret is immediately invalidated — update the sending service before rotating.

---

## Ingest Endpoint (Public)

```http
POST /webhook/ingest/<token>/
Content-Type: application/json
X-Hub-Signature-256: sha256=<hmac_hex>
```

This endpoint requires **no authentication token** — the `token` in the URL and the HMAC signature together serve as both routing and authentication.

### Signing requests

Compute the signature as HMAC-SHA256 of the **raw request body bytes** using the `secret` as the key:

```python
import hashlib, hmac, json

payload = {"amount": 1500, "customer": "Jane Doe"}
body = json.dumps(payload).encode("utf-8")
secret = "your-endpoint-secret"

sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
# → "sha256=abc123..."
```

Send the result as the `X-Hub-Signature-256` request header.

### Node.js example

```javascript
const crypto = require('crypto');
const axios  = require('axios');

const payload = JSON.stringify({ amount: 1500, customer: 'Jane Doe' });
const secret  = 'your-endpoint-secret';
const sig     = 'sha256=' + crypto.createHmac('sha256', secret)
                                  .update(payload)
                                  .digest('hex');

await axios.post('https://your-app.com/webhook/ingest/<token>/', payload, {
    headers: {
        'Content-Type':          'application/json',
        'X-Hub-Signature-256':   sig,
    },
});
```

### Payload formats

**Single record (JSON object):**
```json
{ "amount": 1500, "customer": "Jane Doe", "region": "Nairobi" }
```
→ 1 record created.

**Multiple records (JSON array):**
```json
[
    { "amount": 1500, "customer": "Jane Doe" },
    { "amount": 2300, "customer": "John Kamau" }
]
```
→ 2 records created. Maximum 1000 elements per request.

### Response

**Success (HTTP 200):**
```json
{ "created": 2 }
```

**Partial success (some rows failed validation):**
```json
{
    "created": 1,
    "errors": [
        { "index": 1, "error": "Record validation failed: Field 'amount' must be a number" }
    ]
}
```

**All rows failed (HTTP 400):**
```json
{
    "created": 0,
    "errors": [...]
}
```

**Invalid/missing signature (HTTP 401):**
```json
{ "error": "Invalid signature" }
```

**Inactive or unknown endpoint (HTTP 404):** no body.

---

## Schema validation

Records are validated against the target DataTable's schema before being saved. If the table has no schema defined, any JSON object is accepted. If schema validation fails for a row, that row is skipped and reported in the `errors` array — other valid rows in the same request are still saved.

---

## Zapier / Make integration

### Zapier Webhooks action

1. Choose action **"Webhooks by Zapier" → POST**.
2. URL: `https://your-app.com/webhook/ingest/<token>/`
3. Payload type: **JSON**
4. Data: map Zapier fields to your DataTable columns.
5. Headers: add `X-Hub-Signature-256` using a Zapier Code step to compute the HMAC (see Python example above).

### Make (Integromat) HTTP module

1. Add an **HTTP → Make a request** module.
2. Method: POST, URL as above.
3. Body type: Raw (JSON).
4. Add the `X-Hub-Signature-256` header using a Make function or a Tools → Set variable module with the HMAC computation.

---

## Security considerations

- **Keep the secret private.** Anyone with both the `token` and `secret` can write arbitrary records to your DataTable.
- **Rotate immediately if compromised.** Use `POST /api/v1/webhooks/<id>/regenerate-secret/` — the old secret stops working instantly.
- **Disable before deleting.** Set `is_active = false` to stop accepting requests while you audit usage, then delete.
- **The token is not a secret.** It only routes the request to the right endpoint. The HMAC signature is the actual security mechanism.

---

## Testing

```bash
python manage.py test apps.dashboards.tests.TestWebhookEndpointAPI \
                      apps.dashboards.tests.TestWebhookIngestView \
                      --settings=config.settings.test
```

Tests cover:
- Endpoint CRUD (create, list, get, patch, delete)
- Secret rotation — old secret invalidated, new one returned
- Ingest: valid object payload, valid array payload
- Ingest: invalid HMAC → 401, missing signature → 401
- Inactive endpoint → 404, unknown token → 404
- Telemetry increment after successful ingest
