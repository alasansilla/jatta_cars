"""Driver sign-up and sign-in by phone, end to end.

Sign-up: number -> texted 6-digit code -> name and where you drive -> pending.
Sign-in: number -> code -> the right place (dashboard if approved, application
status if not). Uses the in-process fake SMS transport; nothing is sent.
"""
import html
import re
import time
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app, phone_auth, sms
from app.models import AuthEvent, Operator, OperatorFare, PhoneCode, db
from app.phone import normalise
from app.settings import save_settings
from tests.test_marketplace import MarketplaceCase, TestConfig

NUMBER = "770 1234"                       # an Africell number, stored in 9-digit form
E164 = normalise("+220", NUMBER)


class FakeSmsConfig(TestConfig):
    SMS_BACKEND = "fake"
    SMS_FAKE_OUTBOX = ""


class FlowCase(MarketplaceCase):
    def setUp(self):
        super().setUp()
        # Start with no driver accounts at all, so counts mean what they say.
        OperatorFare.query.delete()
        Operator.query.delete()
        db.session.commit()
        self.app.config.update(SMS_BACKEND="fake", SMS_FAKE_OUTBOX="")
        sms.outbox().clear()
        self.browser = self.new_browser()

    def new_browser(self, ip="203.0.113.10"):
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = ip
        client.get("/driver/sign-in")
        return client

    def text(self, response):
        return html.unescape(response.get_data(as_text=True))

    def post(self, client, path, **data):
        with client.session_transaction() as session:
            data.setdefault("csrf_token", session.get("_csrf_token", ""))
        return client.post(path, data=data)

    def send(self, client=None, number=NUMBER, intent="join", country="+220"):
        return self.post(client or self.browser, "/driver/send-code",
                         country_code=country, phone=number, intent=intent)

    def last_code(self):
        return re.search(r"\b(\d{6})\b", sms.outbox()[-1]["body"]).group(1)

    def verify(self, client=None, code=None):
        return self.post(client or self.browser, "/driver/code", code=code or self.last_code())

    def details(self, client=None, **overrides):
        data = {"name": "Fatou Jallow", "service_area": "Kololi and the airport", "about": ""}
        data.update(overrides)
        return self.post(client or self.browser, "/driver/name", **data)

    def signed_in_as(self, client):
        with client.session_transaction() as session:
            return session.get("operator_id")

    def join_fully(self, client=None, number=NUMBER):
        client = client or self.browser
        self.assertEqual(self.send(client, number).status_code, 302)
        self.assertEqual(self.verify(client).status_code, 302)
        return self.details(client)


# --- the pages -----------------------------------------------------------------

class PageCopyTests(FlowCase):
    def test_pages_offer_a_working_form_without_placeholder_or_setup_copy(self):
        save_settings({"operators_intro": "[TBC] intro", "operators_apply_note": "[TBC] note",
                       "operators_requirements": "[TBC] rule"})
        for backend in ("fake", ""):
            self.app.config["SMS_BACKEND"] = backend
            for path in ("/driver/join", "/driver/sign-in"):
                page = self.app.test_client().get(path).get_data(as_text=True)
                self.assertNotIn("[TBC]", page, (backend, path))
                self.assertNotIn("switched on", page.lower(), (backend, path))
                button = re.search(r'<button[^>]*type="submit"[^>]*>\s*Send me a code', page)
                self.assertIsNotNone(button, (backend, path))
                self.assertNotIn("disabled", button.group(0), (backend, path))


# --- normalisation and one account per number -------------------------------

class NormalisationAndUniquenessTests(FlowCase):
    def test_every_spelling_is_the_same_number(self):
        for spelling in ("7701234", "770 1234", "+220 770-1234", "00220 7701234", "2207701234",
                         "0770 1234", "87 770 1234", "+220 87 770 1234"):
            self.assertEqual(normalise("+220", spelling), E164, spelling)

    def test_signing_up_again_with_another_spelling_opens_the_same_account(self):
        self.join_fully()
        first = Operator.query.one()
        self.browser.get("/driver/sign-out")
        self.app.config["SMS_RESEND_SECONDS"] = 0
        other = self.new_browser()
        self.assertEqual(self.send(other, "+220 87 770-1234", intent="join").status_code, 302)
        response = self.verify(other)
        self.assertEqual(Operator.query.count(), 1)
        self.assertEqual(self.signed_in_as(other), first.id)
        # Their licence and identification are still outstanding, so that is
        # where they land rather than on the waiting page.
        self.assertIn("/driver/documents", response.location)

    def test_the_database_refuses_a_second_account_for_a_number(self):
        from sqlalchemy.exc import IntegrityError
        db.session.add(Operator(name="A", slug="a", phone_e164=E164, status="pending"))
        db.session.commit()
        db.session.add(Operator(name="B", slug="b", phone_e164=E164, status="pending"))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_invalid_numbers_are_refused_before_anything_is_sent(self):
        for number in ("123", "abc", "4201234", "770 1234 ext 5"):
            response = self.send(number=number)
            self.assertEqual(response.status_code, 400, number)
        self.assertEqual(len(sms.outbox()), 0)
        self.assertEqual(PhoneCode.query.count(), 0)


