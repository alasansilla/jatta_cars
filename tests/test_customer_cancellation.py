"""A customer can call off a rental or a booked journey themselves.

Until now only an on-demand ride could be cancelled online: a rental or an
airport transfer had to be sorted out with the driver, which left the car
looking booked to everyone else in the meantime. Cancelling frees the dates at
once, earns nobody commission, and cannot be done to a booking the driver has
already finished — or by anyone who has not proved the reference and email.
"""
import unittest
from datetime import date, timedelta

from app import create_app
from app.models import Booking, CommissionEntry, Operator, Vehicle, db
from app.settings import save_settings
from config import Config


class CancellationCase(unittest.TestCase):
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
        self.driver = Operator(name="Awa Jallow", slug="awa-jallow", status="approved")
        db.session.add(self.driver)
        db.session.commit()
        self.car = Vehicle(make="Toyota", model="Yaris", year=2020, daily_rate=2500,
                           deposit=7500, operator_id=self.driver.id, is_active=True,
                           service_mode="rental")
        db.session.add(self.car)
        db.session.commit()
        self.start = date.today() + timedelta(days=5)
        self.end = self.start + timedelta(days=3)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def booking(self, status="confirmed", kind="rental", **extra):
        booking = Booking(reference=Booking.new_reference(), booking_type=kind,
                          vehicle_id=self.car.id if kind == "rental" else None,
                          operator_id=self.driver.id, customer_name="Fatou Ceesay",
                          email="fatou@example.com", phone="+2207000000",
                          pickup_location="Kololi", dropoff_location="Kololi",
                          start_date=self.start, end_date=self.end, total_price=7500,
                          deposit_amount=7500, status=status, **extra)
        db.session.add(booking)
        db.session.commit()
        return booking

    def customer(self, booking):
        """A client that has proved the reference and email, as the lookup does,
        and has the booking page open (which is where the form token comes from)."""
        client = self.app.test_client()
        client.post("/booking", data={"reference": booking.reference, "email": booking.email},
                    follow_redirects=True)
        return client

    def token(self, client):
        with client.session_transaction() as session:
            return session.get("_csrf_token", "")

    def cancel(self, client, booking, token=None):
        return client.post(f"/booking/{booking.reference}/cancel",
                           data={"csrf_token": self.token(client) if token is None else token},
                           follow_redirects=True)


class WhatACustomerCanCancel(CancellationCase):
    def test_a_confirmed_rental_can_be_called_off_and_the_dates_come_free(self):
        booking = self.booking()
        self.assertFalse(self.car.is_available(self.start, self.end))

        page = self.cancel(self.customer(booking), booking)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Your booking is cancelled", page.get_data(as_text=True))
        self.assertEqual(db.session.get(Booking, booking.id).status, "cancelled")
        self.assertTrue(db.session.get(Vehicle, self.car.id).is_available(self.start, self.end))

    def test_a_request_the_driver_has_not_answered_yet(self):
        booking = self.booking(status="pending")
        self.cancel(self.customer(booking), booking)
        self.assertEqual(db.session.get(Booking, booking.id).status, "cancelled")

    def test_a_booked_journey_as_well_as_a_rental(self):
        for kind in ("transfer", "ride"):
            booking = self.booking(kind=kind)
            self.cancel(self.customer(booking), booking)
            self.assertEqual(db.session.get(Booking, booking.id).status, "cancelled", kind)

    def test_the_page_offers_it_only_while_it_is_possible(self):
        for status, offered in [("pending", True), ("confirmed", True),
                                ("completed", False), ("cancelled", False)]:
            booking = self.booking(status=status)
            page = self.customer(booking).get(f"/booking/{booking.reference}").get_data(as_text=True)
            self.assertEqual("/cancel" in page, offered, status)

    def test_nobody_earns_commission_on_a_cancelled_booking(self):
        booking = self.booking(status="confirmed")
        self.cancel(self.customer(booking), booking)
        self.assertEqual(CommissionEntry.query.count(), 0)


class WhatIsRefused(CancellationCase):
    def test_a_stranger_with_the_reference_cannot_cancel(self):
        booking = self.booking()
        stranger = self.app.test_client()
        stranger.get("/booking")   # renders the lookup page, so it has a token
        response = stranger.post(f"/booking/{booking.reference}/cancel",
                                 data={"csrf_token": self.token(stranger)})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/booking", response.headers["Location"])
        self.assertEqual(db.session.get(Booking, booking.id).status, "confirmed")

    def test_a_wrong_email_address_never_opens_the_booking(self):
        booking = self.booking()
        client = self.app.test_client()
        client.post("/booking", data={"reference": booking.reference, "email": "someone@else.com"})
        self.cancel(client, booking)
        self.assertEqual(db.session.get(Booking, booking.id).status, "confirmed")

    def test_a_post_from_another_site_is_refused(self):
        booking = self.booking()
        client = self.customer(booking)
        page = self.cancel(client, booking, token="not-the-token")
        self.assertIn("had expired", page.get_data(as_text=True))
        self.assertEqual(db.session.get(Booking, booking.id).status, "confirmed")

    def test_a_finished_rental_cannot_be_cancelled_after_the_fact(self):
        booking = self.booking(status="completed")
        page = self.cancel(self.customer(booking), booking)
        self.assertIn("can no longer be cancelled here", page.get_data(as_text=True))
        self.assertEqual(db.session.get(Booking, booking.id).status, "completed")

    def test_cancelling_twice_changes_nothing_the_second_time(self):
        booking = self.booking()
        client = self.customer(booking)
        self.cancel(client, booking)
        page = self.cancel(client, booking)
        self.assertIn("can no longer be cancelled here", page.get_data(as_text=True))
        self.assertEqual(db.session.get(Booking, booking.id).status, "cancelled")

    def test_an_on_demand_ride_keeps_its_own_cancel_button(self):
        """That one also releases the driver holding the trip, so it stays where
        it is rather than being handled here."""
        booking = self.booking(status="pending", kind="ride", request_token="tok-1")
        client = self.customer(booking)
        response = client.post(f"/booking/{booking.reference}/cancel",
                               data={"csrf_token": self.token(client)})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/ride/track/{booking.reference}", response.headers["Location"])
        self.assertEqual(db.session.get(Booking, booking.id).status, "pending")


if __name__ == "__main__":
    unittest.main()
