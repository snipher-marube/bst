# Frontend Patterns

AnalyticsMeta's frontend is intentionally server-driven.  Django renders
full HTML pages; **Alpine.js** adds reactive behaviour to individual
components without a build step; **HTMX** is loaded but used sparingly for
progressive enhancement.  **Tailwind CSS v4** handles styling.

---

## Stack overview

| Library | Version | CDN / build | Purpose |
|---|---|---|---|
| **Alpine.js** | 3.x | CDN `<script defer>` | Component reactivity, modals, state machines |
| **HTMX** | 1.9.10 | CDN `<script>` | Partial page replacement (used lightly) |
| **Tailwind CSS** | v4 | npm build (`npm run dev`) | Utility-first styling |
| **Plotly.js** | latest | CDN | Chart rendering inside widgets |
| **Font Awesome** | 6 | CDN | Icons |

CDN tags live in `templates/base.html`.  Tailwind output is compiled to
`static/css/output.css` by the npm dev script.

---

## Alpine.js conventions

### Where components are defined

Alpine components are written as plain JavaScript functions in `<script>`
tags at the bottom of the template that uses them.  There is **no shared
JS bundle** — each page owns its own Alpine code.

```html
<!-- at the bottom of a template -->
<script>
function mpesaPayment() {
    return {
        modalOpen: false,
        step: 'input',   // 'input' | 'waiting' | 'success' | 'failed'
        phone: '',
        loading: false,
        errorMsg: '',
        ...
    }
}
</script>

<!-- mounted on a wrapping div -->
<div x-data="mpesaPayment()">
  ...
</div>
```

### Component lifecycle

Use `x-init` for one-time setup (e.g. fetching initial data, opening a
WebSocket connection):

```html
<div x-data="dashboardShell()" x-init="init()">
```

```js
function dashboardShell() {
    return {
        sidebarOpen: false,
        init() {
            // runs once after Alpine mounts the component
        }
    }
}
```

### Event handling

Use `@click` / `@submit` / `@keydown` shorthand rather than `x-on:`:

```html
<button @click="openModal('starter', 'KES 2,500', workspaceId)">
  Upgrade
</button>
```

### Conditional rendering

| Directive | Behaviour |
|---|---|
| `x-show` | Toggles `display: none`; element stays in DOM |
| `x-if` | Removes element from DOM when false |

Prefer `x-show` for things that toggle often (modals, spinners); use `x-if`
for elements that should never be in the DOM when hidden (e.g. sensitive
content).

```html
<div x-show="loading">
  <i class="fas fa-spinner fa-spin"></i> Loading…
</div>

<template x-if="isOwner">
  <button class="text-red-600">Delete workspace</button>
</template>
```

### Two-way binding

Use `x-model` to sync an `<input>` with a reactive property:

```html
<input type="tel" x-model="phone" placeholder="7XXXXXXXX" maxlength="9">
```

### Dynamic text

Use `x-text` to set an element's `textContent` reactively:

```html
<span x-show="errorMsg" x-text="errorMsg" class="text-red-600"></span>
```

### Transitions

Alpine's `x-transition` modifier animates enter/leave visibility changes:

```html
<div x-show="modalOpen" x-transition.opacity class="fixed inset-0 ...">
```

---

## State machine pattern (M-Pesa modal)

Multi-step flows use a `step` string property as a simple state machine.
Each step has a corresponding `x-show="step === '...'"` block:

```html
<div x-show="step === 'input'">    <!-- phone entry form -->
<div x-show="step === 'waiting'">  <!-- polling spinner -->
<div x-show="step === 'success'">  <!-- confirmation -->
<div x-show="step === 'failed'">   <!-- error + retry -->
```

Transitions between steps happen inside async methods that call the
Django REST API:

