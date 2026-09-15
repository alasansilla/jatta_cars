"""The customer chooses their driver, and nothing chooses for them.

These tests attack the on-demand ride request from the customer's side:

* a missing, malformed or tampered selection is refused outright (400) and
  never falls back to some driver;
* each driver's own fare prices the trip, and a price the customer was not
  shown is never charged;
* only drivers who can really take the trip are offered or reservable;
* a trip can be booked once per estimate and once per open ride;
* a driver who is taken in the meantime is never swapped for someone else;
* the customer's status view reveals no driver contact details before
  acceptance, and driver names travel as data, not markup.

Everything runs on an in-memory database with routing patched out, so no real
routing or SMS provider is ever contacted.
"""
import json
import os
import re
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app import dispatch as dispatch_module
from app.models import Booking, DriverState, OperatorFare, Vehicle, db
from app.routing import Route
from tests.test_marketplace import MarketplaceCase

DISTANCE_M = 7350          # 7.35 km
DURATION_S = 900
GEOMETRY = [[13.44, -16.68], [13.47, -16.72]]
POINTS = dict(pickup_lat=13.44, pickup_lng=-16.68, dropoff_lat=13.47, dropoff_lng=-16.72)
CUSTOMER = dict(pickup_address="Kololi hotel", dropoff_address="Bakau market",
                customer_name="Awa Ceesay", email="awa@example.com", phone="+220700111")

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "app", "templates", "dispatch")


class RideSelectionCase(MarketplaceCase):
    def setUp(self):
        super().setUp()
        # Driver A: cheaper. Driver B ("rival"): pricier.
        self.operator.contact_name = "Musa Jallow"
        self.operator.phone = "+220 300 1234"
        self.operator.phone_e164 = "+220833001234"
        self.rival.contact_name = "Fatou Bojang"
        self.rival.phone = "+220 700 9876"
        self.rival.phone_e164 = "+220877009876"

        self.car_a = Vehicle(make="Toyota", model="Corolla", year=2019, daily_rate=0,
                             deposit=0, is_active=True, seats=4, operator_id=self.operator.id)
        self.car_b = Vehicle(make="Nissan", model="Almera", year=2020, daily_rate=0,
                             deposit=0, is_active=True, seats=4, operator_id=self.rival.id)
        db.session.add_all([self.car_a, self.car_b])
        db.session.commit()

        self.fare_a = self._ride_fare(self.operator, base="50", per_km="20")
        self.fare_b = self._ride_fare(self.rival, base="100", per_km="35.5")

        now = datetime.utcnow()
        db.session.add_all([
            DriverState(operator_id=self.operator.id, vehicle_id=self.car_a.id, available=True,
                        active_booking_id=None, lat=13.441, lng=-16.681, updated_at=now),
            DriverState(operator_id=self.rival.id, vehicle_id=self.car_b.id, available=True,
                        active_booking_id=None, lat=13.442, lng=-16.682, updated_at=now),
        ])
        db.session.commit()

        self.price_a = self.fare_a.quote(DISTANCE_M)[0]
        self.price_b = self.fare_b.quote(DISTANCE_M)[0]
        self.client, self.csrf = self._customer()

    # --- fixtures -------------------------------------------------------------

    def _operator(self, name, email, status="approved", password=None):
        # No password: these drivers never sign in here, and hashing is slow.
        return super()._operator(name, email, status=status, password=password)

    def _ride_fare(self, operator, base, per_km, seats=None):
        fare = OperatorFare(operator_id=operator.id, kind="ride", pricing_model="distance",
                            title=f"{operator.name} rides", from_location="Anywhere",
                            to_location="Anywhere", base_price=Decimal(base),
                            per_km=Decimal(per_km), seats=seats, is_active=True)
        db.session.add(fare)
        db.session.commit()
        return fare

    def _customer(self):
        client = self.app.test_client()
        client.get("/ride")
        with client.session_transaction() as s:
            token = s["_csrf_token"]
        return client, token

    def _post(self, path, data, client=None, csrf=None):
        return (client or self.client).post(
            path, json=data, headers={"X-CSRF-Token": csrf or self.csrf})

    def _post_raw(self, path, text, client=None, csrf=None):
        return (client or self.client).post(
            path, data=text, content_type="application/json",
            headers={"X-CSRF-Token": csrf or self.csrf})

    def _estimate(self, client=None, csrf=None, passengers=2, driver=None, **extra):
        body = dict(POINTS, passengers=passengers)
        if driver is not None:
            body["driver"] = driver
        body.update(extra)
        route = Route(DISTANCE_M, DURATION_S, "test", GEOMETRY)
        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route", return_value=route):
            return self._post("/ride/estimate", body, client, csrf)

    def _quote(self, client=None, csrf=None, **kwargs):
        response = self._estimate(client, csrf, **kwargs)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertTrue(data.get("token"), data)
        return data

    def _body(self, token, fare, vehicle, expected, **overrides):
        body = dict(CUSTOMER, token=token, fare_id=fare.id, vehicle_id=vehicle.id,
                    expected_price=float(expected))
        body.update(overrides)
        return body

    def _request(self, token, fare, vehicle, expected, client=None, csrf=None, **overrides):
        return self._post("/ride/request", self._body(token, fare, vehicle, expected, **overrides),
                          client, csrf)

    def _rides(self):
        db.session.expire_all()
        return Booking.query.filter_by(booking_type="ride").all()

    def _states(self):
        db.session.expire_all()
        return sorted((s.operator_id, s.vehicle_id, s.available, s.active_booking_id,
                       s.lat, s.lng, s.updated_at) for s in DriverState.query.all())

    def _state(self, operator):
        db.session.expire_all()
        return db.session.get(DriverState, operator.id)

    @staticmethod
    def _driver_ids(choices):
        return [c["driver_id"] for c in choices or []]

    def assertNothingBooked(self, states_before, response=None):
        self.assertEqual(self._rides(), [], response.data if response is not None else None)
        self.assertEqual(self._states(), states_before)


