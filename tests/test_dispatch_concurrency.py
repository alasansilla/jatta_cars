"""One driver, two customers, same instant — exactly one may win.

The reservation is a single guarded UPDATE:

    UPDATE driver_states SET active_booking_id = :booking, available = 0
     WHERE operator_id = :op AND available = 1 AND active_booking_id IS NULL ...

and the caller checks `rowcount == 1`. That check is the whole safety property:
without it, two requests that both read "available" a microsecond apart would
both go on to write, and one car would be promised to two people.

These tests race real database sessions rather than calling the update twice in
a row, because a sequential call proves only that the WHERE clause reads the
committed state — not that two in flight at once cannot both succeed.

A file-backed database is used because threads cannot share an in-memory SQLite.
It lives in a temporary directory and is deleted afterwards; the real database is
never opened.
"""
import os
import shutil
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import create_app
from app.models import Booking, DriverState, Operator, Vehicle, db
from config import Config


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "race.db")

        class FileConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.path}"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-only-" + "r" * 40
            STORAGE_BACKEND = "local"
            GEOCODER_URL = ""
            ROUTER_URL = ""

        self.app = create_app(FileConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.operator = Operator(name="Kololi Cabs", slug="kololi",
                                 email="k@example.com", status="approved")
        self.operator.set_password("driver-password")
        db.session.add(self.operator)
        db.session.commit()
        # Plain ids, not ORM objects: setUp closes the session and each thread
        # opens its own, so an attached instance would be detached by the time a
        # test touched it.
        self.operator_id = self.operator.id

        self.car = Vehicle(make="Toyota", model="Corolla", year=2021,
                           daily_rate=10000, deposit=0, is_active=True,
                           operator_id=self.operator_id)
        db.session.add(self.car)
        db.session.commit()
        self.car_id = self.car.id

        # One driver, free, with a fresh GPS fix.
        db.session.add(DriverState(
            operator_id=self.operator_id, vehicle_id=self.car_id, available=True,
            active_booking_id=None, lat=13.44, lng=-16.68,
            updated_at=datetime.utcnow()))
        db.session.commit()

        self.first = self._booking()
        self.second = self._booking()
        db.session.remove()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _booking(self):
        today = date.today()
        booking = Booking(
            reference=Booking.new_reference(), booking_type="ride",
            operator_id=self.operator_id, vehicle_id=self.car_id,
            customer_name="Customer", email="c@example.com",
            pickup_location="Kololi", dropoff_location="Banjul",
            pickup_at=datetime.utcnow(), start_date=today, end_date=today,
            total_price=Decimal("1500.00"), deposit_amount=0, status="pending")
        db.session.add(booking)
        db.session.commit()
        return booking.id

    def _reserve(self, booking_id):
        """The production guard, run on its own connection."""
        engine = create_engine(f"sqlite:///{self.path}")
        try:
            with Session(engine) as session:
                cutoff = datetime.utcnow() - timedelta(minutes=2)
                result = session.execute(
                    update(DriverState)
                    .where(DriverState.operator_id == self.operator_id,
                           DriverState.available.is_(True),
                           DriverState.active_booking_id.is_(None),
                           DriverState.vehicle_id == self.car_id,
                           DriverState.updated_at >= cutoff)
                    .values(active_booking_id=booking_id, available=False))
                won = result.rowcount == 1
                if won:
                    session.commit()
                else:
                    session.rollback()
                return won
        except OperationalError:
            # SQLite refused the second writer outright. That is a loss, not a win.
            return False
        finally:
            engine.dispose()

    def test_two_simultaneous_requests_cannot_both_take_one_driver(self):
        outcomes, ready = [], threading.Barrier(2)

        def attempt(booking_id):
            ready.wait()                     # start both at the same instant
            outcomes.append((booking_id, self._reserve(booking_id)))

        threads = [threading.Thread(target=attempt, args=(booking,))
                   for booking in (self.first, self.second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        winners = [booking for booking, won in outcomes if won]
        self.assertEqual(len(outcomes), 2, "a thread never finished")
        self.assertEqual(len(winners), 1,
                         f"expected exactly one winner, got {winners}")

        # And the database agrees with whoever was told they won.
        db.session.remove()
        state = db.session.get(DriverState, self.operator_id)
        self.assertEqual(state.active_booking_id, winners[0])
        self.assertFalse(state.available)

    def test_many_simultaneous_requests_still_yield_one_winner(self):
        bookings = [self._booking() for _ in range(8)]
        db.session.remove()

        outcomes, ready = [], threading.Barrier(len(bookings))

        def attempt(booking_id):
            ready.wait()
            outcomes.append(self._reserve(booking_id))

        threads = [threading.Thread(target=attempt, args=(booking,))
                   for booking in bookings]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(outcomes), len(bookings), "a thread never finished")
        self.assertEqual(sum(1 for won in outcomes if won), 1,
                         "more than one request reserved the same driver")

    def test_a_reserved_driver_is_not_offered_again(self):
        self.assertTrue(self._reserve(self.first))
        self.assertFalse(self._reserve(self.second),
                         "a busy driver was reserved a second time")

    def test_a_driver_with_a_stale_fix_is_not_reservable(self):
        """Out of contact for two minutes means not dispatchable."""
        db.session.remove()
        state = db.session.get(DriverState, self.operator_id)
        state.updated_at = datetime.utcnow() - timedelta(minutes=10)
        db.session.commit()
        db.session.remove()

        self.assertFalse(self._reserve(self.first))

    def test_an_unavailable_driver_is_not_reservable(self):
        db.session.remove()
        state = db.session.get(DriverState, self.operator_id)
        state.available = False
        db.session.commit()
        db.session.remove()

        self.assertFalse(self._reserve(self.first))


if __name__ == "__main__":
    unittest.main()


class OfferAcceptanceTests(ConcurrencyTests):
    """An unclaimed trip, several drivers, one winner.

    This is the case the brief cares about: two drivers pressing accept on the
    same offer at the same moment. The reservation path proves a *customer*
    cannot double-book one driver; this proves a *driver* cannot double-claim
    one trip.
    """

    def _open_offer(self):
        """A ride nobody has claimed."""
        booking_id = self._booking()
        db.session.remove()
        booking = db.session.get(Booking, booking_id)
        booking.operator_id = None
        booking.vehicle_id = None
        booking.status = "pending"
        db.session.commit()
        db.session.remove()
        return booking_id

    def _driver_ready(self, operator_id, vehicle_id):
        """An operator online, free, with a fresh fix — able to accept."""
        state = db.session.get(DriverState, operator_id)
        if state is None:
            state = DriverState(operator_id=operator_id)
            db.session.add(state)
        state.vehicle_id = vehicle_id
        state.available = True
        state.active_booking_id = None
        state.lat, state.lng = 13.44, -16.68
        state.updated_at = datetime.utcnow()
        db.session.commit()

    def _second_operator(self):
        rival = Operator(name="Rival Rides", slug="rival", email="r@example.com",
                         status="approved")
        rival.set_password("driver-password")
        db.session.add(rival)
        db.session.commit()
        rival_id = rival.id
        car = Vehicle(make="Kia", model="Rio", year=2020, daily_rate=9000,
                      deposit=0, is_active=True, operator_id=rival_id)
        db.session.add(car)
        db.session.commit()
        return rival_id, car.id

    def _claim(self, operator_id, booking_id):
        """The production guard pair, on its own connection."""
        engine = create_engine(f"sqlite:///{self.path}")
        try:
            with Session(engine) as session:
                claimed = session.execute(update(Booking).where(
                    Booking.id == booking_id,
                    Booking.booking_type == "ride",
                    Booking.status == "pending",
                    Booking.operator_id.is_(None),
                ).values(operator_id=operator_id, status="accepted"))
                if claimed.rowcount != 1:
                    session.rollback()
                    return False
                held = session.execute(update(DriverState).where(
                    DriverState.operator_id == operator_id,
                    DriverState.available.is_(True),
                    DriverState.active_booking_id.is_(None),
                ).values(active_booking_id=booking_id, available=False))
                if held.rowcount != 1:
                    session.rollback()
                    return False
                session.commit()
                return True
        except OperationalError:
            return False
        finally:
            engine.dispose()

    def test_two_drivers_cannot_both_claim_one_trip(self):
        rival_id, rival_car = self._second_operator()
        self._driver_ready(self.operator_id, self.car_id)
        self._driver_ready(rival_id, rival_car)
        offer = self._open_offer()

        outcomes, ready = [], threading.Barrier(2)

        def attempt(operator_id):
            ready.wait()
            outcomes.append((operator_id, self._claim(operator_id, offer)))

        threads = [threading.Thread(target=attempt, args=(op,))
                   for op in (self.operator_id, rival_id)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        winners = [op for op, won in outcomes if won]
        self.assertEqual(len(outcomes), 2, "a thread never finished")
        self.assertEqual(len(winners), 1, f"expected one winner, got {winners}")

        db.session.remove()
        booking = db.session.get(Booking, offer)
        self.assertEqual(booking.status, "accepted")
        self.assertEqual(booking.operator_id, winners[0],
                         "the trip went to a driver who was told they lost")
        loser = rival_id if winners[0] == self.operator_id else self.operator_id
        self.assertIsNone(db.session.get(DriverState, loser).active_booking_id,
                          "the losing driver was left holding the trip")

    def test_a_claimed_trip_cannot_be_claimed_again(self):
        rival_id, rival_car = self._second_operator()
        self._driver_ready(self.operator_id, self.car_id)
        self._driver_ready(rival_id, rival_car)
        offer = self._open_offer()

        self.assertTrue(self._claim(self.operator_id, offer))
        self.assertFalse(self._claim(rival_id, offer),
                         "a trip already under way was claimed a second time")


class OfferPrivacyTests(unittest.TestCase):
    """Deciding whether to take a job must not reveal who is waiting."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        path = os.path.join(self.directory, "offers.db")

        class FileConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-only-" + "o" * 40
            STORAGE_BACKEND = "local"
            GEOCODER_URL = ""
            ROUTER_URL = ""

        self.app = create_app(FileConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from app.settings import save_settings
        save_settings({"site_live": True})

        self.operator = Operator(name="Kololi Cabs", slug="kololi",
                                 email="k@example.com", status="approved")
        self.operator.set_password("driver-password")
        db.session.add(self.operator)
        db.session.commit()
        self.operator_id = self.operator.id

        car = Vehicle(make="Toyota", model="Corolla", year=2021, daily_rate=10000,
                      deposit=0, is_active=True, operator_id=self.operator_id)
        db.session.add(car)
        db.session.commit()

        db.session.add(DriverState(operator_id=self.operator_id, vehicle_id=car.id,
                                   available=True, active_booking_id=None,
                                   lat=13.44, lng=-16.68,
                                   updated_at=datetime.utcnow()))
        today = date.today()
        db.session.add(Booking(
            reference="JC-OFFER1", booking_type="ride", operator_id=None,
            customer_name="Awa Ceesay", email="awa@example.com", phone="+220700111",
            pickup_location="Kololi", dropoff_location="Banjul",
            pickup_address="Senegambia", dropoff_address="Airport",
            pickup_at=datetime.utcnow(), start_date=today, end_date=today,
            total_price=Decimal("1500.00"), deposit_amount=0, status="pending"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _driver(self):
        client = self.app.test_client()
        client.post("/operator/login", data={"email": "k@example.com",
                                             "password": "driver-password"})
        return client

    def test_an_offer_shows_the_job_not_the_person(self):
        body = self._driver().get("/operator/drive/offers").get_json()
        self.assertEqual(len(body["offers"]), 1)
        offer = body["offers"][0]
        self.assertEqual(offer["pickup"], "Senegambia")
        self.assertIsNotNone(offer["fare"])

        rendered = str(offer)
        for private in ("Awa", "awa@example.com", "+220700111"):
            self.assertNotIn(private, rendered,
                             "an offer leaked the customer's details")

    def test_offers_are_closed_to_the_public(self):
        anonymous = self.app.test_client()
        self.assertNotEqual(anonymous.get("/operator/drive/offers").status_code, 200)

    def test_a_busy_driver_is_offered_nothing(self):
        state = db.session.get(DriverState, self.operator_id)
        state.active_booking_id = Booking.query.first().id
        state.available = False
        db.session.commit()
        body = self._driver().get("/operator/drive/offers").get_json()
        self.assertEqual(body["offers"], [])
