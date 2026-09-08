"""The draft must not carry the old German demo copy, or promise things the
site cannot do (no email is sent, no payment is taken)."""
import unittest
from datetime import date, timedelta

from app import create_app
from app.models import Vehicle, db
from app.settings import (
    DEFAULTS, PLACEHOLDER_MARKER, current_settings, outstanding_items, save_settings,
)
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "test-only"


PUBLIC_PAGES = ["/", "/fleet", "/about", "/contact", "/booking"]

# Wording that belonged to the Berlin demo, or that the site cannot honour.
FORBIDDEN = [
    "berlin", "germany", "potsdam", "dresden", "autobahn", "hauptbahnhof",
    "baltic", "ringbahn", "winter tyres", "+49", "€",
    "unlimited kilometres", "24/7 roadside", "comprehensive insurance",
    "confirm by email", "free cancellation up to 24",
]


class GambiaContentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.car = Vehicle(
            make="Sample", model="Car", year=2022, category="Economy",
            daily_rate=2500, deposit=10000, is_active=True,
        )
        db.session.add(self.car)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_public_pages_carry_no_demo_or_unsupported_copy(self):
        pages = PUBLIC_PAGES + [f"/fleet/{self.car.id}"]
        for path in pages:
            body = self.client.get(path).get_data(as_text=True).lower()
            hits = [phrase for phrase in FORBIDDEN if phrase in body]
            self.assertEqual(hits, [], f"demo/unsupported copy on {path}: {hits}")

    def test_currency_is_not_the_euro(self):
        self.assertEqual(current_settings()["currency"], "D")

    def test_unknown_details_are_marked_not_invented(self):
        """Contact details we were never told must read as placeholders."""
        settings = current_settings()
        for key in ("company_phone", "company_email", "company_address",
                    "opening_hours"):
            self.assertIn(PLACEHOLDER_MARKER, settings[key], key)

    def test_pickup_points_start_from_what_the_business_told_us(self):
        """Kololi is a confirmed fact; the rest is left open rather than guessed."""
        locations = current_settings()["locations"]
        self.assertIn("Kololi", locations)
        self.assertTrue(
            any("agree it when we confirm" in item for item in locations),
            "there should be an option for the other places they hand cars over",
        )

    def test_a_deposit_is_never_described_as_insurance(self):
        """A refundable deposit is not cover, and must not be sold as it."""
        settings = current_settings()
        deposit_copy = (settings["deposit_policy"] + settings["reason_2_body"]).lower()
        self.assertNotIn("insurance", deposit_copy)
        # Insurance itself stays an open question until the business answers it.
        self.assertIn(PLACEHOLDER_MARKER, settings["insurance_note"])

    def test_foreign_prices_appear_only_once_a_rate_is_entered(self):
        from app.settings import save_settings as save

        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("\u2248", page, "no conversion before a rate is set")

        save({"fx_eur_rate": "70"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("\u2248", page)
        self.assertIn("\u20ac36", page)  # D2,500 at 70 to the euro

    def test_conversion_rate_of_zero_is_treated_as_unset(self):
        from app.settings import save_settings as save

        save({"fx_eur_rate": "0", "fx_gbp_rate": "0"})
        self.assertNotIn("\u2248", self.client.get("/").get_data(as_text=True))

    def test_excess_is_not_quoted_until_it_is_set(self):
        """A zero excess must never render as a real figure such as D0.00."""
        from app.settings import fill_tokens

        settings = current_settings()
        self.assertEqual(settings["default_excess"], 0)
        rendered = fill_tokens("Excess is {excess}.", settings)
        self.assertIn(PLACEHOLDER_MARKER, rendered)
        self.assertNotIn("D0", rendered)

    def test_checklist_lists_every_placeholder(self):
        self.assertEqual(len(outstanding_items(DEFAULTS)), 40)

    def test_checklist_shrinks_as_settings_are_filled_in(self):
        before = len(outstanding_items())
        save_settings({"company_phone": "+220 700 0000"})
        self.assertEqual(len(outstanding_items()), before - 1)

    def test_booking_still_works_end_to_end(self):
        settings = current_settings()
        start = date.today() + timedelta(days=3)
        end = start + timedelta(days=2)
        response = self.client.post(
            f"/fleet/{self.car.id}/book",
            data={
                "start": start.isoformat(), "end": end.isoformat(),
                "customer_name": "Test Customer", "email": "test@example.com",
                "phone": "+220 700 1111",
                "pickup_location": settings["locations"][0],
                "dropoff_location": settings["locations"][0],
            },
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("JC-", page)
        self.assertIn("D", page)  # priced in dalasi

    def test_booking_rejects_a_location_that_is_no_longer_offered(self):
        stale = current_settings()["locations"][0]
        save_settings({"locations": "Serrekunda office\nBanjul airport"})
        start = date.today() + timedelta(days=3)
        response = self.client.post(
            f"/fleet/{self.car.id}/book",
            data={
                "start": start.isoformat(),
                "end": (start + timedelta(days=2)).isoformat(),
                "customer_name": "Test Customer", "email": "test@example.com",
                "pickup_location": stale, "dropoff_location": "Banjul airport",
            },
            follow_redirects=True,
        )
        self.assertIn("Choose a pick-up location", response.get_data(as_text=True))

    def test_reviews_are_placeholders_not_invented_quotes(self):
        settings = current_settings()
        for n in (1, 2, 3):
            self.assertIn(PLACEHOLDER_MARKER, settings[f"review_{n}_quote"])
            self.assertIn(PLACEHOLDER_MARKER, settings[f"review_{n}_name"])

    def test_reviews_section_can_be_switched_off(self):
        def shown():
            return "quote-card" in self.client.get("/").get_data(as_text=True)

        self.assertTrue(shown(), "reviews should render by default")
        save_settings({"show_reviews": False})
        self.assertFalse(shown(), "reviews should disappear once switched off")

    def test_avatar_initial_ignores_the_placeholder_marker(self):
        """The initial must not render as '[' from '[TBC] customer name'."""
        page = self.client.get("/").get_data(as_text=True)
        bad = '<span class="avatar" aria-hidden="true">[</span>' in page
        self.assertFalse(bad, "avatar initial fell back to the marker bracket")


if __name__ == "__main__":
    unittest.main()


class DemoFleetRemovalTests(unittest.TestCase):
    """The demo-fleet tool must never take a car someone is relying on."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _demo_car(self, **overrides):
        from seed import DEMO_FLEET

        spec = dict(DEMO_FLEET[0])
        spec.update(overrides)
        car = Vehicle(**spec)
        db.session.add(car)
        db.session.commit()
        return car

    def test_untouched_demo_car_is_removable(self):
        from tools.clear_demo_fleet import classify

        self.assertIsNone(classify(self._demo_car()))

    def test_car_with_a_booking_is_kept(self):
        from datetime import date, timedelta

        from app.models import Booking
        from tools.clear_demo_fleet import classify

        car = self._demo_car()
        start = date.today() + timedelta(days=1)
        db.session.add(Booking(
            reference="JC-KEEP01", vehicle=car, customer_name="Someone",
            email="someone@example.com", pickup_location="x", dropoff_location="x",
            start_date=start, end_date=start + timedelta(days=1), total_price=1,
        ))
        db.session.commit()
        self.assertEqual(classify(car), "has bookings against it")

    def test_car_with_an_uploaded_photo_is_kept(self):
        from tools.clear_demo_fleet import classify

        self.assertEqual(
            classify(self._demo_car(image="uploads/mine.jpg")),
            "has a photo you uploaded",
        )

    def test_car_with_an_edited_description_is_kept(self):
        from tools.clear_demo_fleet import classify

        self.assertEqual(
            classify(self._demo_car(description="My own words about this car.")),
            "the description has been edited",
        )

    def test_a_car_you_added_yourself_is_never_touched(self):
        from tools.clear_demo_fleet import classify

        mine = Vehicle(make="Toyota", model="Hilux", year=2021, daily_rate=3000)
        db.session.add(mine)
        db.session.commit()
        self.assertEqual(classify(mine), "not part of the demo fleet")


class InlineFleetEditingTests(unittest.TestCase):
    """A car must be addable, editable and removable without leaving the page."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from app.models import AdminUser

        admin = AdminUser(username="admin")
        admin.set_password("test-password")
        db.session.add(admin)
        db.session.commit()
        self.client = self.app.test_client()
        self.client.post("/admin/login",
                         data={"username": "admin", "password": "test-password"})
        # The token is minted when a page renders, so fetch one first.
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["_csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _add(self):
        response = self.client.post("/admin/api/vehicle/new", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["id"]

    def test_a_new_car_starts_hidden_from_the_public_site(self):
        vehicle_id = self._add()
        car = db.session.get(Vehicle, vehicle_id)
        self.assertFalse(car.is_active, "a half-filled car must not be public")
        self.assertNotIn("New car", self.client.get("/fleet").get_data(as_text=True))

    def test_every_offered_field_can_be_edited_inline(self):
        vehicle_id = self._add()
        response = self.client.post("/admin/api/save", headers=self.headers, json={
            "records": {
                f"vehicle:{vehicle_id}:make": "Toyota",
                f"vehicle:{vehicle_id}:model": "RAV4",
                f"vehicle:{vehicle_id}:year": "2019",
                f"vehicle:{vehicle_id}:seats": "5",
                f"vehicle:{vehicle_id}:doors": "5",
                f"vehicle:{vehicle_id}:luggage": "3",
                f"vehicle:{vehicle_id}:category": "SUV",
                f"vehicle:{vehicle_id}:transmission": "Automatic",
                f"vehicle:{vehicle_id}:fuel": "Petrol",
                f"vehicle:{vehicle_id}:daily_rate": "10,000",
                f"vehicle:{vehicle_id}:deposit": "5000",
            },
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        car = db.session.get(Vehicle, vehicle_id)
        self.assertEqual(car.name, "Toyota RAV4")
        self.assertEqual(car.category, "SUV")
        self.assertEqual(float(car.daily_rate), 10000.0)  # comma tolerated
        self.assertEqual(float(car.deposit), 5000.0)
        self.assertEqual(car.year, 2019)

    def test_a_choice_field_rejects_anything_off_the_list(self):
        vehicle_id = self._add()
        response = self.client.post("/admin/api/save", headers=self.headers, json={
            "records": {f"vehicle:{vehicle_id}:category": "Spaceship"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be one of", response.get_json()["error"])

    def test_a_choice_field_accepts_a_differently_cased_value(self):
        vehicle_id = self._add()
        self.client.post("/admin/api/save", headers=self.headers, json={
            "records": {f"vehicle:{vehicle_id}:transmission": "automatic"}})
        self.assertEqual(db.session.get(Vehicle, vehicle_id).transmission, "Automatic")

    def test_nonsense_numbers_are_refused(self):
        vehicle_id = self._add()
        for field, value in (("year", "1066"), ("seats", "900"), ("doors", "0")):
            response = self.client.post("/admin/api/save", headers=self.headers, json={
                "records": {f"vehicle:{vehicle_id}:{field}": value}})
            self.assertEqual(response.status_code, 400, f"{field}={value} was accepted")

    def test_listing_can_be_toggled_without_leaving_the_page(self):
        vehicle_id = self._add()
        response = self.client.post(
            f"/admin/api/vehicle/{vehicle_id}/listed", headers=self.headers)
        self.assertTrue(response.get_json()["listed"])
        self.assertTrue(db.session.get(Vehicle, vehicle_id).is_active)

    def test_a_car_can_be_removed_inline(self):
        vehicle_id = self._add()
        response = self.client.post(
            f"/admin/api/vehicle/{vehicle_id}/delete", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(db.session.get(Vehicle, vehicle_id))

    def test_a_car_with_a_live_booking_cannot_be_removed_inline(self):
        from datetime import date as _date

        from app.models import Booking

        vehicle_id = self._add()
        start = _date.today() + timedelta(days=2)
        db.session.add(Booking(
            reference="JC-LIVE01", vehicle_id=vehicle_id, customer_name="Someone",
            email="someone@example.com", pickup_location="Kololi",
            dropoff_location="Kololi", start_date=start,
            end_date=start + timedelta(days=1), total_price=1, status="confirmed",
        ))
        db.session.commit()
        response = self.client.post(
            f"/admin/api/vehicle/{vehicle_id}/delete", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("open booking", response.get_json()["error"])
        self.assertIsNotNone(db.session.get(Vehicle, vehicle_id))

    def test_the_fleet_endpoints_are_closed_to_the_public(self):
        """A signed-out visitor gets nowhere, and changes nothing.

        Writes are refused by the CSRF guard before the login check even runs,
        so the status is 400 rather than a redirect; what matters is that the
        request is rejected and the fleet is untouched.
        """
        anonymous = self.app.test_client()
        before = Vehicle.query.count()
        for path in ("/admin/api/vehicle/new", "/admin/api/vehicle/1/delete",
                     "/admin/api/vehicle/1/listed"):
            self.assertNotEqual(anonymous.post(path).status_code, 200, path)
        self.assertEqual(Vehicle.query.count(), before)
        # Reads are behind the login redirect.
        self.assertEqual(anonymous.get("/admin/api/choices").status_code, 302)

    def test_adding_a_car_requires_the_csrf_token(self):
        self.assertEqual(self.client.post("/admin/api/vehicle/new").status_code, 400)
