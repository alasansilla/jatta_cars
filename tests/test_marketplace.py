"""The marketplace: operators, journeys, commission and who may see what."""
import logging
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import commission, create_app
from app.models import (
    AdminUser, Booking, CommissionEntry, Operator, OperatorFare, Vehicle, db,
)
from app.settings import current_settings, save_settings
from config import Config


class TestConfig(Config):
    GEOCODER_URL = ''
    ROUTER_URL = ''
    GEOCODER_API_KEY = ''
    ROUTER_API_KEY = ''
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    SECRET_KEY = "test-only-" + "s" * 40
    STORAGE_BACKEND = "local"


class MarketplaceCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})

        self.admin = AdminUser(username="admin")
        self.admin.set_password("admin-password-long")
        self.operator = self._operator("Kololi Cabs", "kololi@example.com")
        self.rival = self._operator("Rival Rides", "rival@example.com")
        self.car = Vehicle(make="Toyota", model="Corolla", year=2021,
                           daily_rate=10000, deposit=5000, is_active=True)
        db.session.add_all([self.admin, self.car])
        db.session.commit()

        self.fare = OperatorFare(
            operator_id=self.operator.id, kind="transfer", title="Airport → Kololi",
            from_location="Banjul airport", to_location="Kololi",
            price=Decimal("2500.00"), seats=4, is_active=True)
        db.session.add(self.fare)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _operator(self, name, email, status="approved", password="operator-password"):
        operator = Operator(name=name, slug=Operator.make_slug(name), email=email,
                            status=status)
        if password:
            operator.set_password(password)
        db.session.add(operator)
        db.session.commit()
        return operator

    def _sign_in_operator(self, email="kololi@example.com", password="operator-password"):
        client = self.app.test_client()
        client.post("/operator/login", data={"email": email, "password": password})
        return client

    def _csrf(self, client):
        client.get("/operator/")
        with client.session_transaction() as session:
            return session.get("_csrf_token")

    def _request_ride(self, client=None, **overrides):
        client = client or self.client
        data = {
            "pickup_date": (date.today() + timedelta(days=4)).isoformat(),
            "pickup_time": "09:30",
            "pickup_address": "Banjul airport arrivals",
            "dropoff_address": "Senegambia",
            "passengers": "2", "luggage_count": "1",
            "customer_name": "Awa Ceesay", "email": "awa@example.com",
            "phone": "+220700111",
        }
        data.update(overrides)
        return client.post(f"/rides/{self.fare.id}/request", data=data)

    def _journey(self, status="pending", operator=None, fare=Decimal("2500.00")):
        when = date.today() + timedelta(days=3)
        booking = Booking(
            reference=Booking.new_reference(), booking_type="transfer",
            operator_id=(operator or self.operator).id,
            customer_name="Awa", email="awa@example.com", phone="+220700111",
            pickup_location="Banjul airport", dropoff_location="Kololi",
            start_date=when, end_date=when, total_price=fare,
            deposit_amount=Decimal("0"), status=status)
        db.session.add(booking)
        db.session.commit()
        return booking


# --- commission arithmetic --------------------------------------------------