# --- strict selection ----------------------------------------------------------

class StrictSelectionTests(RideSelectionCase):
    def test_a_correct_selection_books_the_chosen_driver(self):
        """Control case: the fixtures really are bookable, so refusals below mean something."""
        quote = self._quote()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 201, response.data)
        (trip,) = self._rides()
        self.assertEqual((trip.operator_id, trip.fare_id, trip.vehicle_id),
                         (self.operator.id, self.fare_a.id, self.car_a.id))
        self.assertEqual(self._state(self.operator).active_booking_id, trip.id)

    def test_missing_selection_fields_are_refused(self):
        quote = self._quote()
        before = self._states()
        for missing in ("fare_id", "vehicle_id", "expected_price"):
            with self.subTest(missing=missing):
                body = self._body(quote["token"], self.fare_a, self.car_a, self.price_a)
                del body[missing]
                response = self._post("/ride/request", body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNothingBooked(before, response)
        with self.subTest(missing="all three"):
            body = dict(CUSTOMER, token=quote["token"])
            response = self._post("/ride/request", body)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertNothingBooked(before, response)

    def test_malformed_ids_are_refused(self):
        quote = self._quote()
        before = self._states()
        fid, vid = self.fare_a.id, self.car_a.id
        bad_values = [True, False, None, 1.5, float(fid) + 0.5, float(fid), -fid, 0,
                      f"{fid}abc", f"{fid} OR 1=1", f"{fid};", "", " ", "one",
                      [fid], {"id": fid}, f"0x{fid}", f"{fid}.0", f"-{fid}", f"+{fid}"]
        for field, good in (("fare_id", fid), ("vehicle_id", vid)):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    body = self._body(quote["token"], self.fare_a, self.car_a, self.price_a)
                    body[field] = value
                    response = self._post("/ride/request", body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertNothingBooked(before, response)

    def test_malformed_prices_are_refused(self):
        quote = self._quote()
        before = self._states()
        price = f"{self.price_a:.2f}"   # "197.00"
        bad_values = [True, False, None, "", "Infinity", "-Infinity", "NaN", "nan", "inf",
                      -float(self.price_a), f"-{price}", "1.97e2", "197e0", "1.97E+2",
                      f"{price} GMD", f"D{price}", "197.001", "1,97", "197.", "0x197",
                      [float(self.price_a)], {"amount": float(self.price_a)}]
        for value in bad_values:
            with self.subTest(value=value):
                body = self._body(quote["token"], self.fare_a, self.car_a, self.price_a)
                body["expected_price"] = value
                response = self._post("/ride/request", body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNothingBooked(before, response)

    def test_non_finite_json_number_literals_are_refused(self):
        """NaN / Infinity as bare JSON literals, not strings."""
        quote = self._quote()
        before = self._states()
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                body = self._body(quote["token"], self.fare_a, self.car_a, 0)
                text = json.dumps(body).replace('"expected_price": 0.0',
                                                f'"expected_price": {literal}')
                self.assertIn(literal, text)
                response = self._post_raw("/ride/request", text)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNothingBooked(before, response)

    def test_a_body_that_is_not_an_object_is_refused(self):
        quote = self._quote()
        before = self._states()
        good = self._body(quote["token"], self.fare_a, self.car_a, self.price_a)
        for text in (json.dumps([good]), json.dumps(list(good.items())), json.dumps("x"),
                     "null", "42", "{not json"):
            with self.subTest(body=text[:40]):
                response = self._post_raw("/ride/request", text)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNothingBooked(before, response)

    def test_a_wrong_token_is_refused(self):
        quote = self._quote()
        before = self._states()
        token = quote["token"]
        for bad in (token[:-1] + ("A" if token[-1] != "A" else "B"), token + "x", "",
                    None, 12345, [token], {"token": token}, token.upper() if token.upper() != token else token.lower()):
            with self.subTest(token=bad):
                response = self._request(bad, self.fare_a, self.car_a, self.price_a)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertNothingBooked(before, response)
        with self.subTest(token="absent"):
            body = self._body(token, self.fare_a, self.car_a, self.price_a)
            del body["token"]
            response = self._post("/ride/request", body)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertNothingBooked(before, response)

    def test_an_expired_token_is_refused(self):
        quote = self._quote()
        before = self._states()
        with self.client.session_transaction() as s:
            stored = dict(s["ride_quote"])
            stored["expires"] = (datetime.utcnow() - timedelta(seconds=1)).timestamp()
            s["ride_quote"] = stored
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingBooked(before, response)

    def test_a_token_from_another_browser_is_refused(self):
        quote = self._quote()
        before = self._states()
        other, other_csrf = self._customer()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a,
                                 client=other, csrf=other_csrf)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingBooked(before, response)

    def test_request_without_any_estimate_is_refused(self):
        before = self._states()
        response = self._request("made-up-token", self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingBooked(before, response)

    def test_fare_of_one_driver_with_car_of_another_is_refused(self):
        quote = self._quote()
        before = self._states()
        for fare, car, price in ((self.fare_a, self.car_b, self.price_a),
                                 (self.fare_a, self.car_b, self.price_b),
                                 (self.fare_b, self.car_a, self.price_b),
                                 (self.fare_b, self.car_a, self.price_a)):
            with self.subTest(fare=fare.id, car=car.id, price=str(price)):
                response = self._request(quote["token"], fare, car, price)
                self.assertIn(response.status_code, (400, 409), response.data)
                self.assertNothingBooked(before, response)

    def test_a_non_ride_fare_cannot_be_used_to_book(self):
        """The fixed-price transfer fare of driver A is not a per-km ride fare."""
        quote = self._quote()
        before = self._states()
        response = self._request(quote["token"], self.fare, self.car_a, Decimal("2500.00"))
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)

    def test_unknown_ids_are_refused(self):
        quote = self._quote()
        before = self._states()
        response = self._post("/ride/request", dict(
            CUSTOMER, token=quote["token"], fare_id=99999, vehicle_id=99999,
            expected_price=float(self.price_a)))
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)


# --- each driver's own price -----------------------------------------------------

class DriverPricingTests(RideSelectionCase):
    def test_every_choice_is_priced_by_that_drivers_own_fare(self):
        self.assertNotEqual(self.price_a, self.price_b)
        quote = self._quote()
        cards = {c["driver_id"]: c for c in quote["choices"]}
        self.assertEqual(set(cards), {self.operator.id, self.rival.id})
        self.assertEqual(Decimal(str(cards[self.operator.id]["price"])), self.price_a)
        self.assertEqual(Decimal(str(cards[self.rival.id]["price"])), self.price_b)
        self.assertEqual(cards[self.operator.id]["fare_id"], self.fare_a.id)
        self.assertEqual(cards[self.operator.id]["vehicle_id"], self.car_a.id)
        self.assertEqual(cards[self.rival.id]["fare_id"], self.fare_b.id)
        self.assertEqual(cards[self.rival.id]["vehicle_id"], self.car_b.id)
        self.assertEqual(self._driver_ids(quote["choices"]), [self.operator.id, self.rival.id],
                         "cheapest first when nobody was asked for")

    def test_choosing_the_pricier_driver_books_at_their_price(self):
        quote = self._quote()
        state_a = next(row for row in self._states() if row[0] == self.operator.id)
        response = self._request(quote["token"], self.fare_b, self.car_b, self.price_b)
        self.assertEqual(response.status_code, 201, response.data)
        (trip,) = self._rides()
        self.assertEqual(trip.operator_id, self.rival.id)
        self.assertEqual(trip.fare_id, self.fare_b.id)
        self.assertEqual(trip.vehicle_id, self.car_b.id)
        self.assertEqual(Decimal(str(trip.total_price)), self.fare_b.quote(DISTANCE_M)[0])
        self.assertEqual(Decimal(str(trip.total_price)), Decimal("360.93"))
        self.assertEqual(self._state(self.rival).active_booking_id, trip.id)
        self.assertFalse(self._state(self.rival).available)
        self.assertIn(state_a, self._states(), "the cheaper driver was not touched")

    def test_the_cheaper_driver_books_at_their_own_price(self):
        quote = self._quote()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 201, response.data)
        (trip,) = self._rides()
        self.assertEqual(Decimal(str(trip.total_price)), self.fare_a.quote(DISTANCE_M)[0])
        self.assertEqual(trip.route_distance_m, DISTANCE_M)

    def test_an_expected_price_lower_than_the_real_price_is_refused_with_fresh_choices(self):
        quote = self._quote()
        before = self._states()
        response = self._request(quote["token"], self.fare_b, self.car_b,
                                 self.price_b - Decimal("0.01"))
        self.assertEqual(response.status_code, 409, response.data)
        data = response.get_json()
        self.assertNothingBooked(before, response)
        cards = {c["driver_id"]: c for c in data["choices"]}
        self.assertIn(self.rival.id, cards)
        self.assertEqual(Decimal(str(cards[self.rival.id]["price"])), self.price_b)
        self.assertTrue(data.get("token"))

        # The customer looks again and accepts the real price: that works.
        again = self._request(data["token"], self.fare_b, self.car_b, self.price_b)
        self.assertEqual(again.status_code, 201, again.data)
        (trip,) = self._rides()
        self.assertEqual(Decimal(str(trip.total_price)), self.price_b)

    def test_the_cheaper_drivers_price_cannot_be_used_for_the_pricier_driver(self):
        quote = self._quote()
        before = self._states()
        response = self._request(quote["token"], self.fare_b, self.car_b, self.price_a)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNothingBooked(before, response)

    def test_an_expected_price_higher_than_the_real_price_is_also_refused(self):
        quote = self._quote()
        before = self._states()
        response = self._request(quote["token"], self.fare_a, self.car_a,
                                 self.price_a + Decimal("1"))
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNothingBooked(before, response)

    def test_a_fare_change_after_the_estimate_needs_the_customer_to_look_again(self):
        quote = self._quote()
        self.fare_b.per_km = Decimal("50")
        db.session.commit()
        new_price = self.fare_b.quote(DISTANCE_M)[0]
        before = self._states()
        response = self._request(quote["token"], self.fare_b, self.car_b, self.price_b)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNothingBooked(before, response)
        cards = {c["driver_id"]: c for c in response.get_json()["choices"]}
        self.assertEqual(Decimal(str(cards[self.rival.id]["price"])), new_price)