```js
async initiatePayment() {
    this.loading = true
    const res = await fetch('/subscriptions/mpesa/stk-push/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)[1],
        },
        body: JSON.stringify({ phone: '254' + this.phone, workspace_id: this.workspaceId, plan: this.plan }),
    })
    const data = await res.json()
    if (data.CheckoutRequestID) {
        this.checkoutRequestId = data.CheckoutRequestID
        this.step = 'waiting'
        this.pollStatus()
    } else {
        this.errorMsg = data.error || 'Could not initiate payment'
    }
    this.loading = false
},
```

---

## Calling Django views from Alpine

### CSRF token

Django requires a `X-CSRFToken` header on all non-safe requests.  Read it
from the cookie — the CSRF cookie is always available because
`CSRF_USE_SESSIONS = False` (the default):

```js
const csrfToken = document.cookie
    .split('; ')
    .find(row => row.startsWith('csrftoken='))
    ?.split('=')[1]

fetch('/some/endpoint/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
    },
    body: JSON.stringify(payload),
})
```

### Workspace header for REST API

REST API views use `HasWorkspaceAccess` which reads the workspace UUID from
the `X-Workspace-ID` request header.  Pass it on every API call:

```js
fetch('/api/v1/tables/', {
    headers: {
        'X-Workspace-ID': workspaceId,   // UUID string
    },
})
```

### Polling pattern

Long-running operations (M-Pesa STK Push, report generation) expose a
status endpoint.  Poll it at a fixed interval and stop when a terminal
state is reached:

```js
async pollStatus() {
    const interval = setInterval(async () => {
        const res = await fetch(`/subscriptions/mpesa/status/${this.checkoutId}/`)
        const data = await res.json()
        if (data.status === 'completed') {
            clearInterval(interval)
            this.step = 'success'
        } else if (data.status === 'failed') {
            clearInterval(interval)
            this.errorMsg = data.message || 'Payment failed'
            this.step = 'failed'
        }
    }, 3000)   // poll every 3 seconds
    this.pollInterval = interval
}
```

Always store the interval ID so you can `clearInterval` when the component
unmounts (use Alpine's `x-effect` or the `destroy()` hook if needed).

---

## HTMX

HTMX is loaded but used **sparingly** — most interactions use full page
redirects (Django messages + redirect) or Alpine + `fetch()`.  HTMX is
available for future partial-page replacements without requiring a rebuild.

If you add an HTMX interaction, the standard pattern is:

```html
<button
  hx-post="/dashboard/some/action/"
  hx-target="#result-container"
  hx-swap="innerHTML"
  hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'
>
  Do something
</button>
<div id="result-container"></div>
```

The view should return an HTML fragment (not a full page) when it detects
the `HX-Request: true` header:

```python
def my_view(request):
    if request.headers.get('HX-Request'):
        return render(request, 'fragments/result.html', context)
    return render(request, 'full/page.html', context)
```

---

## Tailwind CSS

### Development

Tailwind v4 scans templates on the fly.  Run the watcher while developing:

```bash
npm run dev      # watches templates/ and static/ for class changes
```

### Production

The CI/CD pipeline and Docker build run `npm run build` to generate a
minified `static/css/output.css`.  `whitenoise` serves this file.

### Conventions

- Use **Tailwind utilities only** — no custom CSS unless absolutely
  necessary.
- Colours follow the brand palette: `[#03466e]` (primary blue),
  `[#f8fafc]` (page background).
- All interactive states use Tailwind's `hover:`, `focus:`, `disabled:`
  variants rather than JavaScript class toggling.

---

## WebSocket integration with Alpine

Dashboard pages open a WebSocket connection via the browser's native API
and update Alpine reactive data when push events arrive.  See
[docs/websockets.md](./websockets.md) for the full event reference.

Minimal pattern:

```js
function dashboardShell() {
    return {
        ws: null,
        init() {
            const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
            this.ws = new WebSocket(`${protocol}://${location.host}/ws/dashboard/${dashboardId}/`)
            this.ws.onmessage = (event) => {
                const msg = JSON.parse(event.data)
                if (msg.type === 'widget_update') {
                    // update reactive widget data
                    this.widgets[msg.widget_id] = msg.data
                }
            }
        }
    }
}
```
