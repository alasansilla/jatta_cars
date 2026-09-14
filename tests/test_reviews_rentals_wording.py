"""Reviews, rentals owned by drivers, and the customer-facing wording.

Written from the requirements, adversarially: when the application disagrees
with a requirement the test is left failing on purpose.
"""
import glob
import html
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import reviews as reviews_module
from app.models import (
    Booking, BookingReview, CommissionEntry, DriverState, Operator, OperatorFare,
    Vehicle, db,
)
from app.settings import current_settings
from tests.test_marketplace import MarketplaceCase

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

FORBIDDEN_WORDS = re.compile(r"\b(operators?|providers?|partners?)\b", re.IGNORECASE)
FORBIDDEN_PHRASES = [re.compile(r"\blist\s+your\s+service\b", re.IGNORECASE),
                     re.compile(r"\bprovider\s+login\b", re.IGNORECASE)]
FALSE_CLAIMS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bowners?\s+manage\s+(their\s+|the\s+)?(cars|vehicles|fleet)\b",
        r"\badd\s+your\s+(own\s+)?(vehicles|cars)\b",
        r"\bstaff\s+accounts?\b",
        r"\bfleet\s+staff\b",
        r"\bmanage\s+your\s+(own\s+)?(cars|vehicles|fleet)\b",
    )
]


def visible_text(markup):
    """What a reader sees: no scripts, styles, comments, tags or attributes."""
    text = markup.decode("utf-8") if isinstance(markup, bytes) else markup
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<(script|style|template|noscript)\b.*?</\1\s*>", " ", text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]*>", " ", text, flags=re.S)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text)