# --- who is offered at all -------------------------------------------------------

class AvailabilityTests(RideSelectionCase):
    def _assert_driver_a_unavailable(self, make_unavailable):
        # Estimated while A was free, so the customer holds a real card for A.
        quote = self._quote()
        self.assertIn(self.operator.id, self._driver_ids(quote["choices"]))

        make_unavailable()
        db.session.commit()

        # A new estimate does not offer A, but still offers B.
        fresh_client, fresh_csrf = self._customer()
        fresh = self._estimate(fresh_client, fresh_csrf).get_json()
        self.assertNotIn(self.operator.id, self._driver_ids(fresh["choices"]))
        self.assertIn(self.rival.id, self._driver_ids(fresh["choices"]))

        # Requesting A with the earlier card is refused, and B is not given instead.
        before = self._states()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)
        data = response.get_json()
        self.assertNotIn(self.operator.id, self._driver_ids(data.get("choices")))

        # And with a token from the fresh estimate as well.
        response = self._request(fresh["token"], self.fare_a, self.car_a, self.price_a,
                                 client=fresh_client, csrf=fresh_csrf)
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)

    def test_suspended_driver(self):
        def change():
            self.operator.status = "suspended"
        self._assert_driver_a_unavailable(change)

    def test_pending_driver(self):
        def change():
            self.operator.status = "pending"
        self._assert_driver_a_unavailable(change)

    def test_inactive_vehicle(self):
        def change():
            self.car_a.is_active = False
        self._assert_driver_a_unavailable(change)

    def test_inactive_fare(self):
        def change():
            self.fare_a.is_active = False
        self._assert_driver_a_unavailable(change)

    def test_driver_offline(self):
        def change():
            db.session.get(DriverState, self.operator.id).available = False
        self._assert_driver_a_unavailable(change)

    def test_driver_already_holding_a_trip(self):
        other = self._journey(operator=self.operator)

        def change():
            state = db.session.get(DriverState, self.operator.id)
            state.active_booking_id = other.id   # still marked available: the hold decides
        self._assert_driver_a_unavailable(change)

    def test_stale_gps(self):
        def change():
            db.session.get(DriverState, self.operator.id).updated_at = \
                datetime.utcnow() - timedelta(minutes=3)
        self._assert_driver_a_unavailable(change)

    def test_no_shared_position_still_offered(self):
        """Being online is a heartbeat. A driver shares no position until they
        accept a trip, so a missing position must not hide them."""
        state = db.session.get(DriverState, self.operator.id)
        state.lat = state.lng = None
        db.session.commit()
        fresh_client, fresh_csrf = self._customer()
        fresh = self._estimate(fresh_client, fresh_csrf).get_json()
        self.assertIn(self.operator.id, self._driver_ids(fresh["choices"]))

    def test_driving_a_car_that_belongs_to_someone_else(self):
        def change():
            db.session.get(DriverState, self.operator.id).vehicle_id = self.car_b.id
        quote = self._quote()
        change()
        db.session.commit()
        before = self._states()
        for car in (self.car_a, self.car_b):
            with self.subTest(car=car.id):
                response = self._request(quote["token"], self.fare_a, car, self.price_a)
                self.assertIn(response.status_code, (400, 409), response.data)
                self.assertNothingBooked(before, response)


