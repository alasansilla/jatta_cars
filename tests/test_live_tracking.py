"""A customer may see a driver's position only when it is real, fresh and theirs.

The rule this file defends: a position shown on the tracking page must have come
from an authenticated driver posting their own GPS fix a moment ago, for this
trip. Nothing is interpolated, guessed at, or carried over from another job. A
stale fix is withheld rather than shown, because a pin in the wrong place is
worse than no pin.

Every fixture here is built in an in-memory database. Nothing touches the real
one, and no operator invented for a test can reach it.
"""
import secrets
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import create_app
from app.models import (
    Booking, DriverState, Operator, OperatorFare, Vehicle, db,
)
from app.settings import save_settings
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    SECRET_KEY = "test-only-" + "t" * 40
    STORAGE_BACKEND = "local"
    # Never let a test reach a real provider or spend a real key.
    GEOCODER_URL = ""
    REVERSE_GEOCODER_URL = ""
    ROUTER_URL = ""


class LiveTrackingCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})

        self.operator = self._operator("Kololi Cabs", "kololi@example.com")
        self.rival = self._operator("Rival Rides", "rival@example.com")
        self.car = Vehicle(make="Toyota", model="Corolla", year=2021,
                           daily_rate=10000, deposit=0, is_active=True,
                           operator_id=self.operator.id)
        db.session.add(self.car)
        db.session.commit()
        self.booking = self._ride(self.operator, self.car, status="confirmed")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _operator(self, name, email, password="driver-password"):
        operator = Operator(name=name, slug=Operator.make_slug(name), email=email,
                            status="approved", phone="+220700000",
                            contact_name=f"{name} driver")
        operator.set_password(password)
        db.session.add(operator)
        db.session.commit()
        return operator

    def _ride(self, operator, vehicle, status="confirmed", reference=None):
        today = date.today()
        booking = Booking(
            reference=reference or Booking.new_reference(), booking_type="ride",
            request_token=secrets.token_urlsafe(16), requested_at=datetime.utcnow(),
            operator_id=operator.id, vehicle_id=vehicle.id if vehicle else None,
            customer_name="Awa Ceesay", email="awa@example.com", phone="+220700111",
            pickup_location="Kololi", dropoff_location="Banjul",
            pickup_address="Senegambia", dropoff_address="Airport",
            pickup_at=datetime.utcnow(), start_date=today, end_date=today,
            total_price=Decimal("1500.00"), deposit_amount=0, status=status)
        db.session.add(booking)
        db.session.commit()
        return booking

    def _customer(self, booking):
        """A browser that has just made this booking."""
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["booking_reference"] = booking.reference
        return client

    def _driver(self, operator, password="driver-password"):
        client = self.app.test_client()
        client.post("/operator/login",
                    data={"email": operator.email, "password": password})
        return client

    def _csrf(self, client):
        client.get("/operator/")
        with client.session_transaction() as session:
            return session.get("_csrf_token")

    def _post_fix(self, client, lat, lng, booking=None):
        booking = booking or self.booking
        return client.post("/operator/drive/location", json={
            "lat": lat, "lng": lng, "booking_id": booking.id,
        }, headers={"X-CSRF-Token": self._csrf(client)})

    def _hold(self, booking=None):
        """This driver holds the (confirmed) trip, with Driving mode open."""
        booking = booking or self.booking
        state = db.session.get(DriverState, self.operator.id)
        if state is None:
            state = DriverState(operator_id=self.operator.id)
            db.session.add(state)
        state.active_booking_id = booking.id
        state.vehicle_id = self.car.id
        state.updated_at = datetime.utcnow()
        db.session.commit()
        return state

    def _status(self, client, booking=None):
        booking = booking or self.booking
        return client.get(f"/ride/status/{booking.reference}")


# --- a position must come from a real driver update -------------------------

class PositionProvenanceTests(LiveTrackingCase):
    def test_no_position_before_any_driver_update(self):
        """The driver's details are known; where they are is not."""
        body = self._status(self._customer(self.booking)).get_json()
        self.assertIsNotNone(body["driver"])
        self.assertNotIn("lat", body["driver"])
        self.assertNotIn("lng", body["driver"])

    def test_the_position_shown_is_exactly_what_the_driver_posted(self):
        driver = self._driver(self.operator)
        self._hold()
        self.assertEqual(self._post_fix(driver, 13.4412, -16.6890).status_code, 200)

        body = self._status(self._customer(self.booking)).get_json()
        self.assertAlmostEqual(body["driver"]["lat"], 13.4412, places=4)
        self.assertAlmostEqual(body["driver"]["lng"], -16.6890, places=4)
        self.assertIn("updated_at", body["driver"],
                      "a position without a timestamp cannot be judged fresh")

    def test_a_stale_fix_is_withheld_rather_than_shown(self):
        """A pin in the wrong place is worse than no pin."""
        state = DriverState(operator_id=self.operator.id, vehicle_id=self.car.id,
                            active_booking_id=self.booking.id,
                            lat=13.44, lng=-16.68,
                            location_at=datetime.utcnow() - timedelta(minutes=5),
                            updated_at=datetime.utcnow() - timedelta(minutes=5))
        db.session.add(state)
        db.session.commit()

        driver = self._status(self._customer(self.booking)).get_json()["driver"]
        self.assertNotIn("lat", driver)
        self.assertIsNotNone(driver["name"], "the driver is still known, just not located")

    def test_a_position_from_another_trip_never_leaks_in(self):
        """The driver is out on someone else's job; this customer sees no pin."""
        other = self._ride(self.operator, self.car, status="confirmed")
        state = DriverState(operator_id=self.operator.id, vehicle_id=self.car.id,
                            active_booking_id=other.id, lat=13.44, lng=-16.68,
                            updated_at=datetime.utcnow())
        db.session.add(state)
        db.session.commit()

        driver = self._status(self._customer(self.booking)).get_json()["driver"]
        self.assertNotIn("lat", driver)

    def test_no_position_once_the_trip_is_over(self):
        for finished in ("completed", "cancelled"):
            booking = self._ride(self.operator, self.car, status=finished)
            db.session.add(DriverState(operator_id=self.rival.id,
                                       active_booking_id=booking.id,
                                       lat=13.44, lng=-16.68,
                                       updated_at=datetime.utcnow()))
            db.session.commit()
            body = self._status(self._customer(booking), booking).get_json()
            self.assertIsNone(body["driver"], f"driver exposed on a {finished} trip")
            db.session.query(DriverState).delete()
            db.session.commit()


