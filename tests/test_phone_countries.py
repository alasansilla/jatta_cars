"""Phone numbers from The Gambia and Germany (and others) through the driver form.

The form must not cap a number at seven digits, must explain the format for the
chosen country, and every spelling must still land on one E.164 number and one
account through the one-time-code flow.
"""
import html
import json
import re
import time

from app import sms
from app.models import Operator, OperatorFare, PhoneCode, db
from app.phone import InvalidPhone, country_hint, country_hints, normalise, try_normalise
from tests.test_marketplace import MarketplaceCase

GAMBIA_E164 = "+220877701234"
GERMANY_E164 = "+4915123456789"


class GambianNumberTests(MarketplaceCase):
    def test_full_local_numbers_old_and_new_all_become_one_number(self):
        for country, number in (("+220", "87 770 1234"), ("+220", "877701234"), ("+220", "770 1234"),
                                ("+220", "0770 1234"), ("220", "87-770-1234"), ("00220", "87 770 1234"),
                                ("+220", "+220 87 770 1234"), ("+220", "00220 877701234"),
                                ("+49", "+220 87 770 1234")):
            self.assertEqual(normalise(country, number), GAMBIA_E164, (country, number))

    def test_other_gambian_networks(self):
        self.assertEqual(normalise("+220", "390 1234"), "+220833901234")   # QCell, new form
        self.assertEqual(normalise("+220", "990 1234"), "+2209901234")     # Gamcel keeps 7 digits

    def test_wrong_gambian_numbers_explain_with_a_gambian_example(self):
        for number in ("12345", "87 770", "87 770 1234 5678"):
            with self.assertRaises(InvalidPhone) as refused:
                normalise("+220", number)
            self.assertIn("The Gambia (+220)", str(refused.exception), number)
            self.assertIn("87 770 1234", str(refused.exception), number)
        with self.assertRaises(InvalidPhone) as landline:
            normalise("+220", "420 1234")
        self.assertIn("landline", str(landline.exception))


class GermanNumberTests(MarketplaceCase):
    def test_normal_german_mobile_numbers_in_every_spelling(self):
        for country, number in (("+49", "01512 3456789"), ("+49", "1512 3456789"), ("+49", "015123456789"),
                                ("+49", "+49 1512 3456789"), ("+49", "0049 1512 3456789"),
                                ("0049", "01512 3456789"), ("49", "01512-3456789"),
                                ("+220", "+49 1512 3456789")):
            self.assertEqual(normalise(country, number), GERMANY_E164, (country, number))

    def test_german_mobiles_of_different_lengths(self):
        self.assertEqual(normalise("+49", "0170 1234567"), "+491701234567")      # 10 national digits
        self.assertEqual(normalise("+49", "0176 12345678"), "+4917612345678")    # 11 national digits

    def test_wrong_german_numbers_explain_with_a_german_example(self):
        with self.assertRaises(InvalidPhone) as short:
            normalise("+49", "0151 23")
        self.assertIn("Germany (+49)", str(short.exception))
        self.assertIn("01512 3456789", str(short.exception))
        with self.assertRaises(InvalidPhone) as landline:
            normalise("+49", "030 1234567")
        self.assertIn("landline", str(landline.exception))
        self.assertIn("01512 3456789", str(landline.exception))


class OtherCountryTests(MarketplaceCase):
    def test_numbers_are_not_limited_to_seven_digits(self):
        self.assertEqual(normalise("+44", "07911 123456"), "+447911123456")
        self.assertEqual(normalise("+221", "70 123 45 67"), "+221701234567")
        self.assertEqual(normalise("+1", "415 555 2671"), "+14155552671")

    def test_every_country_example_is_a_number_the_form_accepts(self):
        hints = country_hints()
        self.assertEqual(hints["220"], {"name": "The Gambia", "example": "87 770 1234"})
        self.assertEqual(hints["49"], {"name": "Germany", "example": "01512 3456789"})
        failing = [code for code, hint in hints.items() if try_normalise(hint["example"], "+" + code) is None]
        self.assertEqual(failing, [])

    def test_unknown_country_code_is_explained(self):
        for code in ("+999", "", "+12345"):
            with self.assertRaises(InvalidPhone) as refused:
                normalise(code, "87 770 1234")
            self.assertIn("country code", str(refused.exception))


class PhoneFormTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        OperatorFare.query.delete()
        Operator.query.delete()
        db.session.commit()
        self.app.config.update(SMS_BACKEND="fake", SMS_FAKE_OUTBOX="")
        sms.outbox().clear()

    def browser(self, ip="203.0.113.20"):
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = ip
        client.get("/driver/sign-in")
        return client

    def post(self, client, path, **data):
        with client.session_transaction() as session:
            data["csrf_token"] = session["_csrf_token"]
        return client.post(path, data=data)

    def code(self):
        return re.search(r"\b(\d{6})\b", sms.outbox()[-1]["body"]).group(1)

    def test_the_number_field_is_not_capped_and_explains_the_format(self):
        for path in ("/driver/join", "/driver/sign-in"):
            page = self.app.test_client().get(path).get_data(as_text=True)
            field = re.search(r'<input id="phone"[^>]*>', page, re.S).group(0)
            self.assertGreaterEqual(int(re.search(r'maxlength="(\d+)"', field).group(1)), 20)
            self.assertNotIn("pattern=", field)
            self.assertIn('placeholder="87 770 1234"', field)
            self.assertIn("Example for The Gambia: 87 770 1234", html.unescape(page))
            hints = json.loads(re.search(r"var HINTS = (\{.*?\});", page, re.S).group(1))
            self.assertEqual(hints["49"]["example"], "01512 3456789")

    def test_an_error_keeps_the_typed_number_and_shows_that_countrys_example(self):
        response = self.post(self.browser(), "/driver/send-code", country_code="+49", phone="0151 23",
                             intent="join")
        self.assertEqual(response.status_code, 400)
        page = html.unescape(response.get_data(as_text=True))
        self.assertIn('value="0151 23"', page)
        self.assertIn('placeholder="01512 3456789"', page)
        self.assertIn("Germany (+49)", page)
        self.assertEqual(len(sms.outbox()), 0)

    def join(self, client, country, number):
        response = self.post(client, "/driver/send-code", country_code=country, phone=number, intent="join")
        self.assertEqual((response.status_code, response.location), (302, "/driver/code"), response.get_data(as_text=True)[-400:])
        self.assertEqual(self.post(client, "/driver/code", code=self.code()).status_code, 302)
        return self.post(client, "/driver/name", name="Anna Schmidt", service_area="Kololi")

    def test_a_german_driver_signs_up_and_every_spelling_opens_the_same_account(self):
        first = self.browser()
        self.join(first, "+49", "01512 3456789")
        self.assertEqual(sms.outbox()[-1]["to"], GERMANY_E164)
        driver = Operator.query.one()
        self.assertEqual((driver.phone_e164, driver.status), (GERMANY_E164, "pending"))

        self.app.config["SMS_RESEND_SECONDS"] = 0
        for country, number in (("0049", "1512 3456789"), ("+220", "+49 1512-3456789")):
            again = self.browser()
            sent = self.post(again, "/driver/send-code", country_code=country, phone=number, intent="sign_in")
            self.assertEqual(sent.status_code, 302, (country, number))
            response = self.post(again, "/driver/code", code=self.code())
            self.assertIn("/driver/status", response.location)
            with again.session_transaction() as session:
                self.assertEqual(session["operator_id"], driver.id)
        self.assertEqual(Operator.query.count(), 1)

    def test_a_gambian_driver_with_the_full_local_number(self):
        client = self.browser()
        self.join(client, "+220", "87 770 1234")
        self.assertEqual(Operator.query.one().phone_e164, GAMBIA_E164)
        self.app.config["SMS_RESEND_SECONDS"] = 0
        old_spelling = self.browser()
        self.post(old_spelling, "/driver/send-code", country_code="+220", phone="770 1234", intent="sign_in")
        self.post(old_spelling, "/driver/code", code=self.code())
        self.assertEqual(Operator.query.count(), 1)

    def test_the_code_still_has_to_be_right(self):
        client = self.browser()
        self.post(client, "/driver/send-code", country_code="+49", phone="01512 3456789", intent="join")
        wrong = "000000" if self.code() != "000000" else "111111"
        response = self.post(client, "/driver/code", code=wrong)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Operator.query.count(), 0)
        self.assertEqual(PhoneCode.query.one().attempts, 1)