class Base(MarketplaceCase):
    def setUp(self):
        super().setUp()
        # The rentable car belongs to Kololi Cabs for most tests.
        self.driver_car = Vehicle(make="Hyundai", model="Tucson", year=2020,
                                  daily_rate=Decimal("3000.00"), deposit=Decimal("7000.00"),
                                  seats=5, is_active=True, operator_id=self.operator.id)
        db.session.add(self.driver_car)
        db.session.commit()

    # --- helpers --------------------------------------------------------------

    def _booking(self, booking_type="ride", status="completed", operator=None,
                 vehicle=None, email="awa@example.com", name="Awa Ceesay",
                 price=Decimal("1500.00")):
        when = date.today()
        booking = Booking(
            reference=Booking.new_reference(), booking_type=booking_type,
            operator_id=(operator.id if operator is not None else
                         (None if booking_type == "rental" and vehicle is not None
                          and vehicle.operator_id is None else self.operator.id)),
            vehicle_id=vehicle.id if vehicle is not None else None,
            customer_name=name, email=email, phone="+220877001111",
            pickup_location="Banjul airport", dropoff_location="Kololi",
            start_date=when, end_date=when + (timedelta(days=2) if booking_type == "rental"
                                              else timedelta()),
            total_price=price, deposit_amount=Decimal("0"), status=status)
        if booking_type == "rental" and vehicle is not None:
            booking.operator_id = vehicle.operator_id
        db.session.add(booking)
        db.session.commit()
        return booking

    def _client_holding(self, booking):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["booking_reference"] = booking.reference
        return client

    def _token(self, client, path="/ride"):
        client.get(path)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def _post_review(self, client, booking, rating="5", comment="Great, on time.", token=None):
        token = token or self._token(client)
        return client.post(f"/booking/{booking.reference}/review",
                           data={"csrf_token": token, "rating": rating, "comment": comment})

    def _admin(self):
        admin = self.app.test_client()
        admin.post("/admin/login", data={"username": "admin",
                                         "password": "admin-password-long"})
        admin.get("/admin/vehicles")
        with admin.session_transaction() as session:
            token = session["_csrf_token"]
        return admin, token

    def _signed_in_driver(self, operator=None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = (operator or self.operator).id
            session["operator_name"] = (operator or self.operator).display_name
        return client


# --- reviews: who may write one, and what is accepted --------------------------

class ReviewAccessTests(Base):
    def test_only_a_completed_booking_can_be_reviewed(self):
        for status in ("pending", "confirmed", "accepted", "arriving", "in_progress",
                       "cancelled", "declined", "expired"):
            booking = self._booking(status=status)
            client = self._client_holding(booking)
            self.assertNotEqual(client.get(f"/booking/{booking.reference}/review").status_code,
                                200, status)
            response = self._post_review(client, booking)
            self.assertIn(response.status_code, (403, 404), status)
            self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(),
                             0, status)

    def test_completed_booking_accepts_a_review(self):
        booking = self._booking()
        client = self._client_holding(booking)
        self.assertEqual(client.get(f"/booking/{booking.reference}/review").status_code, 200)
        response = self._post_review(client, booking, rating="4", comment="Clean car, kind driver.")
        self.assertIn(response.status_code, (302, 303))
        review = BookingReview.query.filter_by(booking_id=booking.id).one()
        self.assertEqual(review.rating, 4)
        self.assertEqual(review.comment, "Clean car, kind driver.")

    def test_another_client_gets_404(self):
        booking = self._booking()
        stranger = self.app.test_client()
        self.assertEqual(stranger.get(f"/booking/{booking.reference}/review").status_code, 404)
        response = self._post_review(stranger, booking)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(BookingReview.query.count(), 0)

    def test_a_session_holding_a_different_reference_gets_404(self):
        mine = self._booking(email="mine@example.com")
        theirs = self._booking(email="theirs@example.com")
        client = self._client_holding(mine)
        self.assertEqual(client.get(f"/booking/{theirs.reference}/review").status_code, 404)
        self.assertEqual(self._post_review(client, theirs).status_code, 404)
        self.assertEqual(BookingReview.query.count(), 0)

    def test_lowercase_reference_in_url_still_needs_the_session(self):
        booking = self._booking()
        stranger = self.app.test_client()
        self.assertEqual(
            stranger.get(f"/booking/{booking.reference.lower()}/review").status_code, 404)

    def test_lookup_with_reference_and_email_unlocks_it(self):
        booking = self._booking(email="Awa.Ceesay@example.com")
        client = self.app.test_client()
        self.assertEqual(client.get(f"/booking/{booking.reference}/review").status_code, 404)

        wrong = client.post("/booking", data={"reference": booking.reference,
                                              "email": "someone@example.com"})
        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(client.get(f"/booking/{booking.reference}/review").status_code, 404)

        client.post("/booking", data={"reference": booking.reference.lower(),
                                      "email": " awa.ceesay@EXAMPLE.com "})
        self.assertEqual(client.get(f"/booking/{booking.reference}/review").status_code, 200)
        self._post_review(client, booking)
        self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(), 1)

    def test_a_review_post_without_csrf_creates_nothing(self):
        booking = self._booking()
        client = self._client_holding(booking)
        client.get("/ride")
        client.post(f"/booking/{booking.reference}/review",
                    data={"rating": "5", "comment": "No token here"})
        client.post(f"/booking/{booking.reference}/review",
                    data={"csrf_token": "forged", "rating": "5", "comment": "Forged token"})
        self.assertEqual(BookingReview.query.count(), 0)

    def test_one_review_per_booking(self):
        booking = self._booking()
        client = self._client_holding(booking)
        self._post_review(client, booking, rating="5", comment="First review")
        second = self._post_review(client, booking, rating="1", comment="Second attempt")
        self.assertLess(second.status_code, 500)
        self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(), 1)
        review = BookingReview.query.filter_by(booking_id=booking.id).one()
        self.assertEqual((review.rating, review.comment), (5, "First review"))

        # A second browser that also unlocks the booking cannot add another.
        other = self._client_holding(booking)
        self._post_review(other, booking, rating="2", comment="From another browser")
        self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(), 1)

    def test_rating_must_be_a_whole_number_from_1_to_5(self):
        booking = self._booking()
        client = self._client_holding(booking)
        for bad in ("0", "6", "-1", "4.5", "5.0", "five", "", "1e0", "true", "3,5",
                    "99999999999999999999"):
            response = self._post_review(client, booking, rating=bad, comment="Fine trip")
            self.assertEqual(response.status_code, 400, repr(bad))
            self.assertEqual(BookingReview.query.count(), 0, repr(bad))
        response = client.post(f"/booking/{booking.reference}/review",
                               data={"csrf_token": self._token(client), "comment": "No rating"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BookingReview.query.count(), 0)

    def test_rating_accepts_only_ascii_digits(self):
        """A form posts ASCII digits. Arabic-Indic or full-width digits are not a
        rating the page offers and must be refused like any other odd value."""
        booking = self._booking()
        client = self._client_holding(booking)
        for bad in ("\u0663", "\uff15"):
            response = self._post_review(client, booking, rating=bad, comment="Fine trip")
            self.assertEqual(response.status_code, 400, repr(bad))
            self.assertEqual(BookingReview.query.count(), 0, repr(bad))

    def test_each_valid_rating_is_accepted(self):
        for rating in (1, 2, 3, 4, 5):
            booking = self._booking()
            client = self._client_holding(booking)
            self._post_review(client, booking, rating=str(rating), comment="Okay trip")
            review = BookingReview.query.filter_by(booking_id=booking.id).one()
            self.assertEqual(review.rating, rating)

    def test_comment_length_limits(self):
        booking = self._booking()
        client = self._client_holding(booking)
        for bad in ("", "ab", "   ab   ", "  \n\t ", "x" * 2001):
            response = self._post_review(client, booking, comment=bad)
            self.assertEqual(response.status_code, 400, repr(bad[:10]))
            self.assertEqual(BookingReview.query.count(), 0)

        self._post_review(client, booking, comment="abc")
        self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).one().comment, "abc")

        longest = self._booking()
        client = self._client_holding(longest)
        self._post_review(client, longest, comment="y" * 2000)
        self.assertEqual(len(BookingReview.query.filter_by(booking_id=longest.id).one().comment),
                         2000)

    def test_works_for_ride_transfer_and_rental(self):
        ride = self._booking("ride")
        transfer = self._booking("transfer")
        rental = self._booking("rental", vehicle=self.driver_car)
        ownerless_rental = self._booking("rental", vehicle=self.car)
        for booking in (ride, transfer, rental, ownerless_rental):
            client = self._client_holding(booking)
            self.assertEqual(client.get(f"/booking/{booking.reference}/review").status_code,
                             200, booking.booking_type)
            self._post_review(client, booking, comment=f"Good {booking.booking_type}")
            self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(), 1,
                             booking.booking_type)


