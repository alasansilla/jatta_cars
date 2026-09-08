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
        for location in settings["locations"]:
            self.assertIn(PLACEHOLDER_MARKER, location)

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
