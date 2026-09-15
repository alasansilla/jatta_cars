"""The live trip map: who may see a driver's position, and when it exists at all.

Location is shared only while a driver holds an accepted trip, only with that
trip's customer, and it is cleared the moment the trip ends in any way.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from app import dispatch
from app.models import Booking, DriverState, Operator, OperatorFare, Vehicle, db
from app.routing import Route
from tests.test_marketplace import MarketplaceCase

PICKUP = (13.443211, -16.718446)
DROPOFF = (13.475102, -16.681234)


class LiveMapCase(MarketplaceCase):
    def setUp(self):
        super().setUp()
        dispatch._ROUTE_CACHE.clear()
        self.car.operator_id = self.operator.id
        self.car.seats = 4
        self.fare.kind, self.fare.pricing_model = "ride", "distance"
        self.fare.base_price, self.fare.per_km = 50, 20
        self.rival_car = Vehicle(make="Kia", model="Rio", year=2020, daily_rate=9000, deposit=0,
                                 seats=4, is_active=True, operator_id=self.rival.id)
        db.session.add(self.rival_car)
        db.session.add(OperatorFare(operator_id=self.rival.id, kind="ride", title="Rival rides",
                                    from_location="A", to_location="B", pricing_model="distance",
                                    base_price=60, per_km=25, is_active=True))
        db.session.commit()
        self.driver = self._sign_in_operator()
        self.driver_csrf = self._csrf(self.driver)
        self.rival_driver = self._sign_in_operator("rival@example.com")
        self.rival_csrf = self._csrf(self.rival_driver)
        self.customer = self.app.test_client()
        self.customer.get("/ride")
        with self.customer.session_transaction() as session:
            self.customer_csrf = session["_csrf_token"]

    # --- helpers -------------------------------------------------------------

    def driver_post(self, path, body, client=None, csrf=None):
        return (client or self.driver).post(path, json=body,
                                             headers={"X-CSRF-Token": csrf or self.driver_csrf})

    def go_online(self, client=None, csrf=None, car=None):
        response = self.driver_post("/operator/drive/heartbeat",
                                    {"vehicle_id": (car or self.car).id, "available": True},
                                    client, csrf)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def state(self, operator=None):
        db.session.expire_all()
        return db.session.get(DriverState, (operator or self.operator).id)

    def request_ride(self):
        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route", return_value=Route(3000, 600, "test", [])):
            estimate = self.customer.post("/ride/estimate", headers={"X-CSRF-Token": self.customer_csrf}, json={
                "pickup_lat": PICKUP[0], "pickup_lng": PICKUP[1],
                "dropoff_lat": DROPOFF[0], "dropoff_lng": DROPOFF[1], "passengers": 1}).get_json()
        choice = next(c for c in estimate["choices"] if c["driver_id"] == self.operator.id)
        response = self.customer.post("/ride/request", headers={"X-CSRF-Token": self.customer_csrf}, json={
            "token": estimate["token"], "fare_id": choice["fare_id"], "vehicle_id": choice["vehicle_id"],
            "expected_price": choice["price"], "pickup_address": "Kololi", "dropoff_address": "Bakau",
            "customer_name": "Awa", "email": "awa@example.com", "phone": "+220 3901234"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return Booking.query.filter_by(booking_type="ride").order_by(Booking.id.desc()).first()

    def accepted_trip(self):
        self.go_online()
        booking = self.request_ride()
        response = self.driver_post("/operator/drive/accept", {"booking_id": booking.id})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return booking

    def share(self, booking, lat=13.45, lng=-16.70, client=None, csrf=None):
        return self.driver_post("/operator/drive/location",
                                {"booking_id": booking.id, "lat": lat, "lng": lng}, client, csrf)

    def customer_status(self, booking, client=None):
        return (client or self.customer).get(f"/ride/status/{booking.reference}")


class OnlineWithoutLocationTests(LiveMapCase):
    def test_going_online_stores_no_position_and_still_makes_the_driver_choosable(self):
        self.go_online()
        state = self.state()
        self.assertTrue(state.available)
        self.assertIsNone(state.lat)
        self.assertIsNone(state.location_at)
        booking = self.request_ride()
        self.assertEqual(booking.operator_id, self.operator.id)

    def test_heartbeat_ignores_any_coordinates_sent_with_it(self):
        self.driver_post("/operator/drive/heartbeat",
                         {"vehicle_id": self.car.id, "available": True, "lat": 13.4, "lng": -16.7})
        self.assertIsNone(self.state().lat)

    def test_no_position_is_accepted_before_the_driver_accepts(self):
        self.go_online()
        response = self.driver_post("/operator/drive/location", {"lat": 13.4, "lng": -16.7})
        self.assertEqual(response.status_code, 409)
        booking = self.request_ride()
        response = self.share(booking)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["share_location"])
        self.assertIsNone(self.state().lat)
        self.assertFalse(self.driver.get("/operator/drive/state").get_json()["share_location"])

    def test_a_stale_heartbeat_takes_the_driver_offline(self):
        self.go_online()
        state = self.state()
        state.updated_at = datetime.utcnow() - timedelta(minutes=3)
        db.session.commit()
        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route", return_value=Route(3000, 600, "test", [])):
            estimate = self.customer.post("/ride/estimate", headers={"X-CSRF-Token": self.customer_csrf}, json={
                "pickup_lat": PICKUP[0], "pickup_lng": PICKUP[1],
                "dropoff_lat": DROPOFF[0], "dropoff_lng": DROPOFF[1], "passengers": 1}).get_json()
        self.assertNotIn(self.operator.id, [c["driver_id"] for c in estimate.get("choices", [])])


class LifecycleTests(LiveMapCase):
    def test_accepting_starts_sharing_and_the_customer_sees_the_live_position(self):
        booking = self.accepted_trip()
        self.assertTrue(self.driver.get("/operator/drive/state").get_json()["share_location"])
        self.assertEqual(self.share(booking, 13.451234, -16.701234).status_code, 200)
        driver = self.customer_status(booking).get_json()["driver"]
        self.assertEqual(driver["location"], "live")
        self.assertEqual((driver["lat"], driver["lng"]), (13.451234, -16.701234))
        self.assertTrue(driver["online"])

    def test_before_sharing_the_customer_is_told_it_is_not_shared(self):
        booking = self.accepted_trip()
        driver = self.customer_status(booking).get_json()["driver"]
        self.assertEqual(driver["location"], "not_shared")
        self.assertNotIn("lat", driver)

    def test_completing_at_drop_off_clears_the_position(self):
        booking = self.accepted_trip()
        self.share(booking)
        for stage in ("arriving", "in_progress", "completed"):
            self.assertEqual(self.driver_post("/operator/drive/status",
                                              {"booking_id": booking.id, "status": stage}).status_code, 200)
        state = self.state()
        self.assertEqual((state.lat, state.lng, state.location_at, state.active_booking_id), (None, None, None, None))
        self.assertIsNone(self.customer_status(booking).get_json()["driver"])
        self.assertEqual(self.share(booking).status_code, 409)
        self.assertIsNone(self.state().lat)
        self.assertFalse(self.driver.get("/operator/drive/state").get_json()["share_location"])

    def test_customer_cancellation_clears_the_position(self):
        booking = self.accepted_trip()
        self.share(booking)
        self.assertEqual(self.customer.post(f"/ride/cancel/{booking.reference}",
                                            headers={"X-CSRF-Token": self.customer_csrf}).status_code, 200)
        self.assertIsNone(self.state().lat)
        self.assertEqual(self.share(booking).status_code, 409)

    def test_driver_declining_after_accepting_clears_the_position(self):
        booking = self.accepted_trip()
        self.share(booking)
        self.driver_post("/operator/drive/decline", {"booking_id": booking.id})
        self.assertIsNone(self.state().lat)
        self.assertEqual(self.share(booking).status_code, 409)

    def test_going_offline_during_a_trip_stops_sharing_but_keeps_the_trip(self):
        booking = self.accepted_trip()
        self.share(booking)
        self.driver_post("/operator/drive/offline", {})
        state = self.state()
        self.assertIsNone(state.lat)
        self.assertEqual(state.active_booking_id, booking.id)
        self.assertEqual(self.customer_status(booking).get_json()["driver"]["location"], "not_shared")

    def test_stop_sharing_forgets_the_position(self):
        booking = self.accepted_trip()
        self.share(booking)
        self.driver_post("/operator/drive/location/stop", {})
        self.assertIsNone(self.state().lat)
        self.assertEqual(self.customer_status(booking).get_json()["driver"]["location"], "not_shared")

    def test_admin_cancelling_clears_the_position(self):
        booking = self.accepted_trip()
        self.share(booking)
        admin = self.app.test_client()
        admin.post("/admin/login", data={"username": "admin", "password": "admin-password-long"})
        admin.get("/admin/bookings")
        with admin.session_transaction() as session:
            token = session["_csrf_token"]
        admin.post(f"/admin/bookings/{booking.id}/status", data={"csrf_token": token, "status": "cancelled"})
        self.assertIsNone(self.state().lat)

    def test_migration_0011_clears_positions_outside_active_trips(self):
        import importlib
        step = importlib.import_module("migrations.versions.0011_trip_only_driver_location")
        booking = self.accepted_trip()
        self.share(booking)
        db.session.add(DriverState(operator_id=self.rival.id, vehicle_id=self.rival_car.id,
                                   available=True, lat=13.1, lng=-16.1, updated_at=datetime.utcnow()))
        db.session.commit()
        with db.engine.begin() as connection:
            step.upgrade(connection, db.metadata)
        self.assertIsNone(self.state(self.rival).lat)
        self.assertIsNotNone(self.state().lat)


class StalePositionTests(LiveMapCase):
    def test_an_old_position_is_withheld_and_described(self):
        booking = self.accepted_trip()
        self.share(booking)
        state = self.state()
        state.location_at = datetime.utcnow() - timedelta(seconds=90)
        db.session.commit()
        driver = self.customer_status(booking).get_json()["driver"]
        self.assertEqual(driver["location"], "stale")
        self.assertNotIn("lat", driver)

    def test_a_driver_whose_heartbeat_stopped_is_offline_to_the_customer(self):
        booking = self.accepted_trip()
        self.share(booking)
        state = self.state()
        state.updated_at = state.location_at = datetime.utcnow() - timedelta(minutes=4)
        db.session.commit()
        body = self.customer_status(booking).get_json()
        self.assertEqual(body["driver"]["location"], "offline")
        self.assertFalse(body["driver"]["online"])
        self.assertTrue(body["driver_silent"])
        self.assertTrue(body["can_choose_again"])


class AuthorisationTests(LiveMapCase):
    def test_strangers_and_other_customers_cannot_read_the_trip(self):
        booking = self.accepted_trip()
        self.share(booking)
        stranger = self.app.test_client()
        other_customer = self.app.test_client()
        with other_customer.session_transaction() as session:
            session["booking_reference"] = "JC-OTHER1"
        for client in (stranger, other_customer):
            self.assertEqual(client.get(f"/ride/status/{booking.reference}").status_code, 404)
            self.assertEqual(client.get(f"/ride/route/{booking.reference}").status_code, 404)
            self.assertEqual(client.get(f"/ride/track/{booking.reference}").status_code, 404)

    def test_another_driver_cannot_share_or_read_this_trip(self):
        booking = self.accepted_trip()
        self.go_online(self.rival_driver, self.rival_csrf, self.rival_car)
        response = self.share(booking, client=self.rival_driver, csrf=self.rival_csrf)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.state().lat)
        self.assertIsNone(self.state(self.rival).lat)
        self.assertEqual(self.rival_driver.get(f"/operator/drive/route?booking_id={booking.id}").status_code, 404)

    def test_driver_endpoints_need_a_signed_in_approved_driver_and_csrf(self):
        booking = self.accepted_trip()
        anonymous = self.app.test_client()
        refused = anonymous.post("/operator/drive/location",
                                 json={"booking_id": booking.id, "lat": 1, "lng": 1}).status_code
        self.assertIn(refused, (302, 400))      # no session: CSRF or sign-in stops it
        self.assertEqual(anonymous.get(f"/operator/drive/route?booking_id={booking.id}").status_code, 302)
        no_csrf = self.driver.post("/operator/drive/location", json={"booking_id": booking.id, "lat": 1, "lng": 1})
        self.assertEqual(no_csrf.status_code, 400)
        self.assertIsNone(self.state().lat)

    def test_rubbish_coordinates_are_refused(self):
        booking = self.accepted_trip()
        for lat, lng in ((95, 1), (1, 200), ("nan", 1), (True, 1), (None, 1)):
            self.assertEqual(self.share(booking, lat, lng).status_code, 400, (lat, lng))
        self.assertIsNone(self.state().lat)


class RouteTests(LiveMapCase):
    def test_routes_only_for_an_accepted_trip_and_cached(self):
        self.go_online()
        booking = self.request_ride()
        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route") as route:
            route.side_effect = lambda origin, destination: Route(
                5000, 700, "test", [list(origin), list(destination)])
            pending = self.customer.get(f"/ride/route/{booking.reference}").get_json()
            self.assertIsNone(pending["trip"])
            route.assert_not_called()

            self.driver_post("/operator/drive/accept", {"booking_id": booking.id})
            first = self.customer.get(f"/ride/route/{booking.reference}").get_json()
            self.assertEqual(first["trip"]["geometry"], [list(PICKUP), list(DROPOFF)])
            self.assertIsNone(first["to_pickup"])          # no position shared yet

            self.share(booking, 13.401, -16.801)
            second = self.customer.get(f"/ride/route/{booking.reference}").get_json()
            self.assertEqual(second["to_pickup"]["geometry"][0], [13.401, -16.801])
            driver_view = self.driver.get(f"/operator/drive/route?booking_id={booking.id}").get_json()
            self.assertEqual(driver_view["trip"]["geometry"], [list(PICKUP), list(DROPOFF)])
            calls = route.call_count
            self.customer.get(f"/ride/route/{booking.reference}")
            self.assertEqual(route.call_count, calls, "a repeat within the cache window called the provider")

    def test_routing_unavailable_draws_nothing(self):
        booking = self.accepted_trip()
        self.share(booking)
        body = self.customer.get(f"/ride/route/{booking.reference}").get_json()
        self.assertEqual(body, {"available": False, "trip": None, "to_pickup": None})


class PageTests(LiveMapCase):
    def test_driving_mode_shares_location_only_for_a_trip(self):
        page = self.driver.get("/operator/drive").get_data(as_text=True)
        self.assertEqual(page.count("watchPosition"), 1)
        watch_at = page.index("watchPosition")
        self.assertLess(page.rindex("function startSharing", 0, watch_at), watch_at)
        online = page[page.index("function goOnline"):page.index("function goOffline")]
        self.assertNotIn("geolocation", online)
        self.assertIn("Accepting shares your live location with this customer until drop-off", page)
        self.assertIn("Stop sharing my location", page)

    def test_attribution_stays_on_both_maps(self):
        self.app.config.update(ROUTER_URL="https://route.example.invalid/{coords}")
        booking = self.accepted_trip()
        for page in (self.driver.get("/operator/drive"), self.customer.get(f"/ride/track/{booking.reference}")):
            self.assertIn("Powered by Geoapify", page.get_data(as_text=True))
            self.assertIn("OpenStreetMap", page.get_data(as_text=True))
