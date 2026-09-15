"""Map-selected points: reverse geocoding on the server, and exact coordinates
carried through the estimate into the booking. The in-browser behaviour (taps,
location permission denied, lookup failures) is tested in test_ride_picker_js."""
import logging
import re
from datetime import datetime
from unittest.mock import patch

from app import routing
from app.models import Booking, DriverState, db
from app.routing import Place, Route
from tests.test_marketplace import MarketplaceCase, TestConfig


class ReverseGeocodingTests(MarketplaceCase):
    def test_unconfigured_says_unavailable_without_calling_out(self):
        with patch("app.routing._fetch") as fetch:
            body = self.client.get("/api/reverse-geocode?lat=13.44&lng=-16.71").get_json()
        self.assertEqual(body, {"available": False, "place": None})
        fetch.assert_not_called()

    def test_rubbish_coordinates_are_refused(self):
        for query in ("lat=abc&lng=1", "lat=95&lng=1", "lat=1&lng=181", "lat=nan&lng=1", "lng=1"):
            self.assertEqual(self.client.get("/api/reverse-geocode?" + query).status_code, 400, query)

    def test_a_found_address_is_returned_and_the_key_stays_on_the_server(self):
        self.app.config.update(REVERSE_GEOCODER_URL="https://geo.example.invalid/reverse?lat={lat}&lon={lon}&apiKey={key}",
                               GEOCODER_API_KEY="SECRET-REVERSE-KEY")
        captured = {}

        def fake_fetch(url, api_key, key_in_url=False):
            captured["url"] = url
            return {"results": [{"formatted": "Kairaba Avenue, Serrekunda", "lat": 13.4501, "lon": -16.6802}]}

        with patch("app.routing._fetch", side_effect=fake_fetch):
            response = self.client.get("/api/reverse-geocode?lat=13.45&lng=-16.68")
        body = response.get_json()
        self.assertEqual(body["place"]["label"], "Kairaba Avenue, Serrekunda")
        self.assertIn("lat=13.450000", captured["url"])
        self.assertIn("lon=-16.680000", captured["url"])
        self.assertNotIn("SECRET-REVERSE-KEY", response.get_data(as_text=True))
        self.assertTrue(self.client.get("/ride").status_code == 200)

    def test_provider_failure_is_reported_and_the_point_is_not_logged(self):
        self.app.config.update(REVERSE_GEOCODER_URL="https://geo.example.invalid/reverse?lat={lat}&lon={lon}")
        with patch("app.routing._fetch", side_effect=routing.RoutingUnavailable("provider returned 503")), \
                self.assertLogs(self.app.logger, level=logging.WARNING) as logs:
            body = self.client.get("/api/reverse-geocode?lat=13.456789&lng=-16.654321").get_json()
        self.assertEqual(body, {"available": True, "place": None, "error": "lookup_failed"})
        output = "\n".join(logs.output)
        self.assertNotIn("13.456789", output)
        self.assertNotIn("16.654321", output)

    def test_nothing_found_is_not_an_error(self):
        self.app.config.update(REVERSE_GEOCODER_URL="https://geo.example.invalid/reverse?lat={lat}&lon={lon}")
        with patch("app.routing._fetch", return_value={"results": []}):
            body = self.client.get("/api/reverse-geocode?lat=13.4&lng=-16.6").get_json()
        self.assertEqual(body, {"available": True, "place": None})

    def test_rate_limited(self):
        self.app.config.update(REVERSE_GEOCODER_URL="https://geo.example.invalid/reverse?lat={lat}&lon={lon}")
        with patch("app.routing.reverse_geocode", return_value=None):
            codes = [self.client.get("/api/reverse-geocode?lat=13.4&lng=-16.6").status_code for _ in range(32)]
        self.assertIn(429, codes)