# --- sign-up -----------------------------------------------------------------

class SignUpTests(FlowCase):
    def test_sign_up_collects_details_and_leaves_the_account_pending(self):
        self.assertEqual(self.send().status_code, 302)
        self.assertEqual(Operator.query.count(), 0, "entering a number created an account")
        self.assertIsNone(self.signed_in_as(self.browser))
        self.assertEqual(self.verify().status_code, 302)
        self.assertEqual(Operator.query.count(), 0, "verifying alone created an account")

        form = self.browser.get("/driver/name").get_data(as_text=True)
        self.assertIn('name="service_area"', form)
        self.assertIn('name="name"', form)

        response = self.details(about="I speak Wolof and English.")
        self.assertEqual(response.status_code, 302)
        driver = Operator.query.one()
        self.assertEqual(driver.status, "pending")
        self.assertEqual(driver.phone_e164, E164)
        self.assertEqual(driver.display_name, "Fatou Jallow")
        self.assertEqual(driver.service_area, "Kololi and the airport")
        self.assertEqual(driver.terms, "I speak Wolof and English.")
        self.assertIsNone(driver.email)
        self.assertIsNone(driver.password_hash)
        self.assertEqual(self.signed_in_as(self.browser), driver.id)
        # A new driver is asked for their licence and identification next.
        self.assertIn("/driver/documents", response.location)
        page = self.browser.get("/driver/documents").get_data(as_text=True)
        self.assertIn("Driving licence", page)
        self.assertIn("Photo identification", page)

    def test_details_are_required_and_explained(self):
        self.send(); self.verify()
        for bad in ({"name": ""}, {"name": "1"}, {"service_area": ""}, {"service_area": "!"},
                    {"about": "x" * 1001}):
            response = self.details(**bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn('role="alert"', response.get_data(as_text=True))
        self.assertEqual(Operator.query.count(), 0)

    def test_details_page_needs_a_fresh_verification(self):
        self.assertIn("/driver/join", self.browser.get("/driver/name").location)
        self.send(); self.verify()
        with self.browser.session_transaction() as session:
            verified = dict(session["phone_verified"])
            verified["at"] = int(time.time()) - 3600
            session["phone_verified"] = verified
        response = self.details()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Operator.query.count(), 0)

    def test_pending_accounts_see_status_not_the_dashboard(self):
        self.join_fully()
        self.assertIn("/driver/status", self.browser.get("/operator/").location)
        self.assertIn("/driver/status", self.browser.get("/operator/drive").location)
        page = self.browser.get("/driver/status").get_data(as_text=True)
        self.assertIn("checking your details", page)


# --- sign-in -----------------------------------------------------------------

class SignInTests(FlowCase):
    def make_driver(self, status):
        driver = Operator(name="Modou Ceesay", slug=f"modou-{status}", phone_e164=E164,
                          phone_verified_at=datetime.utcnow(), status=status)
        db.session.add(driver)
        db.session.commit()
        return driver

    def test_an_approved_driver_lands_on_the_dashboard(self):
        driver = self.make_driver("approved")
        self.assertEqual(self.send(intent="sign_in").status_code, 302)
        response = self.verify()
        self.assertIn("/operator/", response.location)
        self.assertEqual(self.signed_in_as(self.browser), driver.id)
        self.assertEqual(self.browser.get("/operator/").status_code, 200)

    def test_a_pending_driver_lands_where_their_application_is_waiting(self):
        driver = self.make_driver("pending")
        self.send(intent="sign_in")
        response = self.verify()
        # Documents outstanding: that is the next thing they can actually do.
        self.assertIn("/driver/documents", response.location)
        self.assertEqual(self.signed_in_as(self.browser), driver.id)
        self.assertIn("Send your licence and photo identification",
                      self.browser.get("/driver/status").get_data(as_text=True))

    def test_a_suspended_driver_is_signed_in_but_cannot_work(self):
        self.make_driver("suspended")
        self.send(intent="sign_in"); self.verify()
        self.assertIn("/driver/status", self.browser.get("/operator/").location)

    def test_an_unknown_number_gets_the_sign_up_details_only_after_verifying(self):
        self.send(intent="sign_in")
        self.assertIsNone(self.signed_in_as(self.browser))
        response = self.verify()
        self.assertIn("/driver/name", response.location)
        self.assertEqual(Operator.query.count(), 0)

    def test_wrong_code_never_signs_in(self):
        self.make_driver("approved")
        self.send(intent="sign_in")
        wrong = "000000" if self.last_code() != "000000" else "111111"
        response = self.verify(code=wrong)
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't right", self.text(response))
        self.assertIsNone(self.signed_in_as(self.browser))


