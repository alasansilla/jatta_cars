"""Driving mode: what a driver may see and do with an on-demand ride.

Every trip here is created the way a customer creates one — /ride/estimate then
/ride/request — with the routing provider patched out, so the booking carries
exactly the fields production writes (request token, requested_at, the chosen
driver's own price). Drivers sign in with email and password.

Nothing here reaches a real routing or SMS provider. The thread race uses a
file-backed SQLite database in a temporary directory that is removed afterwards.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from app.models import (
    Booking, CommissionEntry, DriverState, Operator, OperatorFare, Vehicle, db,
)
from app.routing import Route
from werkzeug.security import generate_password_hash
from app.settings import save_settings
from tests.test_marketplace import MarketplaceCase, TestConfig

PICKUP = dict(pickup_lat=13.40, pickup_lng=-16.70)
DROPOFF = dict(dropoff_lat=13.50, dropoff_lng=-16.60)
ROUTE = Route(3000, 600, "test", [[13.40, -16.70], [13.50, -16.60]])
CUSTOMER = dict(pickup_address="Kololi hotel", dropoff_address="Bakau market",
                customer_name="Awa Ceesay", email="awa@example.com", phone="+220700111")

# Prices for a 3 km trip, each from the driver's own fare.
KOLOLI_PRICE = Decimal("110.00")   # 50 + 20/km
RIVAL_PRICE = Decimal("115.00")    # 40 + 25/km
THIRD_PRICE = Decimal("120.00")    # 90 + 10/km


_HASHES = {}


def _fast_hash(password, method=None, **_):
    """A real pbkdf2 hash with few iterations, made once per password.

    Only the cost of creating fixtures is reduced; signing in still goes through
    the application's own check_password_hash unchanged.
    """
    if password not in _HASHES:
        _HASHES[password] = generate_password_hash(password, method="pbkdf2:sha256:1000")
    return _HASHES[password]


def _routing():
    """Both patches every estimate needs; never touches a real provider."""
    return (patch("app.dispatch.routing.routing_available", return_value=True),
            patch("app.dispatch.routing.route", return_value=ROUTE))


class DriverConsoleCase(MarketplaceCase):
    """Three approved drivers online with fresh fixes and per-km ride fares."""

    def setUp(self):
        hashing = patch("app.models.generate_password_hash", side_effect=_fast_hash)
        hashing.start()
        self.addCleanup(hashing.stop)
        super().setUp()
        # Kololi Cabs: the car from MarketplaceCase, plus its fare turned into a ride fare.
        self.car.operator_id = self.operator.id
        self.car.seats = 4
        self.fare.kind = "ride"
        self.fare.pricing_model = "distance"
        self.fare.base_price = Decimal("50")
        self.fare.per_km = Decimal("20")
        self.fare.price = None
        self.fare.seats = None
        db.session.commit()

        self.rival_car = self._car(self.rival, "Kia", "Rio")
        self.rival_fare = self._ride_fare(self.rival, "40", "25")

        self.third = self._operator("Third Taxi", "third@example.com")
        self.third_car = self._car(self.third, "Nissan", "Almera")
        self.third_fare = self._ride_fare(self.third, "90", "10")

        for driver, car in ((self.operator, self.car), (self.rival, self.rival_car),
                            (self.third, self.third_car)):
            db.session.add(DriverState(operator_id=driver.id, vehicle_id=car.id,
                                       available=True, active_booking_id=None,
                                       lat=13.41, lng=-16.69,
                                       updated_at=datetime.utcnow()))
        db.session.commit()

        self.operator_id = self.operator.id
        self.rival_id = self.rival.id
        self.third_id = self.third.id

    # --- fixtures -------------------------------------------------------------

    def _car(self, driver, make, model, active=True):
        car = Vehicle(make=make, model=model, year=2020, daily_rate=9000, deposit=0,
                      is_active=active, operator_id=driver.id, seats=4)
        db.session.add(car)
        db.session.commit()
        return car

    def _ride_fare(self, driver, base, per_km):
        fare = OperatorFare(operator_id=driver.id, kind="ride", title=f"{driver.name} rides",
                            from_location="Anywhere", to_location="Anywhere",
                            pricing_model="distance", base_price=Decimal(base),
                            per_km=Decimal(per_km), is_active=True)
        db.session.add(fare)
        db.session.commit()
        return fare

    def _fresh(self, model, ident):
        db.session.expire_all()
        return db.session.get(model, ident)

    def _state(self, operator_id):
        return self._fresh(DriverState, operator_id)

    def _booking(self, booking_id):
        return self._fresh(Booking, booking_id)

    # --- the customer -----------------------------------------------------------

    def _customer(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/ride").status_code, 200)
        with client.session_transaction() as session:
            client.csrf = session["_csrf_token"]
        return client

    def _cpost(self, client, path, data=None, token=None):
        return client.post(path, json=data if data is not None else {},
                           headers={"X-CSRF-Token": token or client.csrf})

    def _estimate(self, client):
        available, route = _routing()
        with available, route:
            response = self._cpost(client, "/ride/estimate",
                                   dict(PICKUP, **DROPOFF, passengers=2))
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()

    def _request(self, client=None, fare=None, vehicle=None, price=KOLOLI_PRICE):
        """A real on-demand request from a fresh customer. Returns (client, booking)."""
        client = client or self._customer()
        quote = self._estimate(client)
        fare = fare or self.fare
        vehicle = vehicle or self.car
        response = self._cpost(client, "/ride/request", dict(
            CUSTOMER, token=quote["token"], fare_id=fare.id, vehicle_id=vehicle.id,
            expected_price=float(price)))
        self.assertEqual(response.status_code, 201, response.data)
        with client.session_transaction() as session:
            reference = session["booking_reference"]
        db.session.expire_all()
        booking = Booking.query.filter_by(reference=reference).one()
        return client, booking

    def _status(self, client, reference):
        response = client.get(f"/ride/status/{reference}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()

    # --- the driver -------------------------------------------------------------

    def _driver(self, email="kololi@example.com"):
        client = self._sign_in_operator(email)
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
        self.assertTrue(token, "driver sign-in did not start a session")
        client.csrf = token
        return client

    def _dpost(self, client, path, data=None):
        return client.post(path, json=data if data is not None else {},
                           headers={"X-CSRF-Token": client.csrf})

    def _accept(self, client, booking_id):
        return self._dpost(client, "/operator/drive/accept", {"booking_id": booking_id})

    def _stage(self, client, booking_id, status):
        return self._dpost(client, "/operator/drive/status",
                           {"booking_id": booking_id, "status": status})

    def _drive_state(self, client):
        response = client.get("/operator/drive/state")
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()


# --- who may open Driving mode ------------------------------------------------

class AccessTests(DriverConsoleCase):
    def test_approved_driver_can_open_driving_mode(self):
        driver = self._driver()
        self.assertEqual(driver.get("/operator/drive").status_code, 200)
        self.assertEqual(driver.get("/operator/drive/state").status_code, 200)

    def test_anonymous_is_redirected_to_sign_in(self):
        anonymous = self.app.test_client()
        for path in ("/operator/drive", "/operator/drive/state"):
            response = anonymous.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/driver/sign-in", response.location, path)

    def test_pending_account_is_sent_to_status(self):
        applicant = self._operator("New Applicant", "new@example.com", status="pending")
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = applicant.id
        for path in ("/operator/drive", "/operator/drive/state"):
            response = client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/driver/status", response.location, path)

    def test_suspended_driver_loses_access_immediately(self):
        driver = self._driver()
        self.assertEqual(driver.get("/operator/drive/state").status_code, 200)
        operator = self._fresh(Operator, self.operator_id)
        operator.status = "suspended"
        db.session.commit()
        for path in ("/operator/drive", "/operator/drive/state"):
            response = driver.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/driver/status", response.location, path)

    def test_suspended_driver_cannot_accept(self):
        _, booking = self._request()
        driver = self._driver()
        operator = self._fresh(Operator, self.operator_id)
        operator.status = "suspended"
        db.session.commit()
        response = self._accept(driver, booking.id)
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "pending")


# --- what the driver sees -------------------------------------------------------

class StateTests(DriverConsoleCase):
    def test_pending_request_hides_the_customer(self):
        _, booking = self._request()
        state = self._drive_state(self._driver())
        job = state["job"]
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], booking.id)
        self.assertEqual(job["status"], "pending")
        self.assertIsNone(job["customer"])
        self.assertIsInstance(job["expires_in"], int)
        self.assertGreater(job["expires_in"], 0)
        self.assertLessEqual(job["expires_in"], self.app.config["DRIVER_ACCEPT_SECONDS"])
        self.assertIsNone(job["next"])
        rendered = json.dumps(state)
        for private in ("Awa", "Ceesay", "awa@example.com", "700111"):
            self.assertNotIn(private, rendered, "a pending request leaked the customer")

    def test_accepted_request_shows_the_customer_and_one_next_step(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        job = self._drive_state(driver)["job"]
        self.assertEqual(job["status"], "accepted")
        self.assertEqual(job["customer"]["name"], "Awa Ceesay")
        self.assertEqual(job["customer"]["phone"], self._booking(booking.id).phone)
        self.assertTrue(job["customer"]["phone"])
        self.assertIsInstance(job["next"], dict, "expected exactly one next action")
        self.assertEqual(job["next"]["status"], "arriving")
        self.assertIsNone(job["expires_in"])

    def test_another_driver_sees_nothing_of_the_trip(self):
        _, booking = self._request()
        rival = self._driver("rival@example.com")
        state = self._drive_state(rival)
        self.assertIsNone(state["job"])
        self.assertNotIn(booking.reference, json.dumps(state))


# --- trip stages ----------------------------------------------------------------

class StageTests(DriverConsoleCase):
    def setUp(self):
        super().setUp()
        self.customer, self.trip = self._request()
        self.driver = self._driver()
        self.assertEqual(self._accept(self.driver, self.trip.id).status_code, 200)

    def test_from_accepted_only_arriving_is_allowed(self):
        for skip in ("in_progress", "completed", "accepted", "pending", "cancelled", "confirmed"):
            response = self._stage(self.driver, self.trip.id, skip)
            self.assertEqual(response.status_code, 409, skip)
            self.assertEqual(self._booking(self.trip.id).status, "accepted", skip)
        self.assertEqual(self._stage(self.driver, self.trip.id, "arriving").status_code, 200)
        self.assertEqual(self._booking(self.trip.id).status, "arriving")

    def test_no_stage_can_be_skipped_later_either(self):
        self.assertEqual(self._stage(self.driver, self.trip.id, "arriving").status_code, 200)
        self.assertEqual(self._stage(self.driver, self.trip.id, "completed").status_code, 409)
        self.assertEqual(self._stage(self.driver, self.trip.id, "arriving").status_code, 409)
        self.assertEqual(self._stage(self.driver, self.trip.id, "in_progress").status_code, 200)
        self.assertEqual(self._stage(self.driver, self.trip.id, "arriving").status_code, 409)
        self.assertEqual(self._booking(self.trip.id).status, "in_progress")

    def test_stage_without_booking_id_is_400(self):
        for body in ({"status": "arriving"}, {"booking_id": None, "status": "arriving"},
                     {"booking_id": True, "status": "arriving"},
                     {"booking_id": "abc", "status": "arriving"}):
            response = self._dpost(self.driver, "/operator/drive/status", body)
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(self._booking(self.trip.id).status, "accepted")

    def test_stage_for_another_booking_is_refused(self):
        # A trip belonging to the rival.
        _, other = self._request(fare=self.rival_fare, vehicle=self.rival_car, price=RIVAL_PRICE)
        response = self._stage(self.driver, other.id, "arriving")
        self.assertIn(response.status_code, (404, 409))
        # A trip id that does not exist.
        self.assertIn(self._stage(self.driver, 999999, "arriving").status_code, (404, 409))
        # One of this driver's own scheduled journeys, not the trip they hold.
        scheduled = self._journey(status="confirmed")
        self.assertIn(self._stage(self.driver, scheduled.id, "completed").status_code, (404, 409))
        self.assertEqual(self._booking(other.id).status, "pending")
        self.assertEqual(self._booking(scheduled.id).status, "confirmed")
        self.assertEqual(self._booking(self.trip.id).status, "accepted")

    def test_completing_records_five_percent_and_releases_the_driver(self):
        for stage in ("arriving", "in_progress", "completed"):
            self.assertEqual(self._stage(self.driver, self.trip.id, stage).status_code, 200, stage)
        booking = self._booking(self.trip.id)
        self.assertEqual(booking.status, "completed")
        entries = CommissionEntry.query.filter_by(booking_id=booking.id).all()
        self.assertEqual(len(entries), 1)
        fare = Decimal(str(booking.total_price))
        self.assertEqual(fare, KOLOLI_PRICE)
        self.assertEqual(Decimal(str(entries[0].amount)),
                         (fare * Decimal("0.05")).quantize(Decimal("0.01")))
        self.assertEqual(Decimal(str(entries[0].rate_percent)), Decimal("5"))
        self.assertEqual(entries[0].operator_id, self.operator_id)
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        # A finished trip cannot be advanced or finished again.
        self.assertIn(self._stage(self.driver, self.trip.id, "completed").status_code, (404, 409))
        self.assertEqual(CommissionEntry.query.filter_by(booking_id=booking.id).count(), 1)
        self.assertIsNone(self._drive_state(self.driver)["job"])


# --- accepting ------------------------------------------------------------------

class AcceptTests(DriverConsoleCase):
    def test_stale_gps_cannot_accept(self):
        _, booking = self._request()
        driver = self._driver()
        state = self._state(self.operator_id)
        state.updated_at = datetime.utcnow() - timedelta(minutes=2, seconds=30)
        db.session.commit()
        self.assertEqual(self._accept(driver, booking.id).status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "pending")

    def test_fresh_gps_accepts(self):
        _, booking = self._request()
        driver = self._driver()
        state = self._state(self.operator_id)
        state.updated_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "accepted")
        self.assertEqual(self._state(self.operator_id).active_booking_id, booking.id)

    def test_accepting_twice_is_refused(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        self.assertEqual(self._accept(driver, booking.id).status_code, 409)

    def test_accept_after_the_window_expires_the_request(self):
        customer, booking = self._request()
        driver = self._driver()
        window = self.app.config["DRIVER_ACCEPT_SECONDS"]
        row = self._booking(booking.id)
        row.requested_at = datetime.utcnow() - timedelta(seconds=window + 5)
        db.session.commit()

        response = self._accept(driver, booking.id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "expired")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        self.assertIsNone(self._drive_state(driver)["job"])
        self.assertTrue(self._status(customer, booking.reference)["can_choose_again"])

    def test_the_driver_state_poll_alone_expires_an_old_request(self):
        _, booking = self._request()
        driver = self._driver()
        row = self._booking(booking.id)
        row.requested_at = datetime.utcnow() - timedelta(
            seconds=self.app.config["DRIVER_ACCEPT_SECONDS"] + 1)
        db.session.commit()
        self.assertIsNone(self._drive_state(driver)["job"])
        self.assertEqual(self._booking(booking.id).status, "expired")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        self.assertEqual(self._accept(driver, booking.id).status_code, 409)


# --- declining ------------------------------------------------------------------

class DeclineTests(DriverConsoleCase):
    def test_decline_releases_and_lets_the_customer_choose_again(self):
        customer, booking = self._request()
        driver = self._driver()
        response = self._dpost(driver, "/operator/drive/decline", {"booking_id": booking.id})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._booking(booking.id).status, "declined")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        self.assertTrue(self._status(customer, booking.reference)["can_choose_again"])
        self.assertEqual(self._accept(driver, booking.id).status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "declined")

    def test_decline_without_booking_id_is_400(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._dpost(driver, "/operator/drive/decline", {}).status_code, 400)
        self.assertEqual(self._dpost(driver, "/operator/drive/accept", {}).status_code, 400)
        self.assertEqual(self._booking(booking.id).status, "pending")


# --- other drivers' trips -------------------------------------------------------

class OwnershipTests(DriverConsoleCase):
    def test_rival_cannot_touch_a_pending_trip(self):
        _, booking = self._request()
        rival = self._driver("rival@example.com")
        self.assertIn(self._accept(rival, booking.id).status_code, (403, 404, 409))
        self.assertIn(self._dpost(rival, "/operator/drive/decline",
                                  {"booking_id": booking.id}).status_code, (403, 404, 409))
        self.assertIn(self._stage(rival, booking.id, "arriving").status_code, (403, 404, 409))
        row = self._booking(booking.id)
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.operator_id, self.operator_id)
        self.assertEqual(self._state(self.operator_id).active_booking_id, booking.id)
        self.assertIsNone(self._state(self.rival_id).active_booking_id)

    def test_rival_cannot_touch_an_accepted_trip(self):
        _, booking = self._request()
        self.assertEqual(self._accept(self._driver(), booking.id).status_code, 200)
        rival = self._driver("rival@example.com")
        for stage in ("arriving", "in_progress", "completed"):
            self.assertIn(self._stage(rival, booking.id, stage).status_code, (403, 404, 409))
        self.assertIn(self._dpost(rival, "/operator/drive/decline",
                                  {"booking_id": booking.id}).status_code, (403, 404, 409))
        row = self._booking(booking.id)
        self.assertEqual(row.status, "accepted")
        self.assertEqual(row.operator_id, self.operator_id)
        self.assertEqual(CommissionEntry.query.count(), 0)


# --- customer cancel versus driver accept, in sequence --------------------------

class CancelVersusAcceptTests(DriverConsoleCase):
    def test_cancel_first_then_accept_is_refused(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._cpost(customer, f"/ride/cancel/{booking.reference}").status_code, 200)
        self.assertEqual(self._accept(driver, booking.id).status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "cancelled")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        self.assertIsNone(self._drive_state(driver)["job"])

    def test_accept_first_then_cancel_still_allowed_and_releases(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        self.assertEqual(self._cpost(customer, f"/ride/cancel/{booking.reference}").status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "cancelled")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)
        self.assertEqual(self._stage(driver, booking.id, "arriving").status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "cancelled")
        self.assertIsNone(self._drive_state(driver)["job"])

    def test_cancel_while_arriving_is_allowed(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        self.assertEqual(self._stage(driver, booking.id, "arriving").status_code, 200)
        self.assertEqual(self._cpost(customer, f"/ride/cancel/{booking.reference}").status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "cancelled")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)

    def test_cancel_after_the_trip_started_is_refused(self):
        customer, booking = self._request()
        driver = self._driver()
        for step in ("accept", "arriving", "in_progress"):
            response = self._accept(driver, booking.id) if step == "accept" \
                else self._stage(driver, booking.id, step)
            self.assertEqual(response.status_code, 200, step)
        self.assertEqual(self._cpost(customer, f"/ride/cancel/{booking.reference}").status_code, 409)
        self.assertEqual(self._booking(booking.id).status, "in_progress")
        self.assertEqual(self._state(self.operator_id).active_booking_id, booking.id)


# --- going offline --------------------------------------------------------------

class OfflineTests(DriverConsoleCase):
    def test_offline_with_a_pending_request_declines_it(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._dpost(driver, "/operator/drive/offline").status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "declined")
        state = self._state(self.operator_id)
        self.assertIsNone(state.active_booking_id)
        self.assertFalse(state.available)
        self.assertTrue(self._status(customer, booking.reference)["can_choose_again"])

    def test_offline_during_an_accepted_trip_keeps_the_trip(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        self.assertEqual(self._dpost(driver, "/operator/drive/offline").status_code, 200)
        self.assertEqual(self._booking(booking.id).status, "accepted")
        state = self._state(self.operator_id)
        self.assertEqual(state.active_booking_id, booking.id)
        self.assertNotIn(self._status(customer, booking.reference)["status"],
                         ("cancelled", "declined"))
        self.assertEqual(self._stage(driver, booking.id, "arriving").status_code, 200)


# --- the customer choosing again ------------------------------------------------

class ChooseAgainTests(DriverConsoleCase):
    def _declined(self):
        customer, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._dpost(driver, "/operator/drive/decline",
                                     {"booking_id": booking.id}).status_code, 200)
        # The driver who said no comes straight back online; they must still not
        # be offered for the same trip.
        state = self._state(self.operator_id)
        state.available = True
        state.updated_at = datetime.utcnow()
        db.session.commit()
        return customer, booking

    def _choices(self, customer, reference):
        response = self._cpost(customer, f"/ride/choices/{reference}")
        return response

    def test_choices_exclude_the_driver_who_declined(self):
        customer, booking = self._declined()
        response = self._choices(customer, booking.reference)
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        drivers = {card["driver_id"] for card in body["choices"]}
        self.assertEqual(drivers, {self.rival_id, self.third_id})
        self.assertTrue(body["token"])

    def test_choose_again_moves_the_same_booking_at_the_new_drivers_price(self):
        customer, booking = self._declined()
        body = self._choices(customer, booking.reference).get_json()
        card = next(c for c in body["choices"] if c["driver_id"] == self.rival_id)
        self.assertEqual(Decimal(str(card["price"])), RIVAL_PRICE)
        response = self._cpost(customer, f"/ride/choose-again/{booking.reference}", dict(
            token=body["token"], fare_id=card["fare_id"], vehicle_id=card["vehicle_id"],
            expected_price=card["price"]))
        self.assertEqual(response.status_code, 200, response.data)

        moved = self._booking(booking.id)
        self.assertEqual(moved.reference, booking.reference)
        self.assertEqual(Booking.query.count(), 1)
        self.assertEqual(moved.status, "pending")
        self.assertEqual(moved.operator_id, self.rival_id)
        self.assertEqual(moved.vehicle_id, self.rival_car.id)
        self.assertEqual(moved.fare_id, self.rival_fare.id)
        self.assertEqual(Decimal(str(moved.total_price)), RIVAL_PRICE)
        self.assertEqual(self._state(self.rival_id).active_booking_id, booking.id)
        self.assertIsNone(self._state(self.operator_id).active_booking_id)

        # The new driver sees it; the old one cannot accept it.
        self.assertEqual(self._drive_state(self._driver("rival@example.com"))["job"]["id"],
                         booking.id)
        self.assertIn(self._accept(self._driver(), booking.id).status_code, (404, 409))

    def test_choose_again_refuses_the_driver_who_declined(self):
        customer, booking = self._declined()
        body = self._choices(customer, booking.reference).get_json()
        response = self._cpost(customer, f"/ride/choose-again/{booking.reference}", dict(
            token=body["token"], fare_id=self.fare.id, vehicle_id=self.car.id,
            expected_price=float(KOLOLI_PRICE)))
        self.assertNotEqual(response.status_code, 200)
        row = self._booking(booking.id)
        self.assertEqual(row.status, "declined")
        self.assertIsNone(self._state(self.operator_id).active_booking_id)

    def test_bad_token_or_selection_is_400(self):
        customer, booking = self._declined()
        body = self._choices(customer, booking.reference).get_json()
        card = next(c for c in body["choices"] if c["driver_id"] == self.rival_id)
        good = dict(token=body["token"], fare_id=card["fare_id"],
                    vehicle_id=card["vehicle_id"], expected_price=card["price"])
        bad = [
            dict(good, token="not-the-token"),
            dict(good, token=None),
            {k: v for k, v in good.items() if k != "token"},
            {k: v for k, v in good.items() if k != "fare_id"},
            {k: v for k, v in good.items() if k != "vehicle_id"},
            {k: v for k, v in good.items() if k != "expected_price"},
            dict(good, fare_id=True),
            dict(good, vehicle_id=str(card["vehicle_id"]) + ".5"),
            dict(good, expected_price="abc"),
            dict(good, expected_price=True),
        ]
        for data in bad:
            response = self._cpost(customer, f"/ride/choose-again/{booking.reference}", data)
            self.assertEqual(response.status_code, 400, data)
        row = self._booking(booking.id)
        self.assertEqual(row.status, "declined")
        self.assertEqual(row.operator_id, self.operator_id)
        for operator_id in (self.rival_id, self.third_id):
            self.assertIsNone(self._state(operator_id).active_booking_id)

    def test_one_token_cannot_reserve_two_drivers(self):
        customer, booking = self._declined()
        body = self._choices(customer, booking.reference).get_json()
        cards = {c["driver_id"]: c for c in body["choices"]}
        with customer.session_transaction() as session:
            saved_rechoice = dict(session["rechoice"])

        def choose(card):
            return self._cpost(customer, f"/ride/choose-again/{booking.reference}", dict(
                token=body["token"], fare_id=card["fare_id"], vehicle_id=card["vehicle_id"],
                expected_price=card["price"]))

        self.assertEqual(choose(cards[self.rival_id]).status_code, 200)
        self.assertNotEqual(choose(cards[self.third_id]).status_code, 200)

        # Replaying the old session (the token put back) must not work either.
        with customer.session_transaction() as session:
            session["rechoice"] = saved_rechoice
        self.assertNotEqual(choose(cards[self.third_id]).status_code, 200)

        db.session.expire_all()
        holders = DriverState.query.filter_by(active_booking_id=booking.id).all()
        self.assertEqual([h.operator_id for h in holders], [self.rival_id])
        self.assertEqual(self._booking(booking.id).operator_id, self.rival_id)
        self.assertIsNone(self._state(self.third_id).active_booking_id)

    def test_silent_accepted_driver_lets_the_customer_choose_again(self):
        customer, booking = self._request()
        self.assertEqual(self._accept(self._driver(), booking.id).status_code, 200)
        state = self._state(self.operator_id)
        state.updated_at = datetime.utcnow() - timedelta(minutes=4)
        db.session.commit()
        status = self._status(customer, booking.reference)
        self.assertTrue(status["can_choose_again"])
        response = self._choices(customer, booking.reference)
        self.assertEqual(response.status_code, 200, response.data)
        drivers = {card["driver_id"] for card in response.get_json()["choices"]}
        self.assertNotIn(self.operator_id, drivers)

    def test_driver_with_fresh_location_cannot_be_replaced(self):
        customer, booking = self._request()
        self.assertEqual(self._accept(self._driver(), booking.id).status_code, 200)
        state = self._state(self.operator_id)
        state.updated_at = datetime.utcnow() - timedelta(seconds=30)
        db.session.commit()
        self.assertFalse(self._status(customer, booking.reference)["can_choose_again"])
        self.assertEqual(self._choices(customer, booking.reference).status_code, 409)
        row = self._booking(booking.id)
        self.assertEqual((row.status, row.operator_id), ("accepted", self.operator_id))

    def test_pending_request_cannot_be_rechosen(self):
        customer, booking = self._request()
        self.assertFalse(self._status(customer, booking.reference)["can_choose_again"])
        self.assertEqual(self._choices(customer, booking.reference).status_code, 409)


# --- location updates -----------------------------------------------------------

class LocationTests(DriverConsoleCase):
    """Online is a heartbeat; a position is accepted only for an accepted trip."""

    def _beat(self, client, **data):
        body = dict(vehicle_id=self.car.id, available=True)
        body.update(data)
        return self._dpost(client, "/operator/drive/heartbeat", body)

    def _loc(self, client, **data):
        body = dict(lat=13.45, lng=-16.65)
        body.update(data)
        return self._dpost(client, "/operator/drive/location", body)

    def test_heartbeat_goes_online_without_storing_a_position(self):
        driver = self._driver()
        response = self._beat(driver)
        self.assertEqual(response.status_code, 200, response.data)
        state = self._state(self.operator_id)
        self.assertTrue(state.available)
        self.assertIsNone(state.location_at)

    def test_another_drivers_vehicle_is_rejected(self):
        driver = self._driver()
        self.assertEqual(self._beat(driver, vehicle_id=self.rival_car.id).status_code, 400)
        self.assertEqual(self._state(self.operator_id).vehicle_id, self.car.id)

    def test_inactive_vehicle_is_rejected(self):
        parked = self._car(self.operator, "Honda", "Fit", active=False)
        driver = self._driver()
        self.assertEqual(self._beat(driver, vehicle_id=parked.id).status_code, 400)
        self.assertEqual(self._state(self.operator_id).vehicle_id, self.car.id)

    def test_position_without_an_accepted_trip_is_refused(self):
        driver = self._driver()
        self.assertEqual(self._loc(driver).status_code, 409)
        _, booking = self._request()
        self.assertEqual(self._loc(driver, booking_id=booking.id).status_code, 409)
        self.assertIsNone(self._state(self.operator_id).location_at)

    def test_boolean_and_non_finite_coordinates_are_rejected(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        for data in (dict(lat=True), dict(lng=False), dict(lat="nan"), dict(lng="inf"),
                     dict(lat=None), dict(lat=91), dict(lng=-181), dict(lat=[13.4])):
            self.assertEqual(self._loc(driver, booking_id=booking.id, **data).status_code, 400, data)
        raw = '{"lat": NaN, "lng": -16.65, "booking_id": %d}' % booking.id
        response = driver.post("/operator/drive/location", data=raw, content_type="application/json",
                               headers={"X-CSRF-Token": driver.csrf})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._state(self.operator_id).location_at)
        self.assertEqual(self._loc(driver, booking_id=booking.id).status_code, 200)
        self.assertEqual((self._state(self.operator_id).lat, self._state(self.operator_id).lng),
                         (13.45, -16.65))

    def test_available_only_when_json_true(self):
        driver = self._driver()
        for value in ("true", 1, "1", "yes", None, False):
            response = self._beat(driver, available=value)
            self.assertEqual(response.status_code, 200, value)
            self.assertFalse(self._state(self.operator_id).available, value)
        self.assertEqual(self._beat(driver, available=True).status_code, 200)
        self.assertTrue(self._state(self.operator_id).available)

    def test_vehicle_cannot_be_switched_while_holding_a_trip(self):
        spare = self._car(self.operator, "Hyundai", "Accent")
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        response = self._beat(driver, vehicle_id=spare.id, available=True)
        self.assertIn(response.status_code, (200, 400, 409))
        state = self._state(self.operator_id)
        self.assertEqual(state.vehicle_id, self.car.id)
        self.assertEqual(state.active_booking_id, booking.id)
        self.assertFalse(state.available, "a driver on a trip was put back on offer")

    def test_location_without_csrf_is_refused(self):
        driver = self._driver()
        response = driver.post("/operator/drive/location", json=dict(lat=13.45, lng=-16.65))
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._state(self.operator_id).location_at)


# --- the older booking pages ----------------------------------------------------

class BookingDetailTests(DriverConsoleCase):
    def _form(self, client, path, **data):
        data["csrf_token"] = client.csrf
        return client.post(path, data=data)

    def test_status_form_cannot_accept_an_on_demand_ride(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(driver.get(f"/operator/bookings/{booking.id}").status_code, 200)
        for status in ("confirmed", "accepted", "completed", "cancelled"):
            response = self._form(driver, f"/operator/bookings/{booking.id}/status", status=status)
            self.assertEqual(response.status_code, 302, status)
            self.assertIn("/operator/drive", response.location, status)
            self.assertEqual(self._booking(booking.id).status, "pending", status)
        self.assertEqual(CommissionEntry.query.count(), 0)

    def test_status_form_cannot_move_an_accepted_ride(self):
        _, booking = self._request()
        driver = self._driver()
        self.assertEqual(self._accept(driver, booking.id).status_code, 200)
        response = self._form(driver, f"/operator/bookings/{booking.id}/status",
                              status="completed")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/operator/drive", response.location)
        self.assertEqual(self._booking(booking.id).status, "accepted")
        self.assertEqual(CommissionEntry.query.count(), 0)

    def test_set_fare_refuses_on_demand_rides(self):
        _, booking = self._request()
        driver = self._driver()
        for status in ("pending", "accepted"):
            if status == "accepted":
                self.assertEqual(self._accept(driver, booking.id).status_code, 200)
            response = self._form(driver, f"/operator/bookings/{booking.id}/fare",
                                  total_price="1")
            self.assertEqual(response.status_code, 302)
            row = self._booking(booking.id)
            self.assertEqual(Decimal(str(row.total_price)), KOLOLI_PRICE, status)
            self.assertEqual(row.quote_basis, "distance", status)


# --- a real race: driver accept against customer cancel --------------------------

class AcceptCancelThreadRaceTests(unittest.TestCase):
    ROUNDS = 10

    def setUp(self):
        hashing = patch("app.models.generate_password_hash", side_effect=_fast_hash)
        hashing.start()
        self.addCleanup(hashing.stop)
        self.directory = tempfile.mkdtemp()
        path = os.path.join(self.directory, "console-race.db")

        class FileConfig(TestConfig):
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"

        self.app = create_app(FileConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})

        driver = Operator(name="Kololi Cabs", slug="kololi-cabs",
                          email="kololi@example.com", status="approved")
        driver.set_password("operator-password")
        db.session.add(driver)
        db.session.commit()
        car = Vehicle(make="Toyota", model="Corolla", year=2021, daily_rate=10000,
                      deposit=0, is_active=True, operator_id=driver.id, seats=4)
        fare = OperatorFare(operator_id=driver.id, kind="ride", title="Rides",
                            from_location="Anywhere", to_location="Anywhere",
                            pricing_model="distance", base_price=Decimal("50"),
                            per_km=Decimal("20"), is_active=True)
        db.session.add_all([car, fare])
        db.session.commit()
        db.session.add(DriverState(operator_id=driver.id, vehicle_id=car.id, available=True,
                                   active_booking_id=None, lat=13.41, lng=-16.69,
                                   updated_at=datetime.utcnow()))
        db.session.commit()
        self.driver_id, self.car_id, self.fare_id = driver.id, car.id, fare.id
        db.session.remove()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        with self.app.app_context():
            db.engine.dispose()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _free_driver(self):
        db.session.remove()
        state = db.session.get(DriverState, self.driver_id)
        state.available = True
        state.active_booking_id = None
        state.updated_at = datetime.utcnow()
        db.session.commit()
        db.session.remove()

    def _request(self):
        customer = self.app.test_client()
        customer.get("/ride")
        with customer.session_transaction() as session:
            token = session["_csrf_token"]
        available, route = _routing()
        with available, route:
            quote = customer.post("/ride/estimate", json=dict(PICKUP, **DROPOFF, passengers=1),
                                  headers={"X-CSRF-Token": token}).get_json()
        response = customer.post("/ride/request", json=dict(
            CUSTOMER, token=quote["token"], fare_id=self.fare_id, vehicle_id=self.car_id,
            expected_price=float(KOLOLI_PRICE)), headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 201, response.data)
        with customer.session_transaction() as session:
            reference = session["booking_reference"]
        db.session.remove()
        booking_id = Booking.query.filter_by(reference=reference).one().id
        db.session.remove()
        return customer, token, reference, booking_id

    def test_accept_and_cancel_at_once_leave_one_consistent_outcome(self):
        driver = self.app.test_client()
        driver.post("/operator/login", data={"email": "kololi@example.com",
                                             "password": "operator-password"})
        with driver.session_transaction() as session:
            driver_token = session["_csrf_token"]

        for round_number in range(self.ROUNDS):
            self._free_driver()
            customer, token, reference, booking_id = self._request()
            barrier = threading.Barrier(2)
            results = {}

            def accept():
                barrier.wait()
                try:
                    results["accept"] = driver.post(
                        "/operator/drive/accept", json={"booking_id": booking_id},
                        headers={"X-CSRF-Token": driver_token}).status_code
                except Exception as error:  # a locked database is a loss, not a pass
                    results["accept"] = repr(error)

            def cancel():
                barrier.wait()
                # Stagger the customer a little more each round so both orders of
                # arrival, and the overlap between them, are exercised.
                time.sleep(round_number * 0.003)
                try:
                    results["cancel"] = customer.post(
                        f"/ride/cancel/{reference}", json={},
                        headers={"X-CSRF-Token": token}).status_code
                except Exception as error:
                    results["cancel"] = repr(error)

            threads = [threading.Thread(target=accept), threading.Thread(target=cancel)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertFalse(any(t.is_alive() for t in threads), "a request never finished")

            db.session.remove()
            booking = db.session.get(Booking, booking_id)
            state = db.session.get(DriverState, self.driver_id)
            context = f"round {round_number}: {results}, final {booking.status}"
            self.assertIn(booking.status, ("accepted", "cancelled"), context)
            if booking.status == "accepted":
                self.assertEqual(state.active_booking_id, booking_id, context)
                self.assertEqual(results.get("accept"), 200, context)
                self.assertNotEqual(results.get("cancel"), 200, context)
            else:
                self.assertIsNone(state.active_booking_id, context)
                self.assertEqual(results.get("cancel"), 200, context)
            # Neither side may be told something that did not stick.
            if results.get("accept") == 200 and booking.status == "cancelled":
                # Accept then cancel in that order is legitimate; the cancel won last.
                self.assertEqual(results.get("cancel"), 200, context)
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
