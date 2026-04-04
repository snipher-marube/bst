# API Authentication

The REST API at `/api/v1/` supports two authentication methods: **Session** (browser-based) and **Token** (for API clients / mobile apps).

> **Note:** The README mentions "JWT" — this is incorrect. The API uses DRF's built-in **Token authentication** (`rest_framework.authtoken`), not JWT.

---

## Method 1 — Token Authentication (recommended for API clients)

### Obtain a token

Tokens are created per user and do not expire by default. A user gets one token for their account.

**Via Django admin:**
1. Log into `/admin/`
2. Go to **Auth Token → Tokens → Add**
3. Select the user and save — copy the generated token

**Via shell (programmatic):**
```bash
python manage.py shell -c "
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
User = get_user_model()
user = User.objects.get(email='you@example.com')
token, _ = Token.objects.get_or_create(user=user)
print(token.key)
"
```

### Use the token

Pass it in every request as a `Bearer` or `Token` header:

```bash
# Using curl
curl -H "Authorization: Token YOUR_TOKEN_HERE" \
     https://your-app.onrender.com/api/v1/workspaces/

# Or with Bearer prefix (both accepted)
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     https://your-app.onrender.com/api/v1/workspaces/
```

```python
# Using Python requests
import requests

headers = {"Authorization": "Token YOUR_TOKEN_HERE"}
resp = requests.get("https://your-app.onrender.com/api/v1/workspaces/", headers=headers)
print(resp.json())
```

```javascript
// Using fetch (browser / Node.js)
const resp = await fetch('https://your-app.onrender.com/api/v1/workspaces/', {
    headers: { 'Authorization': 'Token YOUR_TOKEN_HERE' }
});
const data = await resp.json();
```

---

## Method 2 — Session Authentication (browser clients)

If you are making requests from within a logged-in browser session (e.g. from the Django templates using HTMX or Alpine.js), session auth works automatically. You must include the CSRF token for non-safe methods (`POST`, `PUT`, `PATCH`, `DELETE`).

```javascript
// Get CSRF token from cookie
function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
}

// POST with CSRF
await fetch('/api/v1/workspaces/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCookie('csrftoken'),
    },
    body: JSON.stringify({ name: 'My Workspace' }),
});
```

---

## Common API examples

All examples use Token auth. Replace `$TOKEN` and `$BASE` with your values.

```bash
export TOKEN=your-token-here
export BASE=https://your-app.onrender.com/api/v1
```

### List your workspaces
```bash
curl -H "Authorization: Token $TOKEN" $BASE/workspaces/
```

### Create a workspace
```bash
curl -X POST -H "Authorization: Token $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name": "Marketing Team"}' \
     $BASE/workspaces/
```

### List tables in a workspace
```bash
WORKSPACE_ID=your-workspace-uuid
curl -H "Authorization: Token $TOKEN" $BASE/workspaces/$WORKSPACE_ID/tables/
```

### Create a table
```bash
curl -X POST -H "Authorization: Token $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Sales Records",
       "description": "Monthly sales data",
       "schema": [
         {"name": "product", "type": "text", "required": true},
         {"name": "amount",  "type": "currency", "required": true},
         {"name": "date",    "type": "date", "required": false}
       ]
     }' \
     $BASE/workspaces/$WORKSPACE_ID/tables/
```

### Add a record
```bash
TABLE_ID=your-table-uuid
curl -X POST -H "Authorization: Token $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"data": {"product": "Widget A", "amount": 4500, "date": "2026-04-01"}}' \
     $BASE/tables/$TABLE_ID/records/
```

### List records (paginated)
```bash
curl -H "Authorization: Token $TOKEN" "$BASE/tables/$TABLE_ID/records/?page=1"
```

### Get user profile
```bash
curl -H "Authorization: Token $TOKEN" $BASE/auth/profile/
```

---

## Error responses

| Status | Meaning |
|---|---|
| `401 Unauthorized` | Missing or invalid token |
| `403 Forbidden` | Authenticated but no access to the resource |
| `404 Not Found` | Resource does not exist or belongs to another workspace |
| `429 Too Many Requests` | Rate limit exceeded (100/day anon, 1000/hour user) |

```json
// 401 example
{ "detail": "Authentication credentials were not provided." }

// 403 example
{ "detail": "You do not have permission to perform this action." }
```

---

## Rate limits

Set in `base.py` via DRF throttling:

| Client type | Limit |
|---|---|
| Unauthenticated | 100 requests / day |
| Authenticated | 1 000 requests / hour |
