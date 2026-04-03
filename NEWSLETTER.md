# Newsletter System — Operations Guide

AnalyticsMeta's newsletter is a full email-marketing pipeline: subscriber management,
campaign authoring, scheduled sending, open/click tracking, bounce handling, and
GDPR-compliant unsubscribe. This guide covers every workflow you need day-to-day.

---

## Architecture at a glance

```
Subscriber signs up (website form)
  → NewsletterSubscriptionForm validates
  → Subscriber created (status = PENDING)
  → Confirmation email sent via SMTP (Celery task)
  → Subscriber clicks link → status = ACTIVE

You create a Campaign (admin)
  → Write content in the visual editor
  → Assign to one or more Lists
  → Send test → approve → queue for sending
  → Celery bulk-sends in batches of 100
  → Open/click tracking pixels embedded automatically
  → Stats update in real-time
```

---

## 1. Managing Subscriber Lists

Go to **Admin → Newsletter → Lists**.

| Field | Purpose |
|---|---|
| Name / Slug | Human name and URL-safe identifier |
| Public | If checked, subscribers can self-select it from the sign-up form |
| Double opt-in | Require email confirmation before activating |

**Best practice** — keep lists focused:
- `Product Updates` — feature announcements
- `Weekly Digest` — curated content
- `Onboarding` — new-user drip sequence

Subscribers can belong to multiple lists. Campaigns target lists, not individual emails.

---

## 2. Growing Your Subscriber Base

### Via the website form
The public signup endpoint is `POST /newsletter/api/subscribe/`.
Pass `email`, optional `first_name`, `consent=true`, and optional `list_id`.

```html
<!-- Example embed on any page -->
<form id="nl-form" method="post" action="/newsletter/api/subscribe/">
  {% csrf_token %}
  <input type="email" name="email" placeholder="Your email" required />
  <input type="hidden" name="list_id" value="1" />
  <input type="checkbox" name="consent" required />
  <label>I agree to receive newsletters</label>
  <button type="submit">Subscribe</button>
</form>
```

The endpoint returns JSON `{"success": true, "message": "..."}` — handle it with JS.

