"""Sending text messages, without committing to a provider.

`JATTA_SMS_BACKEND` picks the transport:

* ``fake`` — nothing leaves the machine. Messages are kept in memory (tests)
  and, when `JATTA_SMS_FAKE_OUTBOX` names a file, appended there so a developer
  can read a code while clicking through the site locally. Refused in
  production, where it would mean drivers never receive their codes.
* ``twilio`` — Twilio's Messaging REST API. Needs the account SID, auth token
  and either a sender number / alphanumeric sender or a Messaging Service SID.
* unset — no transport. Phone sign-in then says it is unavailable rather than
  pretending a code was sent. This is the production default: it fails closed.

Two rules hold for every transport:

* The message body carries a sign-in code, so it is never logged. Neither is the
  destination number. A failure is logged by class and provider status only.
* A send that did not demonstrably succeed raises SmsUnavailable. The caller
  then tells the person to try again later; it never says "we sent a code".
"""
import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from flask import current_app

FAKE = "fake"
TWILIO = "twilio"
BACKENDS = (FAKE, TWILIO)

_outbox_lock = threading.Lock()


class SmsUnavailable(RuntimeError):
    """No message was sent. The text is safe to log: it contains no number or code."""


def _config(key, default=None):
    return current_app.config.get(key, default)


def _is_production(app=None):
    app = app or current_app
    return str(app.config.get("ENV_NAME", "")).lower() in ("production", "prod")


def backend():
    return (_config("SMS_BACKEND") or "").strip().lower()


def configuration_problems(app):
    """Why texts cannot be sent with this configuration. Empty when they can."""
    name = (app.config.get("SMS_BACKEND") or "").strip().lower()
    if not name:
        return ["JATTA_SMS_BACKEND is not set, so no text message can be sent."]
    if name not in BACKENDS:
        return [f"JATTA_SMS_BACKEND={name!r} is not a known transport "
                f"({', '.join(BACKENDS)})."]
    if name == FAKE:
        if _is_production(app):
            return ["JATTA_SMS_BACKEND=fake would never deliver a sign-in code. "
                    "Configure a real SMS provider or leave it unset."]
        return []
    problems = []
    if not app.config.get("TWILIO_ACCOUNT_SID"):
        problems.append("TWILIO_ACCOUNT_SID is missing.")
    if not app.config.get("TWILIO_AUTH_TOKEN"):
        problems.append("TWILIO_AUTH_TOKEN is missing.")
    if not (app.config.get("TWILIO_FROM") or app.config.get("TWILIO_MESSAGING_SERVICE_SID")):
        problems.append("Set TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM (the sender).")
    return problems


def available():
    return not configuration_problems(current_app)


def is_fake():
    return backend() == FAKE and not _is_production()


def send(to_e164, body):
    """Send one text. Returns None on success; raises SmsUnavailable otherwise."""
    problems = configuration_problems(current_app)
    if problems:
        raise SmsUnavailable("SMS is not configured")
    name = backend()
    if name == FAKE:
        _send_fake(to_e164, body)
    elif name == TWILIO:
        _send_twilio(to_e164, body)


# --- fake -------------------------------------------------------------------

def outbox():
    """Messages the fake transport has 'sent' in this process. Tests read this."""
    return current_app.extensions.setdefault("fake_sms_outbox", [])


def _send_fake(to_e164, body):
    record = {"to": to_e164, "body": body,
              "sent_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    outbox().append(record)
    path = _config("SMS_FAKE_OUTBOX")
    if path:
        with _outbox_lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(path, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")


def read_fake_outbox_file(limit=20):
    """Most recent messages written by the fake transport, newest first."""
    path = _config("SMS_FAKE_OUTBOX")
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()[-limit:]
    records = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


# --- Twilio -----------------------------------------------------------------

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _send_twilio(to_e164, body):
    sid = _config("TWILIO_ACCOUNT_SID")
    token = _config("TWILIO_AUTH_TOKEN")
    fields = {"To": to_e164, "Body": body}
    if _config("TWILIO_MESSAGING_SERVICE_SID"):
        fields["MessagingServiceSid"] = _config("TWILIO_MESSAGING_SERVICE_SID")
    else:
        fields["From"] = _config("TWILIO_FROM")

    request = urllib.request.Request(
        TWILIO_URL.format(sid=urllib.parse.quote(sid, safe="")),
        data=urllib.parse.urlencode(fields).encode("ascii"),
        method="POST",
        headers={
            "Authorization": "Basic " + base64.b64encode(
                f"{sid}:{token}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_config("SMS_TIMEOUT", 10)) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as error:
        # Twilio's error body can echo the destination number, so only the
        # status and Twilio's numeric error code are kept.
        code = None
        try:
            code = json.loads(error.read().decode("utf-8", "replace")).get("code")
        except (ValueError, AttributeError):
            pass
        current_app.logger.warning("SMS send failed: HTTP %s, provider code %s",
                                   error.code, code)
        raise SmsUnavailable(f"provider returned {error.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        current_app.logger.warning("SMS send failed: %s", type(error).__name__)
        raise SmsUnavailable("provider unreachable") from None

    if payload.get("status") in ("failed", "undelivered") or not payload.get("sid"):
        current_app.logger.warning("SMS send refused by provider: status %s",
                                   payload.get("status"))
        raise SmsUnavailable("provider did not accept the message")
