"""Adversarial tests for driver sign-in by phone number.

Written from the requirements, not from the implementation: one number is one
account however it is typed; typing a number proves nothing; codes are short
lived, single use, attempt-capped, replaced by newer ones and tied to the
browser that asked; sending is rate limited; nothing sensitive reaches the logs
or the database in the clear; proving a number never approves a driver;
production fails closed; and staff can only ever remove a number.

Nothing here talks to a real SMS or routing provider: the fake transport keeps
messages in memory, and the one Twilio path exercised has urlopen patched.
"""
import hashlib
import io
import json
import logging
import re
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app, phone_auth, sms
from app.models import (
    AdminUser, AuthEvent, DriverState, Operator, OperatorFare, PhoneCode, Vehicle, db,
)
from app.phone import normalise
from tests.test_marketplace import TestConfig

NUMBER = "+220877701234"
OTHER = "+220877701235"
THIRD = "+220877701236"
FOURTH = "+220877701237"

SPELLINGS = ["770 1234", "+220 770-1234", "00220 7701234", "2207701234",
             "(220) 770 1234", "07701234", "0770 1234", "+220877701234", "770-12-34"]


class SecurityConfig(TestConfig):
    ENV_NAME = "development"
    SMS_BACKEND = "fake"
    SMS_FAKE_OUTBOX = ""
    SMS_WEBOTP_DOMAIN = ""
    SMS_CODE_TTL_SECONDS = 600
    SMS_CODE_MAX_ATTEMPTS = 5
    SMS_RESEND_SECONDS = 60
    SMS_MAX_PER_NUMBER_HOUR = 5
    SMS_MAX_PER_NUMBER_DAY = 10
    SMS_MAX_PER_IP_HOUR = 10
    SMS_MAX_PER_DAY = 300
    SMS_WRONG_CODES_PER_IP_HOUR = 30
    PHONE_VERIFIED_SECONDS = 900
    RECENT_SIGN_IN_SECONDS = 900
    TRUSTED_PROXIES = None
    TWILIO_ACCOUNT_SID = ""
    TWILIO_AUTH_TOKEN = ""
    TWILIO_FROM = ""
    TWILIO_MESSAGING_SERVICE_SID = ""
    SESSION_COOKIE_SECURE = False


class PhoneSecurityCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SecurityConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.new_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # --- helpers ---------------------------------------------------------------

    def new_client(self, ip="198.51.100.7"):
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = ip
        return client

    def token(self, client):
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
        if not token:
            client.get("/driver/join")
            with client.session_transaction() as session:
                token = session.get("_csrf_token")
        if not token:
            with client.session_transaction() as session:
                session["_csrf_token"] = token = "test-csrf-token-value"
        return token

    def post(self, client, path, **data):
        data.setdefault("csrf_token", self.token(client))
        return client.post(path, data=data)

    def send(self, client, number, intent="join", country_code="+220"):
        return self.post(client, "/driver/send-code", country_code=country_code,
                         phone=number, intent=intent)

    def codes_to(self, e164):
        return [re.search(r"\b\d{6}\b", message["body"]).group()
                for message in sms.outbox() if message["to"] == e164]

    def last_code(self, e164):
        codes = self.codes_to(e164)
        self.assertTrue(codes, "no code was texted to that number")
        return codes[-1]

    @staticmethod
    def wrong(code):
        return "000000" if code != "000000" else "111111"

    def session_of(self, client):
        with client.session_transaction() as session:
            return dict(session), session.permanent

    def signed_in_id(self, client):
        return self.session_of(client)[0].get("operator_id")

    def driver(self, phone_e164=NUMBER, status="approved", name="Lamin Jallow", **extra):
        operator = Operator(name=name, slug=Operator.make_slug(name + " " + str(time.time())),
                            status=status, phone_e164=phone_e164,
                            phone_verified_at=datetime.utcnow() if phone_e164 else None,
                            **extra)
        db.session.add(operator)
        db.session.commit()
        return operator

    def relax_limits(self):
        self.app.config.update(SMS_RESEND_SECONDS=0, SMS_MAX_PER_NUMBER_HOUR=1000,
                               SMS_MAX_PER_NUMBER_DAY=1000, SMS_MAX_PER_IP_HOUR=1000,
                               SMS_MAX_PER_DAY=1000)

    def join(self, client, number="770 1234", name="Lamin Jallow"):
        response = self.send(client, number, intent="join")
        self.assertEqual(response.status_code, 302, response.data[:500])
        e164 = normalise("+220", number)
        response = self.post(client, "/driver/code", code=self.last_code(e164))
        self.assertEqual(response.status_code, 302)
        response = self.post(client, "/driver/name", name=name)
        self.assertEqual(response.status_code, 302)
        return Operator.query.filter_by(phone_e164=e164).one()

    def sign_in(self, client, number, intent="sign_in"):
        response = self.send(client, number, intent=intent)
        self.assertEqual(response.status_code, 302, response.data[:500])
        e164 = normalise("+220", number)
        return self.post(client, "/driver/code", code=self.last_code(e164))