# --- seats and passengers --------------------------------------------------------

class CapacityTests(RideSelectionCase):
    def test_more_passengers_than_vehicle_seats_is_not_offered(self):
        self.car_a.seats = 2
        db.session.commit()
        quote = self._quote(passengers=3)
        self.assertNotIn(self.operator.id, self._driver_ids(quote["choices"]))
        self.assertIn(self.rival.id, self._driver_ids(quote["choices"]))
        before = self._states()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)

    def test_more_passengers_than_fare_seats_is_not_offered(self):
        self.fare_a.seats = 2
        db.session.commit()
        quote = self._quote(passengers=3)
        self.assertNotIn(self.operator.id, self._driver_ids(quote["choices"]))
        before = self._states()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertNothingBooked(before, response)

    def test_exactly_full_is_still_offered(self):
        self.car_a.seats = 3
        self.fare_a.seats = 3
        db.session.commit()
        quote = self._quote(passengers=3)
        self.assertIn(self.operator.id, self._driver_ids(quote["choices"]))

    def test_eight_passengers_is_allowed_when_a_car_fits_them(self):
        self.car_b.seats = 8
        db.session.commit()
        quote = self._quote(passengers=8)
        self.assertEqual(self._driver_ids(quote["choices"]), [self.rival.id])

    def test_passengers_outside_one_to_eight_or_not_whole_are_refused(self):
        self.car_b.seats = 20
        db.session.commit()
        for value in (0, -1, 9, 20, 2.5, 2.0, "2.5", "two", "", True, False, None, [2],
                      {"n": 2}, "9", "0", "1e1", "NaN"):
            with self.subTest(passengers=value):
                client, csrf = self._customer()   # a fresh browser: no rate limit overlap
                response = self._estimate(client, csrf, passengers=value)
                self.assertEqual(response.status_code, 400, response.data)
                data = response.get_json()
                self.assertFalse(data.get("token"))
                self.assertFalse(data.get("choices"))
                with client.session_transaction() as s:
                    self.assertNotIn("ride_quote", s)

    def test_passengers_in_the_request_body_cannot_override_the_estimate(self):
        self.car_a.seats = 2
        db.session.commit()
        quote = self._quote(passengers=2)
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a,
                                 passengers=9)
        if response.status_code == 201:
            (trip,) = self._rides()
            self.assertEqual(trip.passengers, 2)
        else:
            self.assertIn(response.status_code, (400, 409), response.data)
            self.assertEqual(self._rides(), [])