class CommissionArithmeticTests(MarketplaceCase):
    def test_the_default_rate_is_five_percent(self):
        self.assertEqual(commission.default_rate(), Decimal("5"))
        self.assertEqual(current_settings()["commission_rate"], 5)

    def test_commission_is_five_percent_of_the_fare(self):
        self.assertEqual(commission.calculate(2500, 5), Decimal("125.00"))
        self.assertEqual(commission.calculate(10000, 5), Decimal("500.00"))

    def test_rounding_goes_half_up_not_to_even(self):
        """Money rounds the way people expect; banker's rounding would shave."""
        self.assertEqual(commission.calculate(Decimal("4.90"), 5), Decimal("0.25"))
        self.assertEqual(commission.calculate(Decimal("0.10"), 5), Decimal("0.01"))

    def test_nothing_is_charged_on_nothing(self):
        self.assertEqual(commission.calculate(0, 5), Decimal("0.00"))
        self.assertEqual(commission.calculate(2500, 0), Decimal("0.00"))
        self.assertEqual(commission.calculate(-50, 5), Decimal("0.00"))

    def test_an_operator_rate_overrides_the_default(self):
        self.operator.commission_rate = Decimal("7.50")
        db.session.commit()
        self.assertEqual(commission.rate_for(self.operator), Decimal("7.50"))
        self.assertEqual(commission.rate_for(self.rival), Decimal("5"))

    def test_changing_the_default_moves_operators_without_their_own_rate(self):
        save_settings({"commission_rate": "8"})
        self.assertEqual(commission.rate_for(self.rival), Decimal("8"))

    def test_the_deposit_is_never_part_of_the_base(self):
        """A deposit is the customer's money held and handed back."""
        booking = self._journey(status="completed")
        booking.deposit_amount = Decimal("5000.00")
        db.session.commit()

        entry = commission.record_for(booking)
        db.session.commit()
        self.assertEqual(Decimal(str(entry.base_amount)), Decimal("2500.00"))
        self.assertEqual(Decimal(str(entry.amount)), Decimal("125.00"))


# --- when commission is recorded -------------------------------------------

class CommissionRecordingTests(MarketplaceCase):
    def test_nothing_is_recorded_before_completion(self):
        for status in ("pending", "confirmed", "cancelled"):
            booking = self._journey(status=status)
            self.assertIsNone(commission.record_for(booking), status)
        self.assertEqual(CommissionEntry.query.count(), 0)

    def test_completing_records_it_once(self):
        booking = self._journey(status="completed")
        entry = commission.record_for(booking)
        db.session.commit()
        self.assertIsNotNone(entry)
        self.assertIs(commission.record_for(booking), entry)
        self.assertEqual(CommissionEntry.query.count(), 1)

    def test_completing_stamps_the_time(self):
        booking = self._journey(status="completed")
        commission.record_for(booking)
        db.session.commit()
        self.assertIsInstance(booking.completed_at, datetime)

    def test_un_completing_gives_the_commission_back(self):
        booking = self._journey(status="completed")
        commission.record_for(booking)
        db.session.commit()

        booking.status = "cancelled"
        commission.sync_for(booking)
        db.session.commit()
        self.assertIsNone(db.session.get(Booking, booking.id).commission)
        self.assertEqual(CommissionEntry.query.count(), 0)

    def test_the_recorded_rate_survives_a_later_rate_change(self):
        """History must not be rewritten when the marketplace changes its price."""
        booking = self._journey(status="completed")
        commission.record_for(booking)
        db.session.commit()

        save_settings({"commission_rate": "20"})
        entry = db.session.get(Booking, booking.id).commission
        self.assertEqual(Decimal(str(entry.rate_percent)), Decimal("5"))
        self.assertEqual(Decimal(str(entry.amount)), Decimal("125.00"))

    def test_totals_are_per_operator(self):
        for _ in range(2):
            commission.record_for(self._journey(status="completed"))
        commission.record_for(self._journey(status="completed", operator=self.rival))
        db.session.commit()
        self.assertEqual(commission.totals(self.operator), Decimal("250.00"))
        self.assertEqual(commission.totals(self.rival), Decimal("125.00"))


# --- operator approval ------------------------------------------------------