# --- one number, one account ------------------------------------------------------

class SameNumberSameAccountTests(PhoneSecurityCase):
    def test_every_spelling_normalises_to_one_e164(self):
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(normalise("+220", spelling), NUMBER)
        for country_code in ("+220", "220", " +220 "):
            with self.subTest(country_code=country_code):
                self.assertEqual(normalise(country_code, "770 1234"), NUMBER)
        # An explicit international prefix wins over the country box.
        self.assertEqual(normalise("+44", "+220 770 1234"), NUMBER)
        self.assertEqual(normalise("+44", "00220 770 1234"), NUMBER)

    def test_joining_then_every_spelling_signs_into_the_same_account(self):
        self.relax_limits()
        first = self.join(self.client, "770 1234")
        for index, spelling in enumerate(SPELLINGS):
            with self.subTest(spelling=spelling):
                client = self.new_client(ip=f"198.51.100.{20 + index}")
                intent = "join" if index % 2 else "sign_in"
                response = self.sign_in(client, spelling, intent=intent)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/driver/name", response.location)
                self.assertEqual(self.signed_in_id(client), first.id)
                self.assertEqual(Operator.query.count(), 1)

    def test_existing_verified_account_is_signed_into_not_duplicated(self):
        existing = self.driver(NUMBER, status="approved")
        response = self.sign_in(self.client, "(220) 770 1234", intent="join")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.signed_in_id(self.client), existing.id)
        self.assertEqual(Operator.query.count(), 1)

    def test_two_browsers_verifying_different_spellings_end_on_one_account(self):
        self.relax_limits()
        first = self.new_client(ip="198.51.100.30")
        second = self.new_client(ip="198.51.100.31")
        self.assertEqual(self.send(first, "770 1234").status_code, 302)
        self.post(first, "/driver/code", code=self.last_code(NUMBER))
        self.assertEqual(self.send(second, "00220 770-1234").status_code, 302)
        self.post(second, "/driver/code", code=self.last_code(NUMBER))
        self.assertEqual(Operator.query.count(), 0)
        self.post(first, "/driver/name", name="First Person")
        self.post(second, "/driver/name", name="Second Person")
        self.assertEqual(Operator.query.count(), 1)
        account = Operator.query.one()
        self.assertEqual(self.signed_in_id(first), account.id)
        self.assertEqual(self.signed_in_id(second), account.id)


# --- a number alone proves nothing ----------------------------------------------------

class NumberAloneTests(PhoneSecurityCase):
    def test_sending_a_code_creates_nothing_and_signs_nobody_in(self):
        for intent, number in (("join", "770 1234"), ("sign_in", "770 1235")):
            client = self.new_client()
            self.assertEqual(self.send(client, number, intent=intent).status_code, 302)
            self.assertIsNone(self.signed_in_id(client))
            self.assertEqual(Operator.query.count(), 0)
            # Skipping the code step gets nowhere either.
            self.assertIn("/driver/join", client.get("/driver/name").location)
            response = self.post(client, "/driver/name", name="Pretend Driver")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/driver/join", response.location)
            self.assertIn("/driver/sign-in", client.get("/driver/status").location)
            self.assertIn("/driver/sign-in", client.get("/operator/").location)
            self.assertIsNone(self.signed_in_id(client))
        self.assertEqual(Operator.query.count(), 0)

    def test_number_of_an_existing_driver_alone_does_not_sign_in(self):
        self.driver(NUMBER, status="approved")
        self.assertEqual(self.send(self.client, "770 1234", intent="sign_in").status_code, 302)
        self.assertIsNone(self.signed_in_id(self.client))
        self.assertIn("/driver/sign-in", self.client.get("/operator/").location)
        for bogus in ("", "abc", "12345", "1234567"):
            response = self.post(self.client, "/driver/code", code=bogus)
            self.assertEqual(response.status_code, 400)
            self.assertIsNone(self.signed_in_id(self.client))
        self.assertEqual(Operator.query.count(), 1)


# --- the code itself ----------------------------------------------------------------------