# --- one estimate, one trip ------------------------------------------------------

class DuplicateRequestTests(RideSelectionCase):
    def test_replaying_the_same_token_after_success_is_refused(self):
        quote = self._quote()
        first = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(first.status_code, 201, first.data)
        after_first = self._states()
        for fare, car, price in ((self.fare_a, self.car_a, self.price_a),
                                 (self.fare_b, self.car_b, self.price_b)):
            with self.subTest(fare=fare.id):
                again = self._request(quote["token"], fare, car, price)
                self.assertIn(again.status_code, (400, 409), again.data)
                self.assertEqual(len(self._rides()), 1)
                self.assertEqual(self._states(), after_first)

    def test_a_second_request_while_the_first_ride_is_open_is_refused_with_its_url(self):
        quote = self._quote()
        first = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(first.status_code, 201, first.data)
        (trip,) = self._rides()
        after_first = self._states()

        second_quote = self._quote()   # B is still free, so a new estimate is issued
        self.assertEqual(self._driver_ids(second_quote["choices"]), [self.rival.id])
        response = self._request(second_quote["token"], self.fare_b, self.car_b, self.price_b)
        self.assertEqual(response.status_code, 409, response.data)
        url = response.get_json().get("url")
        self.assertTrue(url)
        self.assertIn(trip.reference, url)
        self.assertEqual(len(self._rides()), 1)
        self.assertEqual(self._states(), after_first)

    def test_the_unique_request_token_stops_a_second_booking_from_the_same_estimate(self):
        quote = self._quote()
        with self.client.session_transaction() as s:
            saved_quote = dict(s["ride_quote"])
        first = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(first.status_code, 201, first.data)
        (trip,) = self._rides()
        after_first = self._states()

        # A second tab: the same estimate is still in its session, and it has no
        # open-ride marker yet. It chooses the other, still-free driver.
        with self.client.session_transaction() as s:
            s["ride_quote"] = saved_quote
            s.pop("booking_reference", None)
        response = self._request(quote["token"], self.fare_b, self.car_b, self.price_b)
        self.assertIn(response.status_code, (200, 400, 409), response.data)
        rides = self._rides()
        self.assertEqual([r.id for r in rides], [trip.id])
        self.assertEqual(rides[0].operator_id, self.operator.id)
        self.assertEqual(self._states(), after_first, "driver B must not be reserved")
        if response.status_code == 200:
            self.assertIn(trip.reference, response.get_json()["url"])

    def test_the_same_estimate_restored_for_the_same_driver_does_not_book_twice(self):
        quote = self._quote()
        with self.client.session_transaction() as s:
            saved_quote = dict(s["ride_quote"])
        first = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(first.status_code, 201, first.data)
        after_first = self._states()
        with self.client.session_transaction() as s:
            s["ride_quote"] = saved_quote
            s.pop("booking_reference", None)
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertIn(response.status_code, (200, 400, 409), response.data)
        self.assertEqual(len(self._rides()), 1)
        self.assertEqual(self._states(), after_first)