# --- reviews: where they show, and how -----------------------------------------

class ReviewDisplayTests(Base):
    def _reviewed(self, booking, rating, comment):
        client = self._client_holding(booking)
        self._post_review(client, booking, rating=str(rating), comment=comment)
        self.assertEqual(BookingReview.query.filter_by(booking_id=booking.id).count(), 1)

    def test_a_ride_review_shows_on_the_driver_profile_without_the_customer(self):
        booking = self._booking("ride", name="Fatou Unique-Jallow",
                                email="fatou.private@example.com")
        self._reviewed(booking, 4, "Knew every shortcut in Serrekunda")
        page = self.app.test_client().get(f"/drivers/{self.operator.id}")
        self.assertEqual(page.status_code, 200)
        text = visible_text(page.data)
        self.assertIn("Knew every shortcut in Serrekunda", text)
        self.assertRegex(text, r"\b4\s*/\s*5\b")
        raw = page.data.decode()
        for private in ("Fatou", "Unique-Jallow", "fatou.private@example.com", "+220877001111",
                        booking.reference):
            self.assertNotIn(private, raw, private)

    def test_a_transfer_review_shows_on_the_driver_profile(self):
        booking = self._booking("transfer", name="Modou Secretname")
        self._reviewed(booking, 3, "Waited at arrivals")
        raw = self.app.test_client().get(f"/drivers/{self.operator.id}").data.decode()
        self.assertIn("Waited at arrivals", raw)
        self.assertNotIn("Secretname", raw)
        self.assertNotIn("awa@example.com", raw)
        # And not on the rival's profile.
        rival = self.app.test_client().get(f"/drivers/{self.rival.id}").data.decode()
        self.assertNotIn("Waited at arrivals", rival)

    def test_a_rental_review_shows_on_the_car_and_the_driver(self):
        booking = self._booking("rental", vehicle=self.driver_car, name="Isatou Hiddenname",
                                email="isatou.hidden@example.com")
        self._reviewed(booking, 5, "Tucson was spotless")
        car_page = self.app.test_client().get(f"/fleet/{self.driver_car.id}")
        self.assertEqual(car_page.status_code, 200)
        car_text = visible_text(car_page.data)
        self.assertIn("Tucson was spotless", car_text)
        self.assertRegex(car_text, r"\b5\s*/\s*5\b")
        self.assertNotIn("Hiddenname", car_page.data.decode())
        self.assertNotIn("isatou.hidden@example.com", car_page.data.decode())

        profile = self.app.test_client().get(f"/drivers/{self.operator.id}")
        self.assertIn("Tucson was spotless", visible_text(profile.data))
        self.assertNotIn("Hiddenname", profile.data.decode())

        # A different car's page does not show it.
        other_car = Vehicle(make="Kia", model="Rio", year=2019, daily_rate=2000, deposit=1000,
                            is_active=True, operator_id=self.operator.id)
        db.session.add(other_car)
        db.session.commit()
        self.assertNotIn("Tucson was spotless",
                         self.app.test_client().get(f"/fleet/{other_car.id}").data.decode())

    def test_a_ride_review_does_not_show_on_a_car_page(self):
        booking = self._booking("ride", vehicle=self.driver_car)
        self._reviewed(booking, 2, "Ride review only")
        self.assertNotIn("Ride review only",
                         self.app.test_client().get(f"/fleet/{self.driver_car.id}").data.decode())

    def test_comments_are_html_escaped(self):
        payload = "<script>alert(1)</script><img src=x onerror=alert(2)>"
        ride = self._booking("ride")
        rental = self._booking("rental", vehicle=self.driver_car)
        self._reviewed(ride, 5, "Nice " + payload)
        self._reviewed(rental, 5, "Car " + payload)
        pages = [f"/drivers/{self.operator.id}", f"/fleet/{self.driver_car.id}", "/drivers"]
        for path in pages:
            raw = self.app.test_client().get(path).data.decode()
            self.assertNotIn("<script>alert(1)</script>", raw, path)
            self.assertNotIn("<img src=x onerror", raw, path)
        profile = self.app.test_client().get(f"/drivers/{self.operator.id}").data.decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", profile)
        # The thank-you page repeats the comment back: also escaped.
        thanks = self._client_holding(ride).get(f"/booking/{ride.reference}/review").data.decode()
        self.assertNotIn("<script>alert(1)</script>", thanks)

    def test_reviews_of_non_completed_bookings_never_count(self):
        good = self._booking("ride")
        db.session.add(BookingReview(booking_id=good.id, rating=5, comment="Real trip"))
        for status in ("pending", "confirmed", "cancelled", "in_progress", "expired"):
            booking = self._booking("ride", status=status)
            db.session.add(BookingReview(booking_id=booking.id, rating=1, comment=f"Fake {status}"))
        rental = self._booking("rental", vehicle=self.driver_car, status="cancelled")
        db.session.add(BookingReview(booking_id=rental.id, rating=1, comment="Fake rental"))
        db.session.commit()

        self.assertEqual(reviews_module.review_summary(operator_id=self.operator.id),
                         {"review_count": 1, "rating": 5.0})
        self.assertEqual(reviews_module.review_summary(operator_id=self.operator.id,
                                                       journeys_only=True),
                         {"review_count": 1, "rating": 5.0})
        self.assertEqual(reviews_module.review_summary(vehicle_id=self.driver_car.id),
                         {"review_count": 0, "rating": None})

        raw = self.app.test_client().get(f"/drivers/{self.operator.id}").data.decode()
        self.assertNotIn("Fake", raw)
        self.assertIn("Real trip", raw)
        self.assertNotIn("Fake rental",
                         self.app.test_client().get(f"/fleet/{self.driver_car.id}").data.decode())

    def test_a_review_stops_counting_when_its_booking_is_un_completed(self):
        booking = self._booking("rental", vehicle=self.driver_car)
        self._reviewed(booking, 2, "Was completed once")
        self.assertEqual(reviews_module.review_summary(vehicle_id=self.driver_car.id)["review_count"], 1)
        booking.status = "cancelled"
        db.session.commit()
        self.assertEqual(reviews_module.review_summary(vehicle_id=self.driver_car.id),
                         {"review_count": 0, "rating": None})
        self.assertEqual(reviews_module.review_summary(operator_id=self.operator.id)["review_count"], 0)


