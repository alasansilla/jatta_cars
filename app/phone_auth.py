"""Proving a phone number belongs to the person typing it.

Entering a number proves nothing. A driver is signed in, or an account created,
only after they type back a code that was texted to that number. Everything here
exists to keep that true under abuse:

* Codes are six random digits, stored only as a keyed hash, valid for a few
  minutes, usable once, and tied to the browser that asked for them.
* Each code allows a handful of guesses. The guess counter is incremented by a
  guarded UPDATE *before* the comparison, so parallel guesses cannot exceed it.
* Asking for a code has a cooldown, and per-number, per-IP and site-wide caps.
  Texts cost money and can be abused to pump traffic to premium numbers, so the
  caps are counted before a message is sent, whether or not it arrives.
* Too many wrong codes from one IP address locks that address out for a while,
  so nobody can spread guesses across many numbers.

Phone numbers and codes are never written to the log.

Proving a number does not approve a driver. A new account starts "pending", and
only staff approval lets a driver take trips.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import update, text
from sqlalchemy.exc import IntegrityError

from . import sms
from .models import AuthEvent, Operator, PhoneCode, db
from .phone import try_normalise
from .settings import current_settings

SIGN_IN = "sign_in"
ADD_PHONE = "add_phone"
PURPOSES = (SIGN_IN, ADD_PHONE)

SENT_TO_NUMBER = "sms_number"
SENT_FROM_IP = "sms_ip"
SENT_ANYWHERE = "sms_all"
WRONG_CODE_FROM_IP = "code_wrong_ip"


class Refused(Exception):
    """The request was refused. `str(error)` is written for the person on the page."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def brand():
    """The site's name as staff have set it, for texts and messages."""
    return (current_settings().get("brand_name") or current_settings().get("company_name")
            or "us").strip()


def _setting(name, default):
    return current_app.config.get(name, default)


def _secret():
    return str(current_app.config["SECRET_KEY"]).encode("utf-8")


def _digest(*parts):
    message = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def subject(kind, value):
    """A keyed hash standing in for a phone number or IP address in counters."""
    return _digest("subject", kind, value)


def session_binding(nonce):
    return _digest("session", nonce)


def _code_digest(phone_e164, purpose, binding, code):
    return _digest("code", phone_e164, purpose, binding, code)


def new_nonce():
    return secrets.token_urlsafe(24)


def clean_code(raw):
    """The six digits out of whatever was typed or pasted, or None.

    People paste the whole message ("Your code is 123 456"), or type the
    code with a space in the middle. Anything that is not exactly one run of six
    digits once spaces and dashes are removed is not a code.
    """
    text = str(raw or "")[:200]
    squashed = re.sub(r"(?<=\d)[\s\-.](?=\d)", "", text)
    runs = re.findall(r"\d+", squashed)
    sixes = [run for run in runs if len(run) == 6]
    if len(sixes) == 1:
        return sixes[0]
    return None


# --- counting ---------------------------------------------------------------

def _count(kind, subject_hash, since):
    return AuthEvent.query.filter(
        AuthEvent.kind == kind,
        AuthEvent.subject_hash == subject_hash,
        AuthEvent.created_at >= since,
    ).count()


def _record(kind, subject_hash, now):
    db.session.add(AuthEvent(kind=kind, subject_hash=subject_hash, created_at=now))


def _prune(now):
    """Keep the counters small. Old rows answer no question anyone asks."""
    AuthEvent.query.filter(AuthEvent.created_at < now - timedelta(days=2)).delete(
        synchronize_session=False)
    PhoneCode.query.filter(PhoneCode.created_at < now - timedelta(days=7)).delete(
        synchronize_session=False)


def _minutes(seconds):
    minutes = max(1, int(round(seconds / 60)))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def resend_wait(phone_e164, purpose, now=None):
    """Seconds before another code may be sent to this number. 0 means now."""
    now = now or datetime.utcnow()
    latest = (PhoneCode.query
              .filter_by(phone_e164=phone_e164, purpose=purpose)
              .order_by(PhoneCode.created_at.desc()).first())
    if latest is None:
        return 0
    ready = latest.created_at + timedelta(seconds=_setting("SMS_RESEND_SECONDS", 60))
    return max(0, int((ready - now).total_seconds() + 0.999))


def ip_locked(ip, now=None):
    now = now or datetime.utcnow()
    limit = _setting("SMS_WRONG_CODES_PER_IP_HOUR", 30)
    return _count(WRONG_CODE_FROM_IP, subject("ip", ip), now - timedelta(hours=1)) >= limit


# --- sending ----------------------------------------------------------------