### Via CSV import (admin)
1. Go to **Admin → Newsletter → Subscribers → Import** (or use the bulk-upload action).
2. Subscribers created via import are given `source = import` and `status = active`
   (skip double opt-in since you've collected consent offline).

### Via API
`POST /newsletter/api/subscribe/` accepts `application/json` as well as form data.

---

## 3. Creating a Campaign

Go to **Admin → Newsletter → Campaigns → Add Campaign**.

### Fields reference

| Field | Notes |
|---|---|
| **Name** | Internal reference (not visible to subscribers) |
| **Subject** | The email subject line — most important for open rates |
| **Preheader** | Preview text shown after subject in the inbox (~90 chars) |
| **Content** | Use the visual editor (see §4) |
| **Lists** | Which subscriber lists to send to |
| **Priority** | `Normal` for most campaigns; `Urgent` jumps the Celery queue |
| **From Email** | Leave blank to use `DEFAULT_FROM_EMAIL` from settings |
| **Reply-To** | If subscribers reply, where it goes |
| **Track Opens** | Embeds a 1×1 pixel — recommended ON |
| **Track Clicks** | Wraps all links — recommended ON |
| **Schedule** | Set a future date/time to auto-send via `send_campaigns` cron |

---

## 4. Using the Visual Editor

The **Content** field uses **Jodit**, a production-grade WYSIWYG editor with a
full toolbar and raw HTML source toggle.

### Toolbar quick reference

| Button | What it does |
|---|---|
| `</>` (Source) | **Toggle raw HTML** — essential for pasting pre-built email HTML |
| Bold / Italic / Underline | Inline formatting |
| Paragraph / H1-H3 | Block-level headings |
| Brush | Text & background colour picker |
| Align | Left / centre / right / justify |
| Link | Insert hyperlink (auto-tracked when send has Track Clicks ON) |
| Image | Insert image by URL |
| Table | Insert table (useful for multi-column email layouts) |
| Fullscreen | Expand editor to full browser window |

### Working tips

**Start from a template**
Use the HTML source button (`</>`) to paste a pre-built email template, then
switch back to visual mode to edit copy. Keep all styles inline — email clients
strip `<style>` blocks.

**Personalisation tokens**
In the HTML content you can use:
```
{{ subscriber.first_name }}   → subscriber's first name
{{ subscriber.email }}        → their email address
{{ unsubscribe_url }}         → auto-generated unsubscribe link (added automatically)
```
Celery replaces these at send time.

**Image hosting**
Host images on your own domain or a CDN — do not use Gmail attachments.
Reference them with absolute URLs: `<img src="https://yourdomain.com/static/images/logo.png">`.

**Testing before sending**
Always send a test first (see §5). Check it in:
- Gmail
- Outlook (uses Word rendering engine — watch table/font issues)
- Apple Mail
- Mobile (iPhone & Android)

---

## 5. Sending a Campaign

### The one-click flow (recommended)

Open any campaign in the admin. At the bottom of the edit page you'll see a
dark blue **Campaign Actions** bar with three buttons:

| Button | What it does |
|---|---|
| **🧪 Send Test** | Opens a small modal — enter an email → click Send Test. Done. |
| **👁️ Preview** | Opens the campaign HTML in a new browser tab for visual review. |
| **🚀 Send Now** | Opens a confirm modal showing list count + subject → click Confirm & Send. Done. |

The bar only appears on **existing** campaigns (not when adding a new one).
The **Send Now** button is hidden once the campaign status moves to Sending/Sent.

**Recommended pre-send checklist:**
1. Open campaign → click **Preview** — confirm it looks right in the browser.
2. Click **Send Test** → enter your own email → check it in Gmail, Outlook, and on mobile.
3. If it looks good, click **Send Now** → confirm.

Celery picks it up immediately and sends in batches of 100. The campaign status
updates to **Sending** and stats start appearing within seconds.

### Scheduled sending (auto-send at a future time)

Set the **Scheduled For** field to a future datetime, save, and leave the
campaign status as **Scheduled**. A cron job dispatches it automatically:

```bash
# Run every 5 minutes
*/5 * * * * cd /path/to/project && python manage.py send_campaigns
```

Or with Celery Beat — add to `CELERY_BEAT_SCHEDULE`:
```python
'send-scheduled-campaigns': {
    'task': 'newsletter.tasks.send_campaign',
    'schedule': crontab(minute='*/5'),
},
```

---

## 6. Reading Campaign Stats

Open any sent campaign. The **Performance Statistics** fieldset shows:

| Metric | Description |
|---|---|
| Total Recipients | Subscribers targeted |
| Total Sent | Successfully delivered |
| Total Opens | Unique pixel loads |
| **Open Rate** | Opens ÷ Sent × 100 |
| Total Clicks | Link clicks tracked |
| **Click Rate** | Clicks ÷ Sent × 100 |
| Total Bounces | Hard + soft bounces |
| Total Unsubscribes | Unsubscribes from this campaign |

**Industry benchmarks** (B2B SaaS):
- Open rate > 25% = excellent
- Click rate > 3% = excellent
- Bounce rate < 2% = healthy list

---

## 7. Subscriber Health

Go to **Admin → Newsletter → Subscribers**.

### Status meanings

| Status | Meaning | Action |
|---|---|---|
| `ACTIVE` | Confirmed, receives emails | — |
| `PENDING` | Signed up but not confirmed | Re-send confirmation if stuck |
| `UNSUBSCRIBED` | Opted out | Do not re-add manually |
| `BOUNCED` | Hard bounce (bad address) | Remove from list |
| `COMPLAINED` | Marked as spam | Remove immediately |

### Cleaning your list
Run quarterly:
1. Filter by `status = BOUNCED` → export → delete.
2. Filter by `status = COMPLAINED` → delete.
3. Filter by `total_opens = 0` AND `subscribed_at < 6 months ago` → consider
   a re-engagement campaign before removing.

### Engagement score
Shown in the list view as `Low / Medium / High`:
- **Low** — no opens or clicks ever
- **Medium** — 1–4 engagement points
- **High** — 5+ points (1 open = 1 pt, 1 click = 2 pts)

---

## 8. Duplicating a Campaign

Select a sent or draft campaign → action **"📋 Duplicate"**.
A copy is created with status `DRAFT` and all stats zeroed out.
All list assignments are preserved. Useful for recurring campaigns
(monthly digest, weekly tips) — just update the content and send.

---

## 9. Bounce & Complaint Handling

Bounces and complaints arrive via **webhooks** from your email provider (Gmail
does not send webhooks — for production volume use SendGrid, SES, or Mailgun).

Configure your provider to `POST` to:
```
https://yourdomain.com/newsletter/webhook/<provider>/
```
Where `<provider>` is `sendgrid`, `ses`, `mailgun`, or `postmark`.

Events are stored in **Admin → Newsletter → Webhook Events** and processed
asynchronously by Celery. Hard bounces automatically set subscriber
status to `BOUNCED`.

---

## 10. Email Templates (Reusable Blocks)

**Admin → Newsletter → Email Templates** stores reusable HTML snippets.

1. Create a template with a name, subject, and HTML body.
2. Templates auto-detect `{{ variable }}` placeholders and list them in **Variables**.
3. Reference templates when building campaigns to maintain consistent design.

---

## 11. Deliverability Checklist

Before your first production send, verify:

- [ ] Gmail App Password configured in `.env` (`EMAIL_HOST_PASSWORD`)
- [ ] `DEFAULT_FROM_EMAIL` matches the Gmail address (same domain = better)
- [ ] SPF record added to your domain DNS (`v=spf1 include:_spf.google.com ~all`)
- [ ] DKIM enabled in Google Workspace admin
- [ ] DMARC record set (`v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com`)
- [ ] `SITE_URL` in settings is your real production URL (used in tracking/unsubscribe links)
- [ ] Unsubscribe link tested and working
- [ ] Test email checked across Gmail, Outlook, and mobile

---

## 12. Management Commands

```bash
# Check Celery workers are alive
python manage.py check_celery

# Dispatch all due scheduled campaigns
python manage.py send_campaigns
```

---

## 13. Settings Reference

In `config/settings/base.py`:

```python
NEWSLETTER_CONFIRM_REDIRECT = '/'      # Where to redirect after email confirmation
DISPOSABLE_EMAIL_DOMAINS = [...]       # Domains blocked from subscribing
SITE_URL = 'https://yourdomain.com'    # Used in tracking/unsubscribe URLs
SITE_NAME = 'AnalyticsMeta'            # Appears in emails
DEFAULT_FROM_EMAIL = 'hello@yourdomain.com'
```