# --- rentals of cars that belong to drivers ------------------------------------

class RentalTests(Base):
    def _book(self, vehicle, client=None, **overrides):
        client = client or self.app.test_client()
        start = date.today() + timedelta(days=5)
        data = {
            "start": start.isoformat(), "end": (start + timedelta(days=3)).isoformat(),
            "customer_name": "Lamin Touray", "email": "lamin@example.com",
            "phone": "+220877002222",
            "pickup_location": current_settings()["locations"][0],
            "dropoff_location": current_settings()["locations"][0],
        }
        data.update(overrides)
        return client.post(f"/fleet/{vehicle.id}/book", data=data)

    def test_booking_a_driver_car_stores_the_driver_and_deposit(self):
        response = self._book(self.driver_car)
        self.assertEqual(response.status_code, 302)
        booking = Booking.query.filter_by(vehicle_id=self.driver_car.id).one()
        self.assertEqual(booking.booking_type, "rental")
        self.assertEqual(booking.operator_id, self.operator.id)
        self.assertEqual(Decimal(str(booking.deposit_amount)), Decimal("7000.00"))
        self.assertEqual(Decimal(str(booking.total_price)), Decimal("9000.00"))

    def test_the_booked_deposit_does_not_follow_later_changes(self):
        self._book(self.driver_car)
        self.driver_car.deposit = Decimal("1.00")
        db.session.commit()
        booking = Booking.query.filter_by(vehicle_id=self.driver_car.id).one()
        self.assertEqual(Decimal(str(booking.deposit_amount)), Decimal("7000.00"))

    def test_commission_on_completion_excludes_the_deposit(self):
        self._book(self.driver_car)
        booking = Booking.query.filter_by(vehicle_id=self.driver_car.id).one()
        admin, token = self._admin()
        admin.post(f"/admin/bookings/{booking.id}/status",
                   data={"csrf_token": token, "status": "confirmed"})
        admin.post(f"/admin/bookings/{booking.id}/status",
                   data={"csrf_token": token, "status": "completed"})
        db.session.expire_all()
        booking = db.session.get(Booking, booking.id)
        self.assertEqual(booking.status, "completed")
        entry = CommissionEntry.query.filter_by(booking_id=booking.id).one()
        self.assertEqual(entry.operator_id, self.operator.id)
        self.assertEqual(Decimal(str(entry.base_amount)), Decimal("9000.00"))
        self.assertEqual(Decimal(str(entry.amount)), Decimal("450.00"))

    def _assert_hidden(self, vehicle):
        link = f"/fleet/{vehicle.id}"
        for path in ("/", "/fleet"):
            raw = self.app.test_client().get(path).data.decode()
            self.assertNotRegex(raw, re.escape(link) + r"(?![0-9])", path)
            self.assertNotIn("Zebra Phantom", raw, path)
        self.assertEqual(self.app.test_client().get(link).status_code, 404)
        before = Booking.query.count()
        self.assertEqual(self._book(vehicle).status_code, 404)
        self.assertEqual(Booking.query.count(), before)

    def _hidden_car(self, status):
        driver = self._operator(f"Driver {status}", f"{status}@example.com", status=status)
        car = Vehicle(make="Zebra", model="Phantom", year=2022, daily_rate=Decimal("1.00"),
                      deposit=Decimal("10.00"), is_active=True, operator_id=driver.id)
        db.session.add(car)
        db.session.commit()
        return driver, car

    def test_a_car_whose_driver_is_suspended_is_absent(self):
        _, car = self._hidden_car("suspended")
        self._assert_hidden(car)

    def test_a_car_whose_driver_is_pending_is_absent(self):
        _, car = self._hidden_car("pending")
        self._assert_hidden(car)

    def test_suspending_an_approved_driver_hides_their_listed_car(self):
        driver, car = self._hidden_car("approved")
        self.assertIn(f"/fleet/{car.id}", self.app.test_client().get("/fleet").data.decode())
        self.assertEqual(self.app.test_client().get(f"/fleet/{car.id}").status_code, 200)
        driver.status = "suspended"
        db.session.commit()
        self._assert_hidden(car)

    def test_an_approved_driver_car_is_listed(self):
        raw = self.app.test_client().get("/fleet").data.decode()
        self.assertIn(f"/fleet/{self.driver_car.id}", raw)