class OperatorApprovalTests(MarketplaceCase):
    def test_an_application_starts_pending_and_lists_nothing(self):
        response = self.client.post("/operators/apply", data={
            "name": "New Cabs", "contact_name": "Sainey",
            "email": "new@example.com", "phone": "+220700999"})
        self.assertEqual(response.status_code, 302)
        applicant = Operator.query.filter_by(email="new@example.com").one()
        self.assertEqual(applicant.status, "pending")
        self.assertFalse(applicant.can_sign_in)

    def test_a_pending_operator_cannot_sign_in(self):
        self._operator("Waiting", "waiting@example.com", status="pending")
        client = self._sign_in_operator("waiting@example.com")
        self.assertEqual(client.get("/operator/").status_code, 302)

    def test_an_approved_operator_without_a_password_cannot_sign_in(self):
        quiet = self._operator("Quiet", "quiet@example.com", password=None)
        self.assertTrue(quiet.is_approved)
        self.assertFalse(quiet.can_sign_in)

    def test_suspending_signs_them_out_on_their_next_request(self):
        client = self._sign_in_operator()
        self.assertEqual(client.get("/operator/").status_code, 200)

        self.operator.status = "suspended"
        db.session.commit()
        self.assertEqual(client.get("/operator/").status_code, 302)

    def test_only_an_approved_operators_fares_are_public(self):
        self.assertIn("Airport", self.client.get("/rides").get_data(as_text=True))

        self.operator.status = "suspended"
        db.session.commit()
        self.assertNotIn("Airport", self.client.get("/rides").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/rides/{self.fare.id}").status_code, 404)

    def test_suspending_takes_their_cars_off_the_site(self):
        self.car.operator_id = self.operator.id
        db.session.commit()
        self.assertTrue(self.car.is_bookable)

        self.operator.status = "suspended"
        db.session.commit()
        self.assertFalse(db.session.get(Vehicle, self.car.id).is_bookable)

    def test_admin_approval_changes_status(self):
        applicant = self._operator("Fresh", "fresh@example.com", status="pending",
                                   password=None)
        admin = self.app.test_client()
        admin.post("/admin/login", data={"username": "admin",
                                         "password": "admin-password-long"})
        admin.get("/admin/operators")
        with admin.session_transaction() as session:
            token = session["_csrf_token"]

        admin.post(f"/admin/operators/{applicant.id}/decision",
                   data={"csrf_token": token, "status": "approved"})
        refreshed = db.session.get(Operator, applicant.id)
        self.assertEqual(refreshed.status, "approved")
        self.assertIsNotNone(refreshed.approved_at)

    def test_the_operator_area_is_closed_to_the_public(self):
        anonymous = self.app.test_client()
        for path in ("/operator/", "/operator/bookings", "/operator/fares"):
            self.assertEqual(anonymous.get(path).status_code, 302, path)


# --- who may see a customer's details ---------------------------------------

class JourneyPrivacyTests(MarketplaceCase):
    def test_a_ride_request_creates_a_journey_booking(self):
        response = self._request_ride()
        self.assertEqual(response.status_code, 302)
        booking = Booking.query.one()
        self.assertEqual(booking.booking_type, "transfer")
        self.assertTrue(booking.is_journey)
        self.assertEqual(booking.operator_id, self.operator.id)
        self.assertEqual(Decimal(str(booking.total_price)), Decimal("2500.00"))
        self.assertEqual(Decimal(str(booking.deposit_amount)), Decimal("0"))

    def test_only_the_customer_who_booked_sees_the_confirmation(self):
        self._request_ride()
        reference = Booking.query.one().reference

        stranger = self.app.test_client()
        response = stranger.get(f"/booking/{reference}", follow_redirects=True)
        body = response.get_data(as_text=True)
        self.assertNotIn("awa@example.com", body)
        self.assertNotIn("Awa Ceesay", body)

    def test_an_operator_sees_only_their_own_bookings(self):
        booking = self._journey()
        rival_booking = self._journey(operator=self.rival)

        client = self._sign_in_operator()
        self.assertEqual(client.get(f"/operator/bookings/{booking.id}").status_code, 200)
        self.assertEqual(
            client.get(f"/operator/bookings/{rival_booking.id}").status_code, 404,
            "another operator's booking must not be reachable")

        listing = client.get("/operator/bookings").get_data(as_text=True)
        self.assertIn(booking.reference, listing)
        self.assertNotIn(rival_booking.reference, listing)

    def test_contact_details_appear_only_once_the_operator_accepts(self):
        booking = self._journey(status="pending")
        client = self._sign_in_operator()

        pending_view = client.get(f"/operator/bookings/{booking.id}").get_data(as_text=True)
        self.assertNotIn("awa@example.com", pending_view)
        self.assertNotIn("+220700111", pending_view)

        booking.status = "confirmed"
        db.session.commit()
        accepted = client.get(f"/operator/bookings/{booking.id}").get_data(as_text=True)
        self.assertIn("awa@example.com", accepted)

    def test_an_operator_cannot_change_another_operators_booking(self):
        rival_booking = self._journey(operator=self.rival)
        client = self._sign_in_operator()
        token = self._csrf(client)

        response = client.post(f"/operator/bookings/{rival_booking.id}/status",
                               data={"csrf_token": token, "status": "confirmed"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(db.session.get(Booking, rival_booking.id).status, "pending")

    def test_completing_through_the_operator_records_commission(self):
        booking = self._journey(status="confirmed")
        client = self._sign_in_operator()
        token = self._csrf(client)

        client.post(f"/operator/bookings/{booking.id}/status",
                    data={"csrf_token": token, "status": "completed"})
        refreshed = db.session.get(Booking, booking.id)
        self.assertEqual(refreshed.status, "completed")
        self.assertIsNotNone(refreshed.commission)
        self.assertEqual(Decimal(str(refreshed.commission.amount)), Decimal("125.00"))

    def test_an_operator_cannot_reopen_a_completed_booking(self):
        """Reopening would take back commission the marketplace has recorded."""
        booking = self._journey(status="completed")
        commission.record_for(booking)
        db.session.commit()

        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post(f"/operator/bookings/{booking.id}/status",
                    data={"csrf_token": token, "status": "confirmed"})

        refreshed = db.session.get(Booking, booking.id)
        self.assertEqual(refreshed.status, "completed")
        self.assertIsNotNone(refreshed.commission)

    def test_operator_writes_need_the_csrf_token(self):
        booking = self._journey()
        client = self._sign_in_operator()
        client.post(f"/operator/bookings/{booking.id}/status", data={"status": "confirmed"})
        self.assertEqual(db.session.get(Booking, booking.id).status, "pending")


# --- the rental flow must still behave --------------------------------------

class RentalStillWorksTests(MarketplaceCase):
    def _book_car(self, client=None, days_ahead=10, length=3, email="m@example.com"):
        start = date.today() + timedelta(days=days_ahead)
        return (client or self.app.test_client()).post(
            f"/fleet/{self.car.id}/book",
            data={"start": start.isoformat(),
                  "end": (start + timedelta(days=length)).isoformat(),
                  "customer_name": "Modou Jallow", "email": email,
                  "phone": "+220700222", "pickup_location": "Kololi",
                  "dropoff_location": "Kololi"},
            follow_redirects=True)

    def test_a_rental_still_books(self):
        self._book_car()
        booking = Booking.query.filter_by(booking_type="rental").one()
        self.assertFalse(booking.is_journey)
        self.assertIsNotNone(booking.vehicle_id)

    def test_an_overlapping_rental_is_still_refused(self):
        self._book_car()
        response = self._book_car(email="other@example.com")
        self.assertIn("already booked", response.get_data(as_text=True))
        self.assertEqual(Booking.query.filter_by(booking_type="rental").count(), 1)

    def test_a_journey_does_not_hold_a_car_for_the_day(self):
        """Two taxi rides on one car in a day are normal; two hires are not."""
        when = date.today() + timedelta(days=5)
        self.car.operator_id = self.operator.id
        db.session.add(Booking(
            reference="JC-RIDE01", booking_type="ride", operator_id=self.operator.id,
            vehicle_id=self.car.id, customer_name="A", email="a@example.com",
            pickup_location="X", dropoff_location="Y", start_date=when, end_date=when,
            total_price=Decimal("1000.00"), deposit_amount=Decimal("0"),
            status="confirmed"))
        db.session.commit()

        self.assertTrue(self.car.is_available(when, when + timedelta(days=2)))


if __name__ == "__main__":
    unittest.main()


# --- maps, route planning and what they may reveal --------------------------

class RouteLookupTests(MarketplaceCase):
    """The lookup endpoints are a proxy, so they must not leak or be abusable."""

    def setUp(self):
        super().setUp()
        self.distance_fare = OperatorFare(
            operator_id=self.operator.id, kind="ride", title="Metered ride",
            from_location="Kololi", to_location="Anywhere",
            pricing_model="distance", base_price=Decimal("500"),
            per_km=Decimal("60"), minimum_price=Decimal("1000"), is_active=True)
        db.session.add(self.distance_fare)
        db.session.commit()

    def test_lookups_are_quiet_when_no_provider_is_configured(self):
        """Unset is the default, and must degrade rather than error."""
        response = self.client.get("/api/geocode?q=Kololi")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["available"])
        self.assertEqual(body["places"], [])

    def test_routing_unconfigured_leaves_a_distance_fare_unpriced(self):
        response = self.client.get(
            f"/api/route?fare_id={self.distance_fare.id}"
            "&from_lat=13.44&from_lng=-16.70&to_lat=13.45&to_lng=-16.58")
        body = response.get_json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["quote"], "an unmeasured distance fare must not be priced")
        self.assertEqual(body["basis"], "manual")

    def test_a_fixed_fare_still_prices_without_any_routing(self):
        response = self.client.get(
            f"/api/route?fare_id={self.fare.id}"
            "&from_lat=13.44&from_lng=-16.70&to_lat=13.45&to_lng=-16.58")
        body = response.get_json()
        self.assertEqual(Decimal(str(body["quote"])), Decimal("2500.00"))
        self.assertEqual(body["basis"], "fixed")

    def test_nonsense_coordinates_are_refused(self):
        for query in ("from_lat=999&from_lng=0&to_lat=1&to_lng=1",
                      "from_lat=13&from_lng=-16&to_lat=abc&to_lng=1",
                      "from_lat=13&from_lng=-16&to_lat=1"):
            response = self.client.get(f"/api/route?fare_id={self.fare.id}&{query}")
            self.assertEqual(response.status_code, 400, query)

    def test_a_hidden_fare_cannot_be_probed_through_the_route_endpoint(self):
        """The endpoint must not confirm an unlisted operator's routes exist."""
        self.operator.status = "suspended"
        db.session.commit()
        response = self.client.get(
            f"/api/route?fare_id={self.fare.id}"
            "&from_lat=13.44&from_lng=-16.70&to_lat=13.45&to_lng=-16.58")
        self.assertEqual(response.status_code, 404)

    def test_the_page_carries_no_api_key(self):
        """Keys live on the server. A page is public by definition."""
        class Keyed(TestConfig):
            GEOCODER_URL = "https://example.invalid/search?q={query}&key={key}"
            GEOCODER_API_KEY = "SECRET-GEOCODER-KEY"
            ROUTER_URL = "https://example.invalid/route/{coords}?key={key}"
            ROUTER_API_KEY = "SECRET-ROUTER-KEY"

        app = create_app(Keyed)
        with app.app_context():
            db.create_all()
            save_settings({"site_live": True})
            operator = Operator(name="K", slug="k", email="k@example.com",
                                status="approved")
            db.session.add(operator)
            db.session.commit()
            fare = OperatorFare(operator_id=operator.id, kind="ride", title="R",
                                from_location="A", to_location="B",
                                pricing_model="fixed", price=Decimal("100"),
                                is_active=True)
            db.session.add(fare)
            db.session.commit()
            page = app.test_client().get(f"/rides/{fare.id}").get_data(as_text=True)
            self.assertNotIn("SECRET-GEOCODER-KEY", page)
            self.assertNotIn("SECRET-ROUTER-KEY", page)
            self.assertNotIn("Bearer", page)
            db.session.remove()
            db.drop_all()

    def test_map_settings_handed_to_the_page_contain_no_secret(self):
        from app.routing import map_settings

        with self.app.test_request_context():
            settings = map_settings()
        self.assertNotIn("key", " ".join(settings).lower())
        self.assertIn("tile_url", settings)

    def test_a_customers_address_is_never_written_to_a_log(self):
        """Failures are logged; the address the customer typed is not."""
        class Broken(TestConfig):
            GEOCODER_URL = "https://example.invalid/search?q={query}"

        app = create_app(Broken)
        records = []
        with app.app_context():
            db.create_all()
            save_settings({"site_live": True})
            handler = logging.Handler()
            handler.emit = lambda record: records.append(record.getMessage())
            app.logger.addHandler(handler)
            app.logger.propagate = False      # capture it, do not print it
            app.test_client().get("/api/geocode?q=12 Hidden Lane, Kololi")
            app.logger.removeHandler(handler)
            db.session.remove()
            db.drop_all()

        joined = " ".join(records)
        self.assertNotIn("Hidden Lane", joined, "the typed address reached a log")

    def test_the_lookup_endpoints_are_rate_limited(self):
        """Otherwise this is a free open proxy onto somebody's paid quota."""
        class Configured(TestConfig):
            GEOCODER_URL = "https://example.invalid/search?q={query}"

        app = create_app(Configured)
        with app.app_context():
            db.create_all()
            save_settings({"site_live": True})
            client = app.test_client()
            # Each call tries an unreachable host and logs a warning. That is the
            # behaviour under test elsewhere; here it is just noise that would
            # bury a real failure.
            logging.disable(logging.CRITICAL)
            try:
                statuses = {client.get("/api/geocode?q=Kololi beach").status_code
                            for _ in range(60)}
            finally:
                logging.disable(logging.NOTSET)
            db.session.remove()
            db.drop_all()
        self.assertIn(429, statuses, "no rate limit on the geocoding proxy")


class RoutePricingTests(MarketplaceCase):
    """Distance fares are priced from a measured route, never from the form."""

    def setUp(self):
        super().setUp()
        self.distance_fare = OperatorFare(
            operator_id=self.operator.id, kind="ride", title="Metered ride",
            from_location="Kololi", to_location="Anywhere",
            pricing_model="distance", base_price=Decimal("500"),
            per_km=Decimal("60"), minimum_price=Decimal("1000"), is_active=True)
        db.session.add(self.distance_fare)
        db.session.commit()

    def _request(self, fare, **extra):
        data = {
            "pickup_date": (date.today() + timedelta(days=4)).isoformat(),
            "pickup_time": "09:30",
            "pickup_address": "Senegambia strip",
            "dropoff_address": "Banjul airport",
            "passengers": "2",
            "customer_name": "Awa Ceesay", "email": "awa@example.com",
            "phone": "+220700111",
        }
        data.update(extra)
        return self.client.post(f"/rides/{fare.id}/request", data=data)

    def test_an_unmeasurable_distance_fare_is_stored_unpriced(self):
        """Null, never zero: zero would read as free and earn nothing."""
        self._request(self.distance_fare)
        booking = Booking.query.one()
        self.assertIsNone(booking.total_price)
        self.assertTrue(booking.needs_quote)
        self.assertEqual(booking.quote_basis, "manual")

    def test_a_posted_distance_cannot_talk_the_fare_down(self):
        """The server measures; it does not take the browser's word for it."""
        self._request(self.distance_fare, route_distance_m="10",
                      total_price="1", quote_basis="distance")
        booking = Booking.query.one()
        self.assertIsNone(booking.route_distance_m)
        self.assertIsNone(booking.total_price)

    def test_coordinates_are_kept_when_the_page_supplies_them(self):
        self._request(self.fare, pickup_lat="13.4400", pickup_lng="-16.7000",
                      dropoff_lat="13.3380", dropoff_lng="-16.6520")
        booking = Booking.query.one()
        self.assertEqual(float(booking.pickup_lat), 13.44)
        self.assertEqual(float(booking.dropoff_lng), -16.652)
        # Typed text is kept too; it is what the customer actually wrote.
        self.assertEqual(booking.pickup_address, "Senegambia strip")

    def test_rubbish_coordinates_are_dropped_not_stored(self):
        self._request(self.fare, pickup_lat="999", pickup_lng="abc")
        booking = Booking.query.one()
        self.assertIsNone(booking.pickup_lat)
        self.assertIsNone(booking.pickup_lng)

    def test_a_fixed_fare_is_unaffected_by_having_no_route(self):
        self._request(self.fare)
        booking = Booking.query.one()
        self.assertEqual(Decimal(str(booking.total_price)), Decimal("2500.00"))
        self.assertEqual(booking.quote_basis, "fixed")

    def test_an_operator_cannot_complete_an_unpriced_job(self):
        self._request(self.distance_fare)
        booking = Booking.query.one()
        booking.status = "confirmed"
        db.session.commit()

        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post(f"/operator/bookings/{booking.id}/status",
                    data={"csrf_token": token, "status": "completed"})

        refreshed = db.session.get(Booking, booking.id)
        self.assertEqual(refreshed.status, "confirmed")
        self.assertIsNone(refreshed.commission)

    def test_an_operator_sets_a_fare_and_can_then_complete(self):
        self._request(self.distance_fare)
        booking = Booking.query.one()
        booking.status = "confirmed"
        db.session.commit()

        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post(f"/operator/bookings/{booking.id}/fare",
                    data={"csrf_token": token, "total_price": "1800"})
        self.assertEqual(Decimal(str(db.session.get(Booking, booking.id).total_price)),
                         Decimal("1800.00"))

        client.post(f"/operator/bookings/{booking.id}/status",
                    data={"csrf_token": token, "status": "completed"})
        refreshed = db.session.get(Booking, booking.id)
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(Decimal(str(refreshed.commission.amount)), Decimal("90.00"))

    def test_an_operator_cannot_price_another_operators_job(self):
        rival_booking = self._journey(operator=self.rival)
        client = self._sign_in_operator()
        token = self._csrf(client)
        response = client.post(f"/operator/bookings/{rival_booking.id}/fare",
                               data={"csrf_token": token, "total_price": "1"})
        self.assertEqual(response.status_code, 404)

    def test_an_admin_cannot_complete_an_unpriced_job_either(self):
        self._request(self.distance_fare)
        booking = Booking.query.one()
        booking.status = "confirmed"
        db.session.commit()

        admin = self.app.test_client()
        admin.post("/admin/login", data={"username": "admin",
                                         "password": "admin-password-long"})
        admin.get("/admin/bookings")
        with admin.session_transaction() as session:
            token = session["_csrf_token"]
        admin.post(f"/admin/bookings/{booking.id}/status",
                   data={"csrf_token": token, "status": "completed"})

        self.assertEqual(db.session.get(Booking, booking.id).status, "confirmed")


class DistanceFareFormTests(MarketplaceCase):
    def test_an_operator_can_publish_a_distance_fare(self):
        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post("/operator/fares", data={
            "csrf_token": token, "title": "Metered ride", "kind": "ride",
            "from_location": "Kololi", "to_location": "Anywhere",
            "pricing_model": "distance", "base_price": "500",
            "per_km": "60", "minimum_price": "1000"})

        fare = OperatorFare.query.filter_by(title="Metered ride").one()
        self.assertTrue(fare.is_distance_based)
        self.assertIsNone(fare.price)
        self.assertIsNone(fare.display_price, "a distance fare has no single price")
        self.assertEqual(fare.quote(12500), (Decimal("1250.00"), "distance"))

    def test_a_distance_fare_needs_a_rate(self):
        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post("/operator/fares", data={
            "csrf_token": token, "title": "Broken", "kind": "ride",
            "from_location": "A", "to_location": "B",
            "pricing_model": "distance", "base_price": "500"})
        self.assertIsNone(OperatorFare.query.filter_by(title="Broken").first())

    def test_a_fixed_fare_still_needs_its_price(self):
        client = self._sign_in_operator()
        token = self._csrf(client)
        client.post("/operator/fares", data={
            "csrf_token": token, "title": "No price", "kind": "ride",
            "from_location": "A", "to_location": "B", "pricing_model": "fixed"})
        self.assertIsNone(OperatorFare.query.filter_by(title="No price").first())