# --- responses say nothing about whether a number is registered -------------

class GenericResponseTests(FlowCase):
    def normalised_page(self, response):
        text = response.get_data(as_text=True)
        text = re.sub(r'name="csrf_token" value="[^"]+"', "", text)
        return re.sub(r"\+220[\d ]+", "NUMBER", text)

    def test_sending_a_code_looks_identical_for_registered_and_unknown_numbers(self):
        db.session.add_all([
            Operator(name="Known", slug="known", phone_e164=normalise("+220", "390 1111"), status="approved"),
            Operator(name="Known 2", slug="known-2", phone_e164=normalise("+220", "390 3333"), status="pending"),
        ])
        db.session.commit()
        pages = []
        for intent, numbers in (("join", ("390 1111", "390 2222")), ("sign_in", ("390 3333", "390 4444"))):
            for number in numbers:
                browser = self.new_browser()
                sent = self.send(browser, number, intent=intent)
                self.assertEqual((sent.status_code, sent.location), (302, "/driver/code"), (intent, number))
                pages.append((intent, self.normalised_page(browser.get("/driver/code"))))
                again = self.send(browser, number, intent=intent)
                self.assertEqual(again.status_code, 429, (intent, number))
        by_intent = {}
        for intent, page in pages:
            by_intent.setdefault(intent, set()).add(page)
        for intent, variants in by_intent.items():
            self.assertEqual(len(variants), 1, f"{intent}: the code page differs by number")

    def test_no_provider_gives_the_same_retry_message_for_every_number(self):
        self.app.config["SMS_BACKEND"] = ""
        db.session.add(Operator(name="Known", slug="known", phone_e164=E164, status="approved"))
        db.session.commit()
        messages = set()
        for number in (NUMBER, "390 2222"):
            response = self.send(self.new_browser(), number, intent="sign_in")
            self.assertEqual(response.status_code, 400)
            body = self.text(response)
            self.assertNotIn("switched on", body.lower())
            messages.add(re.search(r'role="alert">([^<]+)<', body).group(1))
        self.assertEqual(len(messages), 1)
        self.assertIn("couldn't send a code", messages.pop())
        self.assertEqual(PhoneCode.query.count(), 0)
        self.assertEqual(len(sms.outbox()), 0)

    def test_adding_a_number_another_driver_uses_texts_nobody_and_reveals_nothing(self):
        holder = Operator(name="Holder", slug="holder", phone_e164=E164, status="approved")
        asker = Operator(name="Asker", slug="asker", status="approved")
        db.session.add_all([holder, asker])
        db.session.commit()
        pages = []
        for number in (NUMBER, "390 2222"):
            browser = self.new_browser()
            with browser.session_transaction() as session:
                session["operator_id"] = asker.id
                session["driver_signed_in_at"] = int(time.time())
            sent = self.send(browser, number, intent="add")
            self.assertEqual((sent.status_code, sent.location), (302, "/driver/code"), number)
            pages.append(self.normalised_page(browser.get("/driver/code")))
            if number == NUMBER:
                taken_browser = browser
        self.assertEqual(pages[0], pages[1])
        self.assertEqual([m["to"] for m in sms.outbox()], [normalise("+220", "390 2222")],
                         "a code was texted to another driver's number")
        for guess in ("000000", "123456", "999999"):
            response = self.verify(taken_browser, guess)
            self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Operator, asker.id).phone_e164)


# --- the code itself ---------------------------------------------------------