# --- the admin vehicle form ------------------------------------------------------

class AdminVehicleFormTests(Base):
    def _form(self, **overrides):
        data = {"make": "Mitsubishi", "model": "Pajero", "year": "2018",
                "category": "SUV", "transmission": "Manual", "fuel": "Diesel",
                "daily_rate": "4000", "weekly_rate": "", "deposit": "6000",
                "seats": "7", "doors": "5", "luggage": "3", "is_active": "on"}
        data.update(overrides)
        return data

    def test_a_car_can_be_linked_to_a_driver(self):
        admin, token = self._admin()
        response = admin.post("/admin/vehicles/new",
                              data=self._form(csrf_token=token, operator_id=str(self.rival.id)))
        self.assertEqual(response.status_code, 302)
        car = Vehicle.query.filter_by(model="Pajero").one()
        self.assertEqual(car.operator_id, self.rival.id)

    def test_editing_can_move_or_unlink_a_car(self):
        admin, token = self._admin()
        admin.post(f"/admin/vehicles/{self.car.id}/edit",
                   data=self._form(csrf_token=token, model="Corolla",
                                   operator_id=str(self.operator.id)))
        db.session.expire_all()
        self.assertEqual(db.session.get(Vehicle, self.car.id).operator_id, self.operator.id)
        admin.post(f"/admin/vehicles/{self.car.id}/edit",
                   data=self._form(csrf_token=token, model="Corolla", operator_id=""))
        db.session.expire_all()
        self.assertIsNone(db.session.get(Vehicle, self.car.id).operator_id)

    def test_an_unknown_driver_id_is_rejected(self):
        admin, token = self._admin()
        for bad in ("99999", "abc", "-1", "1.5", "0"):
            response = admin.post("/admin/vehicles/new",
                                  data=self._form(csrf_token=token, operator_id=bad))
            self.assertNotEqual(response.status_code, 302, bad)
            self.assertLess(response.status_code, 500, bad)
            self.assertEqual(Vehicle.query.filter_by(model="Pajero").count(), 0, bad)

    def test_an_unknown_driver_id_on_edit_leaves_the_link_alone(self):
        admin, token = self._admin()
        admin.post(f"/admin/vehicles/{self.driver_car.id}/edit",
                   data=self._form(csrf_token=token, model="Tucson", operator_id="424242"))
        db.session.expire_all()
        car = db.session.get(Vehicle, self.driver_car.id)
        self.assertEqual(car.operator_id, self.operator.id)
        self.assertEqual(car.make, "Hyundai")