# --- the chosen driver is taken in the meantime ----------------------------------

class ChosenDriverTakenTests(RideSelectionCase):
    def _assert_refused_without_substitute(self, response, before_b):
        self.assertEqual(response.status_code, 409, response.data)
        data = response.get_json()
        ids = self._driver_ids(data.get("choices"))
        self.assertNotIn(self.operator.id, ids)
        self.assertIn(self.rival.id, ids, "the other free driver is offered as a choice")
        self.assertEqual(self._rides(), [])
        state_b = self._state(self.rival)
        self.assertEqual((state_b.available, state_b.active_booking_id), before_b)
        self.assertTrue(data.get("token"), "the estimate stays alive so the customer can choose")

    def test_driver_taken_before_the_request_arrives(self):
        quote = self._quote()
        other = self._journey(operator=self.operator)
        state = db.session.get(DriverState, self.operator.id)
        state.active_booking_id, state.available = other.id, False
        db.session.commit()
        before_b = (True, None)
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self._assert_refused_without_substitute(response, before_b)

    def test_driver_taken_between_the_availability_check_and_the_reservation(self):
        """Another customer's reservation commits after offers() read A as free."""
        quote = self._quote()
        other = self._journey(operator=self.operator)
        real_offers = dispatch_module.offers
        calls = []

        def racing_offers(*args, **kwargs):
            result = real_offers(*args, **kwargs)
            if not calls:
                db.session.execute(
                    DriverState.__table__.update()
                    .where(DriverState.operator_id == self.operator.id)
                    .values(active_booking_id=other.id, available=False))
                db.session.commit()
            calls.append(1)
            return result

        with patch("app.dispatch.offers", side_effect=racing_offers):
            response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(len(calls), 2, "the reservation itself must have lost the race")
        self._assert_refused_without_substitute(response, (True, None))
        state_a = self._state(self.operator)
        self.assertEqual(state_a.active_booking_id, other.id)

    def test_after_refusal_the_customer_can_explicitly_choose_the_other_driver(self):
        quote = self._quote()
        state = db.session.get(DriverState, self.operator.id)
        state.available = False
        db.session.commit()
        refused = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self._rides(), [])
        chosen = self._request(refused.get_json()["token"], self.fare_b, self.car_b, self.price_b)
        self.assertEqual(chosen.status_code, 201, chosen.data)
        (trip,) = self._rides()
        self.assertEqual(trip.operator_id, self.rival.id)