class CodeLifecycleTests(FlowCase):
    def test_codes_expire(self):
        self.send()
        code = self.last_code()
        row = PhoneCode.query.one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        response = self.verify(code=code)
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True))
        self.assertIsNone(self.signed_in_as(self.browser))

    def test_codes_work_once(self):
        self.send()
        code = self.last_code()
        self.assertEqual(self.verify(code=code).status_code, 302)
        self.browser.get("/driver/sign-out")
        with self.browser.session_transaction() as session:
            session["phone_flow"] = {"phone": E164, "purpose": "sign_in", "intent": "join",
                                     "nonce": "replayed", "started": int(time.time())}
        self.assertEqual(self.verify(code=code).status_code, 400)

    def test_guesses_are_limited_even_before_the_right_code(self):
        self.app.config["SMS_CODE_MAX_ATTEMPTS"] = 3
        self.send()
        code = self.last_code()
        wrong = [c for c in ("000000", "111111", "222222", "333333") if c != code][:3]
        for guess in wrong:
            self.verify(code=guess)
        response = self.verify(code=code)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.signed_in_as(self.browser))
        self.assertLessEqual(PhoneCode.query.one().attempts, 3)

    def test_a_code_only_works_in_the_browser_that_asked_for_it(self):
        self.send()
        code = self.last_code()
        thief = self.new_browser()
        with self.browser.session_transaction() as session:
            flow = dict(session["phone_flow"])
        flow["nonce"] = "someone-else"
        with thief.session_transaction() as session:
            session["phone_flow"] = flow
        self.assertEqual(self.verify(thief, code).status_code, 400)
        self.assertIsNone(self.signed_in_as(thief))
        self.assertEqual(self.verify(self.browser, code).status_code, 302)

    def test_a_new_code_replaces_the_old_one(self):
        self.app.config["SMS_RESEND_SECONDS"] = 0
        self.send()
        old = self.last_code()
        self.post(self.browser, "/driver/code/resend")
        new = self.last_code()
        if old != new:
            self.assertEqual(self.verify(code=old).status_code, 400)
        self.assertEqual(self.verify(code=new).status_code, 302)

    def test_pasted_message_text_is_accepted(self):
        self.send()
        code = self.last_code()
        self.assertEqual(self.verify(code=f"Your code is {code[:3]} {code[3:]}.").status_code, 302)


# --- abuse limits and CSRF ----------------------------------------------------

class RateLimitAndCsrfTests(FlowCase):
    def test_resend_cooldown(self):
        self.assertEqual(self.send().status_code, 302)
        response = self.send()
        self.assertEqual(response.status_code, 429)
        self.assertIn("wait", response.get_data(as_text=True).lower())
        self.assertEqual(len(sms.outbox()), 1)

    def test_per_number_hourly_cap_across_browsers_and_addresses(self):
        self.app.config.update(SMS_RESEND_SECONDS=0, SMS_MAX_PER_NUMBER_HOUR=3)
        codes = [self.send(self.new_browser(f"198.51.100.{i}")).status_code for i in range(5)]
        self.assertEqual(codes[:3], [302, 302, 302])
        self.assertEqual(codes[3:], [429, 429])
        self.assertEqual(len(sms.outbox()), 3)

    def test_per_address_hourly_cap(self):
        self.app.config.update(SMS_MAX_PER_IP_HOUR=2)
        results = [self.send(self.new_browser("192.0.2.7"), f"390 {1000 + i}").status_code
                   for i in range(4)]
        self.assertEqual(results, [302, 302, 429, 429])

    def test_wrong_codes_lock_the_address(self):
        self.app.config.update(SMS_WRONG_CODES_PER_IP_HOUR=3, SMS_RESEND_SECONDS=0)
        self.send()
        for guess in ("000001", "000002", "000003"):
            self.verify(code=guess)
        self.assertEqual(self.send(number="390 5555").status_code, 429)
        self.assertEqual(self.verify(code=self.last_code()).status_code, 400)

    def test_every_form_needs_the_csrf_token(self):
        for path, data in (("/driver/send-code", {"phone": NUMBER, "country_code": "+220"}),
                           ("/driver/code", {"code": "123456"}),
                           ("/driver/code/resend", {}),
                           ("/driver/name", {"name": "X Y", "service_area": "Kololi"})):
            response = self.browser.post(path, data=dict(data, csrf_token="forged"))
            self.assertEqual(response.status_code, 302, path)
        self.assertEqual(len(sms.outbox()), 0)
        self.assertEqual(PhoneCode.query.count(), 0)
        self.assertEqual(Operator.query.count(), 0)
        self.assertEqual(AuthEvent.query.filter(AuthEvent.kind != "send_lock").count(), 0)
