"""The site must read as what it is: a platform that connects customers with
independent drivers.

It owns no cars, employs no drivers, keeps no office and insures nothing. Where
a term belongs to the driver — deposit, insurance, licences, cancellation — the
wording has to say so and tell the customer to settle it with them, rather than
promise it or leave a placeholder where an answer should be.
"""
import html as html_module
import re
import unittest
from datetime import date, timedelta

from app import create_app
from app.models import Booking, Operator, OperatorFare, Vehicle, db
from app.settings import (
    DEFAULTS, FIELDS, PLACEHOLDER_MARKER, current_settings, outstanding_items, save_settings,
)
from config import Config

# Claims the business cannot make: its own fleet, staff, premises or cover.
FORBIDDEN = [
    "our fleet", "our own fleet", "we own", "our cars are", "our own team",
    "we hold a refundable deposit", "our office", "our garage", "our workshop",
    "we insure", "fully insured", "comprehensive insurance", "self-drive hire",
    "we service", "we look after the cars",
]

# Settings whose wording is the owner's own business detail, still unanswered.
STILL_UNKNOWN = {"company_email", "company_phone"}
# The old single-fleet home page. Nothing renders these any more.
LEGACY_HOME = {key for key in FIELDS if key.startswith(("home_", "reason_", "step_", "review_"))}


def visible(html):
    """The words a customer actually reads, without markup or entities."""
    return " ".join(html_module.unescape(re.sub(r"<[^>]+>", " ", html)).split())