def message_text(code):
    """The text itself. Plain, short, and says what to do with it.

    The last line is the WebOTP format, which lets a phone's browser offer to
    fill the code in automatically. It is only added when the site's own domain
    is configured, because it must match the address in the browser.
    """
    minutes = max(1, _setting("SMS_CODE_TTL_SECONDS", 600) // 60)
    body = (f"Your {brand()} code is {code}. It works for {minutes} minutes. "
            f"Do not share it.")
    domain = (_setting("SMS_WEBOTP_DOMAIN", "") or "").strip()
    if domain:
        body += f"\n\n@{domain} #{code}"
    return body


def request_code(phone_e164, purpose, nonce, ip, now=None):
    """Create a code for this number and text it. Raises Refused with a reason."""
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    now = now or datetime.utcnow()

    if not sms.available():
        raise Refused("Sign-in by text message isn't working at the moment. "
                      f"Please try again later or contact {brand()}.")

    # Serialize send budgets across workers, not only within a Python process.
    if db.engine.dialect.name == "postgresql":
        db.session.execute(text("SELECT pg_advisory_xact_lock(72419008)"))
    else:
        _record("send_lock", "all", now)
        db.session.flush()

    if ip_locked(ip, now):
        raise Refused("Too many wrong codes were tried from this connection. "
                      "Please wait an hour and try again.", retry_after=3600)

    wait = resend_wait(phone_e164, purpose, now)
    if wait:
        raise Refused(f"Please wait {wait} seconds before asking for another code.",
                      retry_after=wait)

    number = subject("phone", phone_e164)
    address = subject("ip", ip)
    hour_ago, day_ago = now - timedelta(hours=1), now - timedelta(days=1)

    if _count(SENT_TO_NUMBER, number, hour_ago) >= _setting("SMS_MAX_PER_NUMBER_HOUR", 5) \
            or _count(SENT_TO_NUMBER, number, day_ago) >= _setting("SMS_MAX_PER_NUMBER_DAY", 10):
        raise Refused("We've sent a lot of codes to this number. "
                      "Please wait an hour and try again.", retry_after=3600)
    if _count(SENT_FROM_IP, address, hour_ago) >= _setting("SMS_MAX_PER_IP_HOUR", 10):
        raise Refused("Too many codes were asked for from this connection. "
                      "Please wait an hour and try again.", retry_after=3600)
    if _count(SENT_ANYWHERE, "all", day_ago) >= _setting("SMS_MAX_PER_DAY", 300):
        current_app.logger.error("Daily SMS cap reached; sign-in codes paused")
        raise Refused("Sign-in by text message is busy right now. "
                      f"Please try again later or contact {brand()}.")

    _prune(now)

    # Only the newest code for a number works. Older ones close now, so a code
    # read off a lost phone stops working the moment a new one is requested.
    db.session.execute(update(PhoneCode).where(
        PhoneCode.phone_e164 == phone_e164,
        PhoneCode.purpose == purpose,
        PhoneCode.closed_at.is_(None),
    ).values(closed_at=now, outcome="replaced"))

    code = f"{secrets.randbelow(10 ** 6):06d}"
    binding = session_binding(nonce)
    row = PhoneCode(
        phone_e164=phone_e164, purpose=purpose, session_hash=binding,
        code_hash=_code_digest(phone_e164, purpose, binding, code),
        created_at=now,
        expires_at=now + timedelta(seconds=_setting("SMS_CODE_TTL_SECONDS", 600)),
        attempts=0,
    )
    db.session.add(row)
    # Counted before sending: a text that fails still cost an attempt, which is
    # what stops a broken provider being hammered.
    _record(SENT_TO_NUMBER, number, now)
    _record(SENT_FROM_IP, address, now)
    _record(SENT_ANYWHERE, "all", now)
    db.session.commit()

    try:
        sms.send(phone_e164, message_text(code))
    except sms.SmsUnavailable:
        db.session.execute(update(PhoneCode).where(
            PhoneCode.id == row.id, PhoneCode.closed_at.is_(None),
        ).values(closed_at=datetime.utcnow(), outcome="not_sent"))
        db.session.commit()
        raise Refused("We couldn't send the text message. Please check the number "
                      "and try again in a minute.")
    return row


# --- checking ---------------------------------------------------------------

def verify_code(phone_e164, purpose, nonce, raw_code, ip, now=None):
    """True when the code is right. Raises Refused for every other outcome."""
    now = now or datetime.utcnow()
    code = clean_code(raw_code)
    if code is None:
        raise Refused("Enter the 6-digit code from the text message.")

    if ip_locked(ip, now):
        raise Refused("Too many wrong codes were tried from this connection. "
                      "Please wait an hour and try again.", retry_after=3600)

    binding = session_binding(nonce)
    row = (PhoneCode.query
           .filter_by(phone_e164=phone_e164, purpose=purpose, session_hash=binding)
           .filter(PhoneCode.closed_at.is_(None))
           .order_by(PhoneCode.created_at.desc()).first())
    if row is None:
        raise Refused("That code is no longer valid. Send a new code.")

    maximum = _setting("SMS_CODE_MAX_ATTEMPTS", 5)
    counted = db.session.execute(update(PhoneCode).where(
        PhoneCode.id == row.id,
        PhoneCode.closed_at.is_(None),
        PhoneCode.expires_at > now,
        PhoneCode.attempts < maximum,
    ).values(attempts=PhoneCode.attempts + 1))
    if counted.rowcount != 1:
        db.session.refresh(row)
        used_up = row.attempts >= maximum
        db.session.execute(update(PhoneCode).where(
            PhoneCode.id == row.id, PhoneCode.closed_at.is_(None),
        ).values(closed_at=now, outcome="locked" if used_up else "expired"))
        db.session.commit()
        if used_up:
            raise Refused("There are no tries left for that code. Send a new code.")
        raise Refused("That code has expired. Send a new code.")
    db.session.commit()

    if hmac.compare_digest(_code_digest(phone_e164, purpose, binding, code), row.code_hash):
        used = db.session.execute(update(PhoneCode).where(
            PhoneCode.id == row.id, PhoneCode.closed_at.is_(None),
        ).values(closed_at=now, outcome="used"))
        db.session.commit()
        if used.rowcount != 1:
            raise Refused("That code has already been used. Send a new code.")
        return True

    _record(WRONG_CODE_FROM_IP, subject("ip", ip), now)
    db.session.refresh(row)
    left = maximum - row.attempts
    if left <= 0:
        db.session.execute(update(PhoneCode).where(
            PhoneCode.id == row.id, PhoneCode.closed_at.is_(None),
        ).values(closed_at=now, outcome="locked"))
        db.session.commit()
        raise Refused("That code isn't right, and there are no tries left. "
                      "Send a new code.")
    db.session.commit()
    raise Refused(f"That code isn't right. You have {left} "
                  f"{'try' if left == 1 else 'tries'} left.")


# --- accounts ---------------------------------------------------------------

def driver_for_phone(phone_e164):
    return Operator.query.filter_by(phone_e164=phone_e164).first()


def unverified_claims(phone_e164):
    """Older accounts listing this number as a contact, never verified.

    Nobody proved those numbers, so they are not a way into those accounts. But
    creating a second account for the same person would split their history, so
    registration stops and explains how to link the number instead.
    """
    rows = Operator.query.filter(Operator.phone_e164.is_(None),
                                 Operator.phone.isnot(None)).all()
    return [row for row in rows if try_normalise(row.phone) == phone_e164]


def find_or_create_driver(phone_e164, name, now=None):
    """The driver account for a verified number: (operator, created).

    If the number already belongs to an account, that account is returned and
    nothing is created — a second tab, or a person joining twice, signs into the
    first account. The unique index decides a race: the loser's INSERT fails and
    it returns the winner's account.
    """
    now = now or datetime.utcnow()
    existing = driver_for_phone(phone_e164)
    if existing is not None:
        return existing, False

    for attempt in range(4):
        base = name if attempt == 0 else f"{name} {secrets.token_hex(2)}"
        driver = Operator(
            name=name, contact_name=name, slug=Operator.make_slug(base),
            phone_e164=phone_e164, phone_verified_at=now,
            status="pending", email=None,
        )
        db.session.add(driver)
        try:
            db.session.commit()
            return driver, True
        except IntegrityError:
            db.session.rollback()
            existing = driver_for_phone(phone_e164)
            if existing is not None:
                return existing, False
            # Otherwise the slug collided with a simultaneous join; try another.
    raise RuntimeError("could not create the driver account")


def attach_phone(operator_id, phone_e164, now=None):
    """Put a verified number on an existing account.

    Returns "attached", "unchanged" or "taken". "taken" means another account
    already holds the number; nothing is moved, merged or overwritten.
    """
    now = now or datetime.utcnow()
    holder = driver_for_phone(phone_e164)
    if holder is not None:
        return "unchanged" if holder.id == operator_id else "taken"
    try:
        changed = db.session.execute(update(Operator).where(Operator.id == operator_id)
                                     .values(phone_e164=phone_e164, phone_verified_at=now))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        holder = driver_for_phone(phone_e164)
        return "unchanged" if holder is not None and holder.id == operator_id else "taken"
    return "attached" if changed.rowcount == 1 else "taken"
