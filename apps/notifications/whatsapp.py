"""
WhatsApp alert delivery via Twilio Messaging API.

Configuration (env vars, resolved in settings/base.py):
  TWILIO_ACCOUNT_SID   — Twilio account SID
  TWILIO_AUTH_TOKEN    — Twilio auth token
  TWILIO_WHATSAPP_FROM — sender number, e.g. whatsapp:+14155238886 (sandbox default)

Usage
-----
    from apps.notifications.whatsapp import send_whatsapp_alert
    send_whatsapp_alert(to="+254712345678", message="Alert: Sales exceeded threshold")

Number format
-------------
Recipients must have joined the Twilio sandbox (in dev) or be reachable via
an approved WhatsApp Business number (production).  Numbers are normalised to
E.164 before sending; a leading "whatsapp:" prefix is added automatically.

Errors are logged and swallowed — a Twilio outage must never block the
in-app notification path.
"""
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)


def _normalise(number: str) -> str:
    """
    Ensure the number is in E.164 format, stripping spaces/dashes.
    Raises ValueError if the result looks invalid.
    """
    cleaned = re.sub(r"[\s\-\(\)]", "", number)
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not re.fullmatch(r"\+\d{7,15}", cleaned):
        raise ValueError(f"Invalid phone number: {number!r}")
    return cleaned


def send_whatsapp_alert(to: str, message: str) -> bool:
    """
    Send *message* to *to* via WhatsApp. Returns True on success.

    Silently returns False (with a warning log) when Twilio credentials
    are not configured so development environments never raise.
    """
    sid   = getattr(settings, "TWILIO_ACCOUNT_SID",  "")
    token = getattr(settings, "TWILIO_AUTH_TOKEN",   "")
    from_ = getattr(settings, "TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

    if not sid or not token:
        logger.debug("WhatsApp alert skipped — TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
        return False

    try:
        number = _normalise(to)
    except ValueError as exc:
        logger.warning("WhatsApp: bad number %r — %s", to, exc)
        return False

    recipient = f"whatsapp:{number}"

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client = Client(sid, token)
        msg = client.messages.create(
            from_=from_,
            to=recipient,
            body=message,
        )
        logger.info("WhatsApp sent to %s (sid=%s)", recipient, msg.sid)
        return True

    except ImportError:
        logger.warning("WhatsApp: twilio package not installed — pip install twilio")
    except Exception as exc:  # noqa: BLE001
        logger.warning("WhatsApp send failed to %s: %s", recipient, exc)
    return False


def send_whatsapp_alert_to_workspace(workspace, title: str, body: str) -> None:
    """
    Send a WhatsApp alert to every workspace member who has opted in.
    Safe to call from Celery — errors per-member are swallowed.
    """
    from apps.notifications.models import NotificationPreference
    from apps.workspaces.models import WorkspaceMembership

    members = WorkspaceMembership.objects.filter(
        workspace=workspace
    ).select_related("user")

    message = f"*{title}*\n{body}\n\n_AnalyticsMeta_"

    for membership in members:
        try:
            prefs, _ = NotificationPreference.objects.get_or_create(user=membership.user)
            if prefs.wants_whatsapp_alerts():
                send_whatsapp_alert(to=prefs.whatsapp_number, message=message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WhatsApp alert failed for user %s: %s", membership.user_id, exc
            )