class CopyCase(unittest.TestCase):
    def setUp(self):
        class Local(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-" + "x" * 40
            GEOCODER_URL = ""
            REVERSE_GEOCODER_URL = ""
            ROUTER_URL = ""
        self.app = create_app(Local)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})
        self.driver = Operator(name="Awa Jallow", slug="awa-jallow", status="approved",
                               service_area="Kololi")
        db.session.add(self.driver)
        db.session.commit()
        self.car = Vehicle(make="Toyota", model="Yaris", year=2020, daily_rate=2500,
                           deposit=7500, operator_id=self.driver.id, is_active=True,
                           service_mode="rental")
        self.fare = OperatorFare(operator_id=self.driver.id, kind="transfer",
                                 title="Airport transfer", from_location="Banjul Airport",
                                 to_location="Kololi", price=1500, is_active=True)
        db.session.add_all([self.car, self.fare])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def pages(self):
        return ["/", "/fleet", "/about", "/contact", "/rides", "/drivers", "/booking",
                f"/fleet/{self.car.id}", f"/rides/{self.fare.id}",
                f"/drivers/{self.driver.id}"]

    def text(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return visible(response.get_data(as_text=True))


class WhatTheSiteClaims(CopyCase):
    def test_no_page_claims_a_fleet_premises_staff_or_insurance(self):
        for path in self.pages():
            body = self.text(path).lower()
            self.assertEqual([phrase for phrase in FORBIDDEN if phrase in body], [], path)

    def test_no_customer_page_is_left_with_unfinished_wording(self):
        for path in self.pages():
            self.assertNotIn(PLACEHOLDER_MARKER, self.text(path), path)

    def test_the_about_page_says_what_the_platform_actually_does(self):
        body = self.text("/about")
        self.assertIn("We do not own the cars and we do not drive them", body)
        self.assertIn("independent drivers", body)
        self.assertIn("Nothing is charged on this website", body)
        # Approval is a real step, so it may be stated.
        self.assertIn("approved", body)

    def test_placeholders_remain_only_where_the_owner_still_has_to_answer(self):
        unanswered = {key for key, value in DEFAULTS.items()
                      if PLACEHOLDER_MARKER in ("\n".join(value) if isinstance(value, list)
                                                else str(value))}
        self.assertEqual(unanswered - LEGACY_HOME, STILL_UNKNOWN)
        listed = {item["field"].key for item in outstanding_items(DEFAULTS)}
        self.assertEqual(listed, unanswered)


class DriverTerms(CopyCase):
    def test_the_car_page_points_every_term_at_that_car_s_driver(self):
        body = self.text(f"/fleet/{self.car.id}")
        self.assertIn("D7,500.00", body)                       # this car's own deposit
        self.assertIn("The deposit is set by the driver who owns the car", body)
        self.assertIn("set by this car's driver", body)
        self.assertIn("Ask what their insurance covers", body)
        self.assertIn("GoGo Taxi does not provide insurance", body)
        self.assertIn("Confirm with them which licences they accept", body)
        self.assertIn("The driver comes back to you to confirm", body)

    def test_a_deposit_is_never_sold_as_cover_and_never_quoted_site_wide(self):
        settings = current_settings()
        self.assertNotIn("insurance", settings["deposit_policy"].lower())
        self.assertIn("never part of our commission", settings["deposit_policy"])
        # One figure for every car would be wrong: each driver sets their own.
        for key in ("deposit_policy", "vehicle_included_items", "about_included_items"):
            value = settings[key]
            text = "\n".join(value) if isinstance(value, list) else value
            self.assertFalse(re.search(r"D?\d[\d,]{3,}", text), f"{key} quotes a fixed amount")

    def test_what_a_customer_should_settle_before_booking(self):
        body = self.text("/about")
        for question in ["What their insurance covers, and any excess",
                         "Any limit on the distance you may drive",
                         "Whether anyone else may drive the car",
                         "What to do if the car breaks down",
                         "Their terms if you have to cancel"]:
            self.assertIn(question, body)
        self.assertIn("Each driver sets their own requirements", body)

    def test_rides_and_transfers_defer_to_the_driver(self):
        body = self.text("/rides")
        self.assertIn("Your request goes to the driver, who confirms it with you directly", body)
        self.assertIn("run by the driver offering it", body)
        self.assertIn("ask where they will meet you", body)

    def test_a_booking_tells_the_customer_who_they_pay_and_who_to_tell(self):
        booking = Booking(reference=Booking.new_reference(), booking_type="rental",
                          vehicle_id=self.car.id, operator_id=self.driver.id,
                          customer_name="Fatou Ceesay", email="f@example.com",
                          phone="+2207000000", pickup_location="Kololi", dropoff_location="Kololi",
                          start_date=date.today() + timedelta(days=2),
                          end_date=date.today() + timedelta(days=4),
                          total_price=5000, deposit_amount=7500, status="pending")
        db.session.add(booking)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["booking_reference"] = booking.reference
        body = visible(self.client.get(f"/booking/{booking.reference}").get_data(as_text=True))
        self.assertIn("You pay the driver directly", body)
        self.assertIn("Cancellation terms are set by the driver", body)
        self.assertIn("Tell your driver as soon as you can", body)


class DetailsTheOwnerHasNotGiven(CopyCase):
    def test_contact_page_hides_an_office_and_hours_that_do_not_exist(self):
        body = self.text("/contact")
        self.assertNotIn("Office", body)
        self.assertNotIn("Opening hours", body)
        self.assertIn("The form is the way to reach us at the moment", body)
        self.assertIn("Where drivers meet customers", body)
        self.assertNotIn("Opening hours", self.text("/about"))

    def test_an_unconfirmed_phone_number_is_never_printed(self):
        self.assertIn(PLACEHOLDER_MARKER, current_settings()["company_phone"])
        for path in ["/booking", "/fleet", f"/fleet/{self.car.id}"]:
            self.assertNotIn(PLACEHOLDER_MARKER, self.text(path), path)
        self.assertIn("Ask the driver who rents it out", self.text(f"/fleet/{self.car.id}"))

    def test_details_appear_as_soon_as_the_owner_confirms_them(self):
        save_settings({"company_phone": "+220 700 0000", "company_address": "12 Kairaba Avenue",
                       "opening_hours": "Monday to Saturday, 8am to 8pm"})
        body = self.text("/contact")
        self.assertIn("+220 700 0000", body)
        self.assertIn("Office", body)
        self.assertIn("12 Kairaba Avenue", body)
        self.assertIn("Monday to Saturday, 8am to 8pm", body)
        self.assertNotIn("The form is the way to reach us", body)
        self.assertIn("+220 700 0000", self.text(f"/fleet/{self.car.id}"))

    def test_staff_can_still_edit_a_detail_that_is_hidden_from_customers(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
        page = self.client.get("/contact").get_data(as_text=True)
        for key in ("company_address", "opening_hours", "company_phone"):
            self.assertIn(f'data-edit="{key}"', page)
        about = self.client.get("/about").get_data(as_text=True)
        self.assertIn('data-edit="about_hours_body"', about)

    def test_wording_the_owner_saved_still_wins_over_the_new_defaults(self):
        save_settings({"about_section1_body": "We are a family business in Serrekunda.",
                       "vehicle_booking_note": "Ring the bell twice."})
        self.assertIn("We are a family business in Serrekunda.", self.text("/about"))
        self.assertIn("Ring the bell twice.", self.text(f"/fleet/{self.car.id}"))


if __name__ == "__main__":
    unittest.main()