class CodeLifecycleTests(PhoneSecurityCase):
    def test_expired_code_is_refused(self):
        self.send(self.client, "770 1234")
        code = self.last_code(NUMBER)
        row = PhoneCode.query.one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        response = self.post(self.client, "/driver/code", code=code)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.signed_in_id(self.client))
        self.assertIn("/driver/join", self.client.get("/driver/name").location)
        self.assertEqual(Operator.query.count(), 0)
        db.session.expire_all()
        self.assertIsNotNone(PhoneCode.query.one().closed_at)

    def test_code_cannot_be_replayed(self):
        driver = self.driver(NUMBER)
        self.send(self.client, "770 1234", intent="sign_in")
        code = self.last_code(NUMBER)
        flow = self.session_of(self.client)[0]["phone_flow"]
        self.post(self.client, "/driver/code", code=code)
        self.assertEqual(self.signed_in_id(self.client), driver.id)

        # Same browser, after signing out, puts the old flow back and replays.
        self.client.get("/driver/sign-out")
        with self.client.session_transaction() as session:
            session["phone_flow"] = flow
        response = self.post(self.client, "/driver/code", code=code)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.signed_in_id(self.client))

        with self.assertRaises(phone_auth.Refused):
            phone_auth.verify_code(flow["phone"], flow["purpose"], flow["nonce"], code,
                                   "198.51.100.7")

    def test_attempts_are_bounded_even_for_the_right_code(self):
        self.app.config["SMS_CODE_MAX_ATTEMPTS"] = 3
        self.driver(NUMBER)
        self.send(self.client, "770 1234", intent="sign_in")
        code = self.last_code(NUMBER)
        for _ in range(3):
            self.assertEqual(self.post(self.client, "/driver/code",
                                       code=self.wrong(code)).status_code, 400)
        response = self.post(self.client, "/driver/code", code=code)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.signed_in_id(self.client))
        db.session.expire_all()
        row = PhoneCode.query.one()
        self.assertLessEqual(row.attempts, 3)
        self.assertIsNotNone(row.closed_at)

    def test_resent_code_replaces_the_older_one(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        driver = self.driver(NUMBER)
        with patch("app.phone_auth.secrets.randbelow", side_effect=[111111, 222222]):
            self.send(self.client, "770 1234", intent="sign_in")
            response = self.post(self.client, "/driver/code/resend")
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.codes_to(NUMBER), ["111111", "222222"])
        self.assertEqual(self.post(self.client, "/driver/code", code="111111").status_code, 400)
        self.assertIsNone(self.signed_in_id(self.client))
        self.post(self.client, "/driver/code", code="222222")
        self.assertEqual(self.signed_in_id(self.client), driver.id)

    def test_new_send_replaces_the_older_code(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        with patch("app.phone_auth.secrets.randbelow", side_effect=[333333, 444444]):
            self.send(self.client, "770 1234")
            self.send(self.client, "+220 770 1234")
        self.assertEqual(self.post(self.client, "/driver/code", code="333333").status_code, 400)
        self.assertIn("/driver/join", self.client.get("/driver/name").location)
        older = PhoneCode.query.order_by(PhoneCode.id).first()
        self.assertEqual(older.outcome, "replaced")


class SessionBindingTests(PhoneSecurityCase):
    def test_code_from_browser_a_is_useless_in_browser_b(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        self.driver(NUMBER)
        browser_a = self.new_client(ip="198.51.100.40")
        browser_b = self.new_client(ip="198.51.100.41")
        with patch("app.phone_auth.secrets.randbelow", side_effect=[555555, 666666]):
            self.assertEqual(self.send(browser_a, "770 1234", "sign_in").status_code, 302)
            self.assertEqual(self.send(browser_b, "7701234", "sign_in").status_code, 302)
        response = self.post(browser_b, "/driver/code", code="555555")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.signed_in_id(browser_b))

    def test_foreign_nonce_cannot_use_the_code(self):
        driver = self.driver(NUMBER)
        self.send(self.client, "770 1234", intent="sign_in")
        code = self.last_code(NUMBER)
        with self.assertRaises(phone_auth.Refused):
            phone_auth.verify_code(NUMBER, phone_auth.SIGN_IN, phone_auth.new_nonce(), code,
                                   "198.51.100.99")
        # Browser B with no flow of its own gets nowhere.
        browser_b = self.new_client(ip="198.51.100.42")
        self.assertEqual(self.send(browser_b, "770 1234", "sign_in").status_code, 429)
        response = self.post(browser_b, "/driver/code", code=code)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.signed_in_id(browser_b))
        # The owner's code still works in the owner's browser.
        self.post(self.client, "/driver/code", code=code)
        self.assertEqual(self.signed_in_id(self.client), driver.id)


# --- rate limits ------------------------------------------------------------------------------