# --- asking for a driver by name -------------------------------------------------

class PreferredDriverTests(RideSelectionCase):
    def test_the_preferred_driver_is_marked_and_listed_first(self):
        quote = self._quote(driver=self.rival.id)   # the pricier one
        choices = quote["choices"]
        self.assertEqual(self._driver_ids(choices), [self.rival.id, self.operator.id])
        self.assertTrue(choices[0]["preferred"])
        self.assertFalse(choices[1]["preferred"])
        self.assertFalse(quote.get("note"))

    def test_an_unavailable_preferred_driver_is_named_in_a_note(self):
        state = db.session.get(DriverState, self.operator.id)
        state.available = False
        db.session.commit()
        quote = self._quote(driver=self.operator.id)
        self.assertTrue(quote.get("note"))
        self.assertIn(self.operator.display_name, quote["note"])
        self.assertEqual(self._driver_ids(quote["choices"]), [self.rival.id])
        self.assertFalse(any(c["preferred"] for c in quote["choices"]))

    def test_asking_for_a_driver_does_not_book_them(self):
        self._quote(driver=self.rival.id)
        self.assertEqual(self._rides(), [])
        self.assertEqual(self._state(self.rival).active_booking_id, None)


# --- what the customer sees about their trip -------------------------------------

class StatusPrivacyTests(RideSelectionCase):
    def _book_a(self):
        quote = self._quote()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 201, response.data)
        (trip,) = self._rides()
        return trip

    def test_a_stranger_cannot_see_the_trip(self):
        trip = self._book_a()
        stranger, _csrf = self._customer()
        self.assertEqual(stranger.get(f"/ride/status/{trip.reference}").status_code, 404)
        self.assertEqual(stranger.get(f"/ride/status/{trip.reference.lower()}").status_code, 404)
        self.assertEqual(stranger.get(f"/ride/track/{trip.reference}").status_code, 404)

    def test_before_acceptance_there_is_no_driver_contact(self):
        trip = self._book_a()
        response = self.client.get(f"/ride/status/{trip.reference}")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertEqual(data["status"], "pending")
        self.assertIsNone(data["driver"])
        self.assertEqual(data["chosen"]["name"], self.operator.display_name)
        self.assertNotIn("phone", data["chosen"])
        body = response.get_data(as_text=True)
        for secret in ("3001234", "300 1234", self.operator.phone, self.operator.phone_e164):
            self.assertNotIn(secret, body)