# --- only an authenticated driver may report a position ---------------------

class LocationAuthTests(LiveTrackingCase):
    def test_a_stranger_cannot_post_a_position(self):
        anonymous = self.app.test_client()
        response = anonymous.post("/operator/drive/location",
                                  json={"lat": 13.44, "lng": -16.68,
                                        "vehicle_id": self.car.id})
        self.assertNotEqual(response.status_code, 200)
        self.assertIsNone(db.session.get(DriverState, self.operator.id),
                          "an unauthenticated post created driver state")

    def test_a_position_post_needs_the_csrf_token(self):
        driver = self._driver(self.operator)
        response = driver.post("/operator/drive/location",
                               json={"lat": 13.44, "lng": -16.68,
                                     "vehicle_id": self.car.id})
        self.assertEqual(response.status_code, 400)

    def test_impossible_coordinates_are_refused(self):
        driver = self._driver(self.operator)
        for lat, lng in [(999, 0), (0, 999), ("north", 0), (None, None),
                         (float("nan"), 0), (float("inf"), 0)]:
            response = self._post_fix(driver, lat, lng)
            self.assertEqual(response.status_code, 400, f"accepted {lat},{lng}")

        state = db.session.get(DriverState, self.operator.id)
        self.assertTrue(state is None or state.lat is None,
                        "rubbish coordinates were stored")

    def test_a_driver_cannot_claim_another_operators_vehicle(self):
        rival_car = Vehicle(make="Kia", model="Rio", year=2020, daily_rate=9000,
                            deposit=0, is_active=True, operator_id=self.rival.id)
        db.session.add(rival_car)
        db.session.commit()

        rival_trip = self._ride(self.rival, rival_car, status="confirmed")
        db.session.add(DriverState(operator_id=self.rival.id, vehicle_id=rival_car.id,
                                   active_booking_id=rival_trip.id, updated_at=datetime.utcnow()))
        db.session.commit()
        driver = self._driver(self.operator)
        response = self._post_fix(driver, 13.44, -16.68, booking=rival_trip)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(db.session.get(DriverState, self.rival.id).lat)

    def test_a_real_fix_is_timestamped_by_the_server(self):
        """The clock is ours. A driver cannot backdate or postdate a fix."""
        driver = self._driver(self.operator)
        self._hold()
        before = datetime.utcnow() - timedelta(seconds=1)
        self.assertEqual(self._post_fix(driver, 13.44, -16.68).status_code, 200)
        db.session.expire_all()
        state = db.session.get(DriverState, self.operator.id)
        self.assertIsNotNone(state.location_at)
        self.assertGreaterEqual(state.location_at, before)
        self.assertLessEqual(state.location_at, datetime.utcnow() + timedelta(seconds=1))


# --- only the customer who booked may watch ---------------------------------

class TrackingPrivacyTests(LiveTrackingCase):
    def test_a_stranger_cannot_read_a_trip(self):
        anonymous = self.app.test_client()
        self.assertEqual(self._status(anonymous).status_code, 404)
        self.assertEqual(
            anonymous.get(f"/ride/track/{self.booking.reference}").status_code, 404)

    def test_another_customers_reference_does_not_open_this_trip(self):
        other = self._ride(self.operator, self.car)
        intruder = self._customer(other)          # holds a different reference
        self.assertEqual(self._status(intruder).status_code, 404)

    def test_knowing_the_reference_is_not_enough(self):
        """Guessing a reference must not be a way in without the session."""
        guesser = self.app.test_client()
        with guesser.session_transaction() as session:
            session["booking_reference"] = "JC-NOPE01"
        self.assertEqual(self._status(guesser).status_code, 404)

    def test_the_customer_gets_driver_details_only_once_accepted(self):
        pending = self._ride(self.operator, self.car, status="pending")
        body = self._status(self._customer(pending), pending).get_json()
        self.assertIsNone(body["driver"],
                          "driver contact exposed before the trip was accepted")

    def test_tracking_responses_are_never_cached(self):
        """A shared phone must not keep a stranger's trip in its back button."""
        response = self._status(self._customer(self.booking))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_the_customer_never_sees_another_operators_contact(self):
        body = self._status(self._customer(self.booking)).get_json()
        self.assertNotIn("Rival", body["driver"]["name"])


if __name__ == "__main__":
    unittest.main()