class RateLimitTests(PhoneSecurityCase):
    def test_resend_cooldown_is_429_and_sends_nothing(self):
        self.assertEqual(self.send(self.client, "770 1234").status_code, 302)
        self.assertEqual(len(sms.outbox()), 1)
        self.assertEqual(self.send(self.client, "770 1234").status_code, 429)
        self.assertEqual(self.send(self.client, "+220 770-1234").status_code, 429)
        self.assertEqual(self.post(self.client, "/driver/code/resend").status_code, 429)
        other = self.new_client(ip="203.0.113.50")
        self.assertEqual(self.send(other, "00220 7701234").status_code, 429)
        self.assertEqual(len(sms.outbox()), 1)
        self.assertEqual(PhoneCode.query.count(), 1)

    def test_per_number_hourly_cap(self):
        self.app.config.update(SMS_RESEND_SECONDS=0, SMS_MAX_PER_NUMBER_HOUR=3)
        for index in range(3):
            client = self.new_client(ip=f"203.0.113.{index + 1}")
            self.assertEqual(self.send(client, "770 1234").status_code, 302)
        client = self.new_client(ip="203.0.113.9")
        self.assertEqual(self.send(client, "770 1234").status_code, 429)
        self.assertEqual(len(self.codes_to(NUMBER)), 3)
        self.assertEqual(PhoneCode.query.count(), 3)

    def test_per_ip_hourly_cap(self):
        self.app.config.update(SMS_RESEND_SECONDS=0, SMS_MAX_PER_IP_HOUR=2)
        for number in ("770 1234", "770 1235"):
            client = self.new_client(ip="203.0.113.77")
            self.assertEqual(self.send(client, number).status_code, 302)
        client = self.new_client(ip="203.0.113.77")
        self.assertEqual(self.send(client, "770 1236").status_code, 429)
        self.assertEqual(self.codes_to(THIRD), [])
        elsewhere = self.new_client(ip="203.0.113.78")
        self.assertEqual(self.send(elsewhere, "770 1236").status_code, 302)
        self.assertEqual(len(self.codes_to(THIRD)), 1)

    def test_site_wide_daily_cap(self):
        self.app.config.update(SMS_RESEND_SECONDS=0, SMS_MAX_PER_DAY=2)
        for index, number in enumerate(("770 1234", "770 1235")):
            client = self.new_client(ip=f"203.0.113.{100 + index}")
            self.assertEqual(self.send(client, number).status_code, 302)
        client = self.new_client(ip="203.0.113.150")
        response = self.send(client, "770 1236")
        self.assertIn(response.status_code, (400, 429))
        self.assertEqual(len(sms.outbox()), 2)
        self.assertEqual(PhoneCode.query.count(), 2)
        self.assertNotIn("phone_flow", self.session_of(client)[0])

    def test_wrong_code_ip_lock_blocks_verify_and_send(self):
        self.app.config.update(SMS_WRONG_CODES_PER_IP_HOUR=3, SMS_CODE_MAX_ATTEMPTS=10)
        self.driver(NUMBER)
        ip = "203.0.113.200"
        clients = {}
        # Spread the guesses across numbers: the lock is per address.
        for number, e164 in (("770 1234", NUMBER), ("770 1235", OTHER), ("770 1236", THIRD)):
            clients[e164] = client = self.new_client(ip=ip)
            self.assertEqual(self.send(client, number, "sign_in").status_code, 302)
            response = self.post(client, "/driver/code", code=self.wrong(self.last_code(e164)))
            self.assertEqual(response.status_code, 400)
        response = self.post(clients[NUMBER], "/driver/code", code=self.last_code(NUMBER))
        self.assertIn(response.status_code, (400, 429))
        self.assertIsNone(self.signed_in_id(clients[NUMBER]))

        sent = len(sms.outbox())
        locked = self.new_client(ip=ip)
        self.assertEqual(self.send(locked, "770 1237").status_code, 429)
        self.assertEqual(len(sms.outbox()), sent)
        self.assertEqual(self.codes_to(FOURTH), [])

        elsewhere = self.new_client(ip="203.0.113.201")
        self.assertEqual(self.send(elsewhere, "770 1237").status_code, 302)


# --- nothing sensitive in logs or tables ---------------------------------------------------------

class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        self.lines.append(f"{record.name}: {text} {record.args!r}")