# --- names travel as data --------------------------------------------------------

class NamesAreDataTests(RideSelectionCase):
    HOSTILE = '<script>alert("x")</script> Musa & <b>Co</b>'

    def setUp(self):
        super().setUp()
        self.operator.contact_name = self.HOSTILE
        db.session.commit()

    def test_choices_carry_the_name_exactly_as_stored(self):
        quote = self._quote()
        card = next(c for c in quote["choices"] if c["driver_id"] == self.operator.id)
        self.assertEqual(card["driver"], self.HOSTILE)

    def test_the_unavailable_note_carries_the_name_exactly_as_stored(self):
        db.session.get(DriverState, self.operator.id).available = False
        db.session.commit()
        quote = self._quote(driver=self.operator.id)
        self.assertIn(self.HOSTILE, quote["note"])

    def test_status_carries_the_name_exactly_as_stored(self):
        quote = self._quote()
        self.assertEqual(self._request(quote["token"], self.fare_a, self.car_a,
                                       self.price_a).status_code, 201)
        (trip,) = self._rides()
        data = self.client.get(f"/ride/status/{trip.reference}").get_json()
        self.assertEqual(data["chosen"]["name"], self.HOSTILE)

    def test_the_ride_page_escapes_a_preferred_drivers_name(self):
        response = self.client.get(f"/ride?driver={self.operator.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('<script>alert("x")', html)
        self.assertNotIn("<b>Co</b>", html)

    def _scripts(self, name):
        with open(os.path.join(TEMPLATES, name), encoding="utf-8") as handle:
            source = handle.read()
        return source, "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                            source, flags=re.S))

    def test_templates_never_build_html_from_strings(self):
        for name in ("ride.html", "track.html"):
            with self.subTest(template=name):
                source, script = self._scripts(name)
                self.assertTrue(script.strip(), "inline script found")
                self.assertNotIn("innerHTML", source)
                for sink in ("outerHTML", "insertAdjacentHTML", "document.write",
                             "createContextualFragment", "DOMParser", ".html("):
                    self.assertNotIn(sink, script)

    def test_driver_names_are_not_interpolated_into_markup(self):
        name_refs = re.compile(r"\bchoice\.driver\b|\.chosen\.name\b|\bperson\.name\b|"
                               r"\bdriver\.name\b|\bdata\.note\b|\bname\b")
        markup_literal = re.compile(r"""(["'`])[^"'`\n]*<\s*/?\s*[A-Za-z!][^"'`\n]*\1""")
        for name in ("ride.html", "track.html"):
            _source, script = self._scripts(name)
            for number, line in enumerate(script.splitlines(), 1):
                if name_refs.search(line):
                    with self.subTest(template=name, line=line.strip()[:80]):
                        self.assertIsNone(markup_literal.search(line))
                        self.assertNotRegex(line, r"\$\{[^}]*(driver|name|note)[^}]*\}")


# --- routing switched off --------------------------------------------------------

class RoutingOffTests(RideSelectionCase):
    def test_no_choices_and_no_token_when_routing_is_off(self):
        # TestConfig has no router URL, so routing really is off; do not patch it on.
        def must_not_route(*args, **kwargs):
            raise AssertionError("routing.route was called while routing is off")

        with patch("app.dispatch.routing.route", side_effect=must_not_route):
            response = self._post("/ride/estimate", dict(POINTS, passengers=2))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertEqual(data["choices"], [])
        self.assertTrue(data.get("error"))
        self.assertFalse(data.get("token"))
        self.assertIsNone(data.get("quote"))
        with self.client.session_transaction() as s:
            self.assertNotIn("ride_quote", s)

    def test_an_older_estimate_is_withdrawn_when_routing_goes_off(self):
        quote = self._quote()
        with patch("app.dispatch.routing.route", side_effect=AssertionError("no routing")):
            self._post("/ride/estimate", dict(POINTS, passengers=2))
        before = self._states()
        response = self._request(quote["token"], self.fare_a, self.car_a, self.price_a)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNothingBooked(before, response)


if __name__ == "__main__":
    unittest.main()