# --- wording ----------------------------------------------------------------------

class WordingTests(Base):
    def setUp(self):
        super().setUp()
        # Enough on the driver's account that every section of their pages renders.
        self.ride_fare = OperatorFare(
            operator_id=self.operator.id, kind="ride", title="Around town",
            from_location="Anywhere", to_location="Anywhere", pricing_model="distance",
            base_price=Decimal("100"), per_km=Decimal("40"), seats=4, is_active=True)
        db.session.add(self.ride_fare)
        db.session.add(DriverState(operator_id=self.operator.id, vehicle_id=self.driver_car.id,
                                   available=True, lat=13.45, lng=-16.68,
                                   updated_at=datetime.utcnow()))
        db.session.commit()
        pending = self._booking("transfer", status="pending")
        done = self._booking("ride", status="completed")
        rental = self._booking("rental", vehicle=self.driver_car, status="confirmed")
        db.session.add(BookingReview(booking_id=done.id, rating=4, comment="Pleasant ride"))
        db.session.commit()
        self.bookings = (pending, done, rental)

    PUBLIC_PAGES = ("/", "/ride", "/rides", "/fleet", "/drivers", "/driver/join",
                    "/driver/sign-in", "/operator/login", "/booking")
    DRIVER_PAGES = ("/operator/", "/operator/drive", "/operator/fares", "/operator/bookings")

    def _problems(self, raw):
        text = visible_text(raw)
        found = [m.group(0) for m in FORBIDDEN_WORDS.finditer(text)]
        found += [m.group(0) for p in FORBIDDEN_PHRASES for m in p.finditer(text)]
        return found, text

    def _context(self, text, word):
        index = text.lower().find(word.lower())
        return text[max(0, index - 60): index + 60]

    def _check_pages(self, client, paths):
        failures = {}
        for path in paths:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            found, text = self._problems(response.data)
            if found:
                failures[path] = [(word, self._context(text, word)) for word in found[:5]]
        self.assertEqual(failures, {})

    def test_public_pages_say_driver_only(self):
        self._check_pages(self.app.test_client(),
                          self.PUBLIC_PAGES + (f"/drivers/{self.operator.id}",))

    def test_signed_in_driver_pages_say_driver_only(self):
        client = self._signed_in_driver()
        self._check_pages(client, self.DRIVER_PAGES)
        booking_page = client.get(f"/operator/bookings/{self.bookings[0].id}")
        self.assertEqual(booking_page.status_code, 200)
        self.assertEqual(self._problems(booking_page.data)[0], [])

    def test_the_email_sign_in_page_signed_in_by_password_says_driver_only(self):
        client = self._sign_in_operator()
        dashboard = client.get("/operator/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(self._problems(dashboard.data)[0], [])

    def test_the_header_offers_driver_links_when_signed_out(self):
        for path in self.PUBLIC_PAGES + (f"/drivers/{self.operator.id}",):
            raw = self.app.test_client().get(path).data.decode()
            header = re.search(r'<header class="site-header".*?</header>', raw, re.S)
            self.assertIsNotNone(header, path)
            header = header.group(0)
            links = {visible_text(label).strip(): href for href, label in
                     re.findall(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', header, re.S)}
            self.assertEqual(links.get("Become a driver"), "/driver/join", path)
            self.assertEqual(links.get("Driver sign in"), "/driver/sign-in", path)
            header_text = visible_text(header)
            self.assertIsNone(FORBIDDEN_WORDS.search(header_text), path)

    def test_the_header_swaps_to_the_dashboard_when_signed_in(self):
        raw = self._signed_in_driver().get("/").data.decode()
        header = re.search(r'<header class="site-header".*?</header>', raw, re.S).group(0)
        self.assertNotIn("Become a driver", visible_text(header))

    def test_no_page_claims_staff_accounts_or_drivers_managing_cars(self):
        anonymous = self.app.test_client()
        driver = self._signed_in_driver()
        pages = [(anonymous, p) for p in self.PUBLIC_PAGES + (
            f"/drivers/{self.operator.id}", f"/fleet/{self.driver_car.id}", "/about",
            "/contact", "/driver/status")]
        pages += [(driver, p) for p in self.DRIVER_PAGES + ("/driver/status",)]
        failures = {}
        for client, path in pages:
            text = visible_text(client.get(path).data)
            hits = [m.group(0) for p in FALSE_CLAIMS for m in p.finditer(text)]
            if hits:
                failures[path] = hits
        self.assertEqual(failures, {})

    def test_the_visible_text_helper_ignores_urls_and_attributes(self):
        sample = ('<a href="/operator/drive" class="operator-link" data-x="partner">Driving</a>'
                  '<script>var provider = 1;</script><!-- operator -->')
        self.assertIsNone(FORBIDDEN_WORDS.search(visible_text(sample)))
        self.assertIsNotNone(FORBIDDEN_WORDS.search(visible_text("<p>Our Partners</p>")))


# --- old joining routes -----------------------------------------------------------

class OldOperatorRoutesTests(Base):
    def test_operators_redirects_permanently_to_join(self):
        response = self.app.test_client().get("/operators")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.location.endswith("/driver/join"), response.location)

    def test_operators_apply_creates_nothing(self):
        before = Operator.query.count()
        client = self.app.test_client()
        client.get("/")
        with client.session_transaction() as session:
            token = session.get("_csrf_token", "")
        response = client.post("/operators/apply", data={
            "csrf_token": token, "name": "Sneaky Cabs", "contact_name": "Sneak",
            "email": "sneak@example.com", "phone": "+220877009999",
            "password": "a-long-password-123", "service_area": "Banjul",
            "terms": "on"})
        self.assertLess(response.status_code, 500)
        self.assertEqual(Operator.query.count(), before)
        self.assertIsNone(Operator.query.filter_by(email="sneak@example.com").first())


# --- templates ---------------------------------------------------------------------

class TemplateSafetyTests(Base):
    def test_templates_never_build_markup_from_strings(self):
        paths = glob.glob(os.path.join(APP_DIR, "templates", "**", "*.html"), recursive=True)
        self.assertGreater(len(paths), 10)
        offenders = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for needle in ("innerHTML", "insertAdjacentHTML"):
                if needle in source:
                    offenders.append((os.path.relpath(path, APP_DIR), needle))
        self.assertEqual(offenders, [])