class SecretsStayOutOfLogsTests(PhoneSecurityCase):
    def test_codes_and_numbers_never_logged(self):
        handler = _Collect()
        root = logging.getLogger()
        app_logger = logging.getLogger("app")
        # SQLAlchemy's own statement echo inherits the root level; keep it at its
        # normal quiet level so this test is about what the application logs.
        sqla = logging.getLogger("sqlalchemy")
        saved = (root.level, app_logger.level, sqla.level)
        root.addHandler(handler)
        app_logger.addHandler(handler)
        root.setLevel(logging.DEBUG)
        app_logger.setLevel(logging.DEBUG)
        sqla.setLevel(logging.WARNING)
        failed_code = "482913"
        try:
            self.app.config["SMS_RESEND_SECONDS"] = 0
            client = self.client
            client.get("/driver/join")
            self.send(client, "770 1234")
            client.get("/driver/code")
            self.post(client, "/driver/code", code=self.wrong(self.last_code(NUMBER)))
            self.post(client, "/driver/code/resend")
            self.post(client, "/driver/code", code=self.last_code(NUMBER))
            client.get("/driver/name")
            self.post(client, "/driver/name", name="Lamin Jallow")
            client.get("/driver/status")
            client.get("/operator/")
            client.get("/driver/sign-out")
            self.sign_in(self.new_client(ip="198.51.100.60"), "+220 770 1234")
            self.send(self.new_client(ip="198.51.100.61"), "12")  # invalid input

            # A failed send through a provider whose error echoes the number.
            self.app.config.update(SMS_BACKEND="twilio", TWILIO_ACCOUNT_SID="AC" + "0" * 32,
                                   TWILIO_AUTH_TOKEN="t" * 32, TWILIO_FROM="+15005550006")
            body = json.dumps({"code": 21211,
                               "message": f"The 'To' number {OTHER} is not valid "
                                          f"(code {failed_code})"}).encode()
            error = urllib.error.HTTPError("https://api.twilio.com/x", 400, "Bad Request",
                                           {}, io.BytesIO(body))
            with patch("urllib.request.urlopen", side_effect=error), \
                    patch("app.phone_auth.secrets.randbelow", return_value=int(failed_code)):
                response = self.send(self.new_client(ip="198.51.100.62"), "770 1235")
            self.assertEqual(response.status_code, 400)
            with patch("urllib.request.urlopen",
                       side_effect=urllib.error.URLError(f"no route to {THIRD}")):
                self.send(self.new_client(ip="198.51.100.63"), "770 1236")

            # And the daily cap, which logs an error.
            self.app.config.update(SMS_BACKEND="fake", SMS_MAX_PER_DAY=0)
            self.send(self.new_client(ip="198.51.100.64"), "770 1237")
        finally:
            root.removeHandler(handler)
            app_logger.removeHandler(handler)
            root.setLevel(saved[0])
            app_logger.setLevel(saved[1])
            sqla.setLevel(saved[2])

        logged = "\n".join(handler.lines)
        # Prove the capture works: the failure and cap paths do log something.
        self.assertIn("SMS send failed", logged)
        self.assertIn("Daily SMS cap", logged)
        forbidden = set(self.codes_to(NUMBER)) | {failed_code}
        for e164 in (NUMBER, OTHER, THIRD, FOURTH):
            digits = e164[4:]
            forbidden |= {e164, e164[1:], digits, f"{digits[:3]} {digits[3:]}",
                          f"{digits[:3]}-{digits[3:]}"}
        forbidden |= {"770 1234", "+220 770 1234"}
        self.assertTrue(forbidden)
        for secret in sorted(forbidden):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)


class StoredSecretsTests(PhoneSecurityCase):
    def test_code_hash_is_not_the_code_and_auth_events_hold_no_raw_identifiers(self):
        ip = "203.0.113.45"
        client = self.new_client(ip=ip)
        with patch("app.phone_auth.secrets.randbelow", return_value=581204):
            self.send(client, "770 1234")
        code = self.last_code(NUMBER)
        self.assertEqual(code, "581204")
        nonce = self.session_of(client)[0]["phone_flow"]["nonce"]
        self.post(client, "/driver/code", code=self.wrong(code))

        row = PhoneCode.query.one()
        self.assertNotIn(code, row.code_hash)
        self.assertNotEqual(row.code_hash, hashlib.sha256(code.encode()).hexdigest())
        self.assertNotIn("7701234", row.code_hash)
        self.assertNotIn(nonce, row.session_hash)
        self.assertNotEqual(row.session_hash, hashlib.sha256(nonce.encode()).hexdigest())

        events = AuthEvent.query.all()
        self.assertTrue(events)
        raw = [NUMBER, NUMBER[1:], "7701234", ip, ip.replace(".", "")]
        for event in events:
            stored = f"{event.kind}|{event.subject_hash}"
            for value in raw:
                self.assertNotIn(value, stored)
            self.assertNotEqual(event.subject_hash, hashlib.sha256(NUMBER.encode()).hexdigest())
            self.assertNotEqual(event.subject_hash, hashlib.sha256(ip.encode()).hexdigest())


# --- verification is not approval ---------------------------------------------------------------

class VerificationIsNotApprovalTests(PhoneSecurityCase):
    def test_new_driver_is_pending_and_cannot_drive(self):
        driver = self.join(self.client)
        self.assertEqual(driver.status, "pending")
        self.assertFalse(driver.is_approved)
        self.assertIsNotNone(driver.phone_verified_at)
        self.assertEqual(self.signed_in_id(self.client), driver.id)
        self.assertEqual(self.client.get("/driver/status").status_code, 200)
        self.assertIn("/driver/status", self.client.get("/operator/").location)

        vehicle = Vehicle(make="Toyota", model="Vitz", year=2019, daily_rate=1000,
                          deposit=0, is_active=True, operator_id=driver.id, seats=4)
        db.session.add(vehicle)
        db.session.add(OperatorFare(operator_id=driver.id, kind="ride", title="Rides",
                                    from_location="Anywhere", to_location="Anywhere",
                                    pricing_model="distance", base_price=100, per_km=20,
                                    is_active=True))
        db.session.commit()

        for path in ("/operator/drive", "/operator/drive/state"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/driver/status", response.location)
        token = self.token(self.client)
        posts = {
            "/operator/drive/location": {"lat": 13.45, "lng": -16.68, "vehicle_id": vehicle.id},
            "/operator/drive/offline": {},
            "/operator/drive/accept": {"booking_id": 1},
            "/operator/drive/decline": {"booking_id": 1},
            "/operator/drive/status": {"booking_id": 1, "status": "arriving"},
        }
        for path, body in posts.items():
            response = self.client.post(path, json=body, headers={"X-CSRF-Token": token})
            self.assertNotEqual(response.status_code, 200, path)
            self.assertIn(response.status_code, (302, 401, 403), path)
        self.assertEqual(DriverState.query.count(), 0)
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, driver.id).status, "pending")

    def test_existing_rejected_account_signing_in_by_phone_stays_rejected(self):
        driver = self.driver(NUMBER, status="rejected")
        self.sign_in(self.client, "770 1234")
        self.assertEqual(self.signed_in_id(self.client), driver.id)
        self.assertIn("/driver/status", self.client.get("/operator/").location)
        self.assertIn("/driver/status", self.client.get("/operator/drive").location)
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, driver.id).status, "rejected")


