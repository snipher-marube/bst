"""
Deliver alert and report notifications to Slack / Teams webhooks.

Usage
-----
Call ``send_chat_alert(workspace, title, message)`` after an alert fires.
It looks up active ChatIntegration rows for the workspace and POSTs once
per configured provider.  Network errors are logged and swallowed so a
Slack outage never breaks the alert pipeline.
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _slack_payload(title: str, message: str, level: str = "warning") -> bytes:
    colour = {"warning": "#f59e0b", "error": "#ef4444", "info": "#3b82f6"}.get(level, "#f59e0b")
    payload = {
        "attachments": [
            {
                "color": colour,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{title}*\n{message}",
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"AnalyticsMeta · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                            }
                        ],
                    },
                ],
            }
        ]
    }
    return json.dumps(payload).encode()


def _teams_payload(title: str, message: str, level: str = "warning") -> bytes:
    colour = {"warning": "warning", "error": "attention", "info": "accent"}.get(level, "warning")
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": title,
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": colour,
                        },
                        {
                            "type": "TextBlock",
                            "text": message,
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"AnalyticsMeta · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                            "size": "Small",
                            "isSubtle": True,
                        },
                    ],
                },
            }
        ],
    }
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# Low-level POST
# ---------------------------------------------------------------------------

def _post_webhook(url: str, body: bytes, timeout: int = 5) -> bool:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as exc:
        logger.warning("Chat webhook HTTP error %s: %s", exc.code, exc.reason)
    except urllib.error.URLError as exc:
        logger.warning("Chat webhook network error: %s", exc.reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chat webhook unexpected error: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_chat_alert(workspace, title: str, message: str, level: str = "warning") -> None:
    """
    Post a notification to every active ChatIntegration for *workspace*.

    Safe to call from Celery tasks — network errors are swallowed.
    """
    from .models import ChatIntegration

    integrations = ChatIntegration.objects.filter(workspace=workspace, is_enabled=True)
    for ci in integrations:
        try:
            url = ci.get_webhook_url()
        except Exception:
            logger.warning("Could not decrypt webhook URL for ChatIntegration %s", ci.id)
            continue

        if ci.provider == ChatIntegration.PROVIDER_SLACK:
            body = _slack_payload(title, message, level)
        else:
            body = _teams_payload(title, message, level)

        ok = _post_webhook(url, body)
        if ok:
            from django.utils import timezone as dj_tz
            ChatIntegration.objects.filter(pk=ci.pk).update(last_used=dj_tz.now())
            logger.info("Chat alert sent via %s for workspace %s", ci.provider, workspace.id)
        else:
            logger.warning("Chat alert failed via %s for workspace %s", ci.provider, workspace.id)


def test_webhook(url: str, provider: str) -> bool:
    """Send a test message to *url*. Returns True on success."""
    title = "AnalyticsMeta — Connection Test"
    message = "This is a test notification from AnalyticsMeta. Your integration is working correctly."
    if provider == "slack":
        body = _slack_payload(title, message, "info")
    else:
        body = _teams_payload(title, message, "info")
    return _post_webhook(url, body)
