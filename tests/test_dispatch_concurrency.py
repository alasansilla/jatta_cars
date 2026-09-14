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


# Open offers (a trip any driver could claim) were removed: a trip only ever goes
# to a driver the customer chose. Acceptance races are covered in
# tests/test_driver_console.py.

if __name__ == "__main__":
    unittest.main()