# --- production fails closed ----------------------------------------------------------------------

class ProductionConfig(SecurityConfig):
    ENV_NAME = "production"
    SECRET_KEY = "prod-test-" + "k" * 48
    SQLALCHEMY_DATABASE_URI = "postgresql+psycopg://user:pass@127.0.0.1:1/jatta"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    STORAGE_BACKEND = "supabase"
    SUPABASE_URL = "https://example.supabase.co"
    SUPABASE_SERVICE_ROLE_KEY = "service-role-key-for-tests"
    TESTING = True


class ProductionFailsClosedTests(unittest.TestCase):
    def test_fake_sms_in_production_refuses_to_start(self):
        class FakeInProduction(ProductionConfig):
            SMS_BACKEND = "fake"
        with self.assertRaises(RuntimeError):
            create_app(FakeInProduction)

    def test_valid_production_config_without_sms_starts(self):
        class NoSmsInProduction(ProductionConfig):
            SMS_BACKEND = ""
        app = create_app(NoSmsInProduction)  # must not raise
        self.assertIsNotNone(app)

    def test_unset_sms_in_production_sends_nothing_and_says_unavailable(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        class NoSmsInProduction(ProductionConfig):
            SMS_BACKEND = ""

        app = create_app(NoSmsInProduction)
        # The config is a real production one; only the engine is swapped for an
        # in-memory database so no network connection is attempted.
        engine = create_engine("sqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        with app.app_context():
            original = db._app_engines[app][None]
            db._app_engines[app][None] = engine
            original.dispose()
            try:
                db.create_all()
                client = app.test_client()
                page = client.get("/driver/join")
                self.assertEqual(page.status_code, 200)
                self.assertRegex(page.get_data(as_text=True), r"isn(&#39;|')t switched on")
                with client.session_transaction() as session:
                    token = session["_csrf_token"]
                response = client.post("/driver/send-code", data={
                    "csrf_token": token, "country_code": "+220", "phone": "770 1234",
                    "intent": "join"})
                self.assertNotEqual(response.status_code, 302)
                self.assertRegex(response.get_data(as_text=True),
                                 r"isn(&#39;|')t working|switched on")
                self.assertEqual(PhoneCode.query.count(), 0)
                self.assertEqual(Operator.query.count(), 0)
                self.assertEqual(sms.outbox(), [])
                with client.session_transaction() as session:
                    self.assertNotIn("phone_flow", session)
            finally:
                db.session.remove()
                engine.dispose()


# --- CSRF -------------------------------------------------------------------------------------------

class CsrfTests(PhoneSecurityCase):
    def test_send_code_without_or_with_wrong_token_does_nothing(self):
        self.client.get("/driver/join")
        for data in ({}, {"csrf_token": ""}, {"csrf_token": "forged"}):
            data.update(country_code="+220", phone="770 1234", intent="join")
            response = self.client.post("/driver/send-code", data=data)
            self.assertEqual(response.status_code, 302)
            self.assertNotIn("/driver/code", response.location)
        self.assertEqual(sms.outbox(), [])
        self.assertEqual(PhoneCode.query.count(), 0)
        self.assertEqual(AuthEvent.query.count(), 0)
        self.assertNotIn("phone_flow", self.session_of(self.client)[0])

    def test_code_name_and_resend_without_token_do_nothing(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        self.send(self.client, "770 1234")
        code = self.last_code(NUMBER)

        self.client.post("/driver/code", data={"code": code})
        self.assertIsNone(self.signed_in_id(self.client))
        db.session.expire_all()
        row = PhoneCode.query.one()
        self.assertEqual(row.attempts, 0)
        self.assertIsNone(row.closed_at)

        self.client.post("/driver/code/resend", data={})
        self.assertEqual(len(sms.outbox()), 1)

        self.post(self.client, "/driver/code", code=code)
        self.client.post("/driver/name", data={"name": "No Token"})
        self.assertEqual(Operator.query.count(), 0)
        self.assertIsNone(self.signed_in_id(self.client))
        self.post(self.client, "/driver/name", name="With Token")
        self.assertEqual(Operator.query.count(), 1)


# --- session hygiene ----------------------------------------------------------------------------------

class SessionTests(PhoneSecurityCase):
    def test_sign_in_rotates_csrf_and_makes_session_permanent(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        driver = self.driver(NUMBER)
        old_token = self.token(self.client)
        self.send(self.client, "770 1234", "sign_in")
        response = self.post(self.client, "/driver/code", code=self.last_code(NUMBER))
        self.assertEqual(response.status_code, 302)
        data, permanent = self.session_of(self.client)
        self.assertEqual(data.get("operator_id"), driver.id)
        self.assertNotEqual(data.get("_csrf_token"), old_token)
        self.assertTrue(data.get("_csrf_token"))
        self.assertTrue(permanent)
        self.assertNotIn("phone_flow", data)

        # The token from before sign-in no longer authorises anything.
        sent = len(sms.outbox())
        self.client.post("/driver/send-code", data={"csrf_token": old_token,
                                                    "country_code": "+220",
                                                    "phone": "770 1235", "intent": "add"})
        self.assertEqual(len(sms.outbox()), sent)

    def test_new_account_session_is_rotated_too(self):
        self.send(self.client, "770 1234")
        self.post(self.client, "/driver/code", code=self.last_code(NUMBER))
        before = self.token(self.client)
        self.post(self.client, "/driver/name", name="Fatou Ceesay")
        data, permanent = self.session_of(self.client)
        self.assertIsNotNone(data.get("operator_id"))
        self.assertNotEqual(data.get("_csrf_token"), before)
        self.assertTrue(permanent)
        self.assertNotIn("phone_verified", data)

    def test_sign_out_removes_the_driver(self):
        driver = self.driver(NUMBER)
        self.sign_in(self.client, "770 1234")
        self.assertEqual(self.signed_in_id(self.client), driver.id)
        self.client.get("/driver/sign-out")
        data, _ = self.session_of(self.client)
        self.assertNotIn("operator_id", data)
        self.assertNotIn("driver_signed_in_at", data)
        self.assertIn("/driver/sign-in", self.client.get("/operator/").location)


# --- changing the sign-in number -----------------------------------------------------------------------

class ChangeNumberTests(PhoneSecurityCase):
    def setUp(self):
        super().setUp()
        self.relax_limits()
        self.account = self.driver(NUMBER, name="Account Holder")
        self.sign_in(self.client, "770 1234")
        self.assertEqual(self.signed_in_id(self.client), self.account.id)

    def age_sign_in(self, client):
        with client.session_transaction() as session:
            session["driver_signed_in_at"] = int(time.time()) - \
                self.app.config["RECENT_SIGN_IN_SECONDS"] - 120

    def test_stale_sign_in_cannot_start_a_change(self):
        self.age_sign_in(self.client)
        self.assertIn("/driver/sign-in", self.client.get("/driver/phone").location)
        self.sign_in(self.client, "770 1234")
        self.age_sign_in(self.client)
        response = self.send(self.client, "770 1235", intent="add")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/driver/code", response.location)
        self.assertEqual(self.codes_to(OTHER), [])
        self.assertIsNone(self.signed_in_id(self.client))
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, NUMBER)

    def test_recent_sign_in_and_verified_new_number_changes_it(self):
        response = self.send(self.client, "770 1235", intent="add")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/driver/code", response.location)
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, NUMBER)
        code = self.last_code(OTHER)
        self.post(self.client, "/driver/code", code=self.wrong(code))
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, NUMBER)
        self.post(self.client, "/driver/code", code=code)
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, OTHER)
        self.assertEqual(Operator.query.count(), 1)

        other = self.new_client(ip="198.51.100.90")
        self.sign_in(other, "770 1235")
        self.assertEqual(self.signed_in_id(other), self.account.id)

    def test_sign_in_going_stale_before_verification_does_not_attach(self):
        self.send(self.client, "770 1235", intent="add")
        code = self.last_code(OTHER)
        self.age_sign_in(self.client)
        self.post(self.client, "/driver/code", code=code)
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, NUMBER)

    def test_number_of_another_account_is_refused_and_nothing_moves(self):
        holder = self.driver(OTHER, name="Other Holder")
        holder_verified = holder.phone_verified_at
        response = self.send(self.client, "770 1235", intent="add")
        if response.status_code == 302 and "/driver/code" in response.location:
            self.post(self.client, "/driver/code", code=self.last_code(OTHER))
        db.session.expire_all()
        mine = db.session.get(Operator, self.account.id)
        theirs = db.session.get(Operator, holder.id)
        self.assertEqual(mine.phone_e164, NUMBER)
        self.assertEqual(theirs.phone_e164, OTHER)
        self.assertEqual(theirs.phone_verified_at, holder_verified)
        self.assertEqual(self.signed_in_id(self.client), self.account.id)
        self.assertEqual(phone_auth.attach_phone(self.account.id, OTHER), "taken")
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, self.account.id).phone_e164, NUMBER)
        self.assertEqual(db.session.get(Operator, holder.id).phone_e164, OTHER)

    def test_add_intent_without_a_session_sends_nothing(self):
        stranger = self.new_client(ip="198.51.100.91")
        response = self.send(stranger, "770 1236", intent="add")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/driver/sign-in", response.location)
        self.assertEqual(self.codes_to(THIRD), [])