class RidePageTests(MarketplaceCase):
    def test_controls_are_labelled_and_location_is_only_read_on_request(self):
        page = self.client.get("/ride").get_data(as_text=True)
        self.assertIn('id="pickup-locate"', page)
        self.assertIn(">Use my location<", page)
        self.assertIn('data-which="pickup" aria-pressed="false"', page)
        self.assertIn('data-which="dropoff" aria-pressed="false"', page)
        self.assertIn("Choose pickup on map", page)
        self.assertIn("Choose destination on map", page)
        self.assertIn('aria-live="polite"', page)
        self.assertIn("js/ride-picker.js", page)
        self.assertNotIn("watchPosition", page)
        # The only geolocation call is inside the button's click handler.
        script = page[page.index("createRidePicker({"):]
        self.assertEqual(script.count("getCurrentPosition"), 0)
        self.assertIn('$("pickup-locate").addEventListener("click"', script)

    def test_picker_module_never_watches_location(self):
        with open("app/static/js/ride-picker.js", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("watchPosition", source)
        self.assertEqual(len(re.findall(r"getCurrentPosition\(", source)), 1)


class CoordinatePersistenceTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        self.car.operator_id = self.operator.id
        self.car.seats = 4
        self.fare.kind, self.fare.pricing_model = "ride", "distance"
        self.fare.base_price, self.fare.per_km = 50, 20
        db.session.add(DriverState(operator_id=self.operator.id, vehicle_id=self.car.id, available=True,
                                   updated_at=datetime.utcnow(), lat=13.4, lng=-16.7))
        db.session.commit()
        self.client.get("/ride")
        with self.client.session_transaction() as session:
            self.token = session["_csrf_token"]

    def test_map_selected_points_price_the_route_and_are_stored(self):
        pickup, dropoff = (13.443211, -16.718446), (13.475102, -16.681234)
        calls = []

        def fake_route(origin, destination):
            calls.append((tuple(origin), tuple(destination)))
            return Route(7250, 900, "test", [list(pickup), list(dropoff)])

        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route", side_effect=fake_route):
            estimate = self.client.post("/ride/estimate", headers={"X-CSRF-Token": self.token}, json={
                "pickup_lat": pickup[0], "pickup_lng": pickup[1],
                "dropoff_lat": dropoff[0], "dropoff_lng": dropoff[1], "passengers": 1}).get_json()
        self.assertEqual(calls, [(pickup, dropoff)])
        choice = estimate["choices"][0]
        self.assertEqual(choice["price"], 50 + 20 * 7.25)

        response = self.client.post("/ride/request", headers={"X-CSRF-Token": self.token}, json={
            "token": estimate["token"], "fare_id": choice["fare_id"], "vehicle_id": choice["vehicle_id"],
            "expected_price": choice["price"], "pickup_address": "Pinned location (13.44321, -16.71845)",
            "dropoff_address": "Kairaba Avenue", "customer_name": "Awa", "email": "awa@example.com"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        booking = Booking.query.one()
        self.assertEqual((float(booking.pickup_lat), float(booking.pickup_lng)), pickup)
        self.assertEqual((float(booking.dropoff_lat), float(booking.dropoff_lng)), dropoff)
        self.assertEqual(booking.route_distance_m, 7250)
        self.assertEqual(float(booking.total_price), 195.0)
        self.assertEqual(booking.pickup_address, "Pinned location (13.44321, -16.71845)")

    def test_tampered_coordinates_after_the_estimate_do_not_change_the_trip(self):
        with patch("app.dispatch.routing.routing_available", return_value=True), \
                patch("app.dispatch.routing.route", return_value=Route(3000, 600, "test", [])):
            estimate = self.client.post("/ride/estimate", headers={"X-CSRF-Token": self.token}, json={
                "pickup_lat": 13.4, "pickup_lng": -16.7, "dropoff_lat": 13.5, "dropoff_lng": -16.6,
                "passengers": 1}).get_json()
        choice = estimate["choices"][0]
        self.client.post("/ride/request", headers={"X-CSRF-Token": self.token}, json={
            "token": estimate["token"], "fare_id": choice["fare_id"], "vehicle_id": choice["vehicle_id"],
            "expected_price": choice["price"], "pickup_address": "Kololi", "dropoff_address": "Bakau",
            "customer_name": "Awa", "email": "awa@example.com",
            "pickup_lat": 1.0, "pickup_lng": 1.0, "dropoff_lat": 2.0, "dropoff_lng": 2.0})
        booking = Booking.query.one()
        self.assertEqual((float(booking.pickup_lat), float(booking.dropoff_lat)), (13.4, 13.5))