# --- legacy unverified contact numbers ------------------------------------------------------------------

class LegacyContactNumberTests(PhoneSecurityCase):
    def test_unverified_contact_number_blocks_registration(self):
        legacy = Operator(name="Old Account", slug="old-account", status="approved",
                          email="old@example.com", phone="00220 770 1234")
        db.session.add(legacy)
        db.session.commit()
        before = (legacy.name, legacy.status, legacy.email, legacy.phone, legacy.phone_e164)

        for intent in ("join", "sign_in"):
            with self.subTest(intent=intent):
                client = self.new_client()
                self.app.config["SMS_RESEND_SECONDS"] = 0
                self.send(client, "+220 770 1234", intent=intent)
                response = self.post(client, "/driver/code", code=self.last_code(NUMBER))
                self.assertIsNone(self.signed_in_id(client))
                page = client.get("/driver/name")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(b'name="name"', page.data)
                self.post(client, "/driver/name", name="Someone New")
                self.assertEqual(Operator.query.count(), 1)
                self.assertIsNone(self.signed_in_id(client))

        db.session.expire_all()
        legacy = db.session.get(Operator, legacy.id)
        self.assertEqual((legacy.name, legacy.status, legacy.email, legacy.phone,
                          legacy.phone_e164), before)


# --- staff can remove, never set ---------------------------------------------------------------------------

class AdminPhoneTests(PhoneSecurityCase):
    def admin_client(self):
        admin = AdminUser(username="staff")
        admin.set_password("staff-password-long")
        db.session.add(admin)
        db.session.commit()
        client = self.new_client(ip="192.0.2.10")
        client.post("/admin/login", data={"username": "staff",
                                          "password": "staff-password-long"})
        client.get("/admin/operators")
        with client.session_transaction() as session:
            self.assertTrue(session.get("admin_id"))
            token = session["_csrf_token"]
        return client, token

    def test_remove_verified_clears_only_that_account(self):
        target = self.driver(NUMBER, name="Target Driver", phone="+220 770 1234")
        bystander = self.driver(OTHER, name="Bystander Driver", phone="+220 770 1235")
        client, token = self.admin_client()
        response = client.post(f"/admin/operators/{target.id}/phone/remove",
                               data={"csrf_token": token, "which": "verified",
                                     "phone_e164": THIRD, "phone": THIRD})
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        target = db.session.get(Operator, target.id)
        bystander = db.session.get(Operator, bystander.id)
        self.assertIsNone(target.phone_e164)
        self.assertIsNone(target.phone_verified_at)
        self.assertEqual(target.phone, "+220 770 1234")
        self.assertEqual(bystander.phone_e164, OTHER)
        self.assertIsNotNone(bystander.phone_verified_at)
        self.assertEqual(bystander.phone, "+220 770 1235")
        self.assertEqual(Operator.query.filter_by(phone_e164=THIRD).count(), 0)

    def test_remove_without_csrf_changes_nothing(self):
        target = self.driver(NUMBER, name="Target Driver")
        client, _token = self.admin_client()
        client.post(f"/admin/operators/{target.id}/phone/remove", data={"which": "verified"})
        db.session.expire_all()
        self.assertEqual(db.session.get(Operator, target.id).phone_e164, NUMBER)

    def test_no_admin_route_sets_a_phone_number(self):
        phone_rules = sorted(rule.rule for rule in self.app.url_map.iter_rules()
                             if rule.rule.startswith("/admin") and "phone" in rule.rule.lower())
        self.assertEqual(phone_rules, ["/admin/operators/<int:operator_id>/phone/remove"])

        empty = self.driver(None, name="No Number")
        client, token = self.admin_client()
        for which in ("verified", "contact", "set", "add", ""):
            client.post(f"/admin/operators/{empty.id}/phone/remove",
                        data={"csrf_token": token, "which": which, "phone_e164": NUMBER,
                              "phone": NUMBER, "country_code": "+220"})
        db.session.expire_all()
        empty = db.session.get(Operator, empty.id)
        self.assertIsNone(empty.phone_e164)
        self.assertIsNone(empty.phone)
        self.assertEqual(Operator.query.filter_by(phone_e164=NUMBER).count(), 0)

        import app.admin as admin_module
        with open(admin_module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assignments = re.findall(r"phone_e164\s*=\s*(?!=)([^\n]+)", source)
        self.assertTrue(all(value.strip() == "None" for value in assignments), assignments)
        self.assertNotRegex(source, r"setattr\([^)]*phone")


if __name__ == "__main__":
    unittest.main()
