"""Settling commission: what a driver owes, what they have paid, and the trail.

Nothing is collected online. Commission is earned when a booking completes, and
a driver hands the money over afterwards, so the only honest record is one
somebody writes down. These entries are append-only: a mistake is put right by
recording the opposite amount with a note, which leaves both rows visible.
"""
import unittest
from datetime import date, timedelta
from decimal import Decimal

from app import commission, create_app
from app.models import AdminUser, Booking, CommissionSettlement, Operator, Vehicle, db
from app.settings import save_settings
from config import Config


class LedgerCase(unittest.TestCase):
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
        save_settings({"site_live": True, "commission_rate": "5"})
        self.staff = AdminUser(username="admin")
        self.staff.set_password("admin-password-long")
        self.driver = Operator(name="Awa Jallow", slug="awa-jallow", status="approved")
        self.other = Operator(name="Musa Touray", slug="musa-touray", status="approved")
        db.session.add_all([self.staff, self.driver, self.other])
        db.session.commit()
        self.car = Vehicle(make="Toyota", model="Yaris", year=2020, daily_rate=2500,
                           deposit=7500, operator_id=self.driver.id, is_active=True,
                           service_mode="rental")
        db.session.add(self.car)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def completed_booking(self, operator=None, fare=10000, deposit=7500):
        """A finished rental, which is what earns commission."""
        start = date.today() - timedelta(days=5)
        booking = Booking(reference=Booking.new_reference(), booking_type="rental",
                          vehicle_id=self.car.id, operator_id=(operator or self.driver).id,
                          customer_name="Fatou Ceesay", email="f@example.com",
                          phone="+2207000000", pickup_location="Kololi",
                          dropoff_location="Kololi", start_date=start,
                          end_date=start + timedelta(days=2), total_price=fare,
                          deposit_amount=deposit, status="completed")
        db.session.add(booking)
        db.session.commit()
        commission.sync_for(booking)
        db.session.commit()
        return booking

    def admin(self):
        client = self.app.test_client()
        client.post("/admin/login", data={"username": "admin", "password": "admin-password-long"})
        client.get("/admin/commission")
        return client

    def token(self, client):
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def settle(self, client, operator, **fields):
        data = {"csrf_token": self.token(client), "amount": "500", "method": "cash"}
        data.update(fields)
        return client.post(f"/admin/commission/{operator.id}/settle", data=data,
                           follow_redirects=True)


class WhatIsOwed(LedgerCase):
    def test_owed_is_what_was_earned_until_something_is_paid(self):
        self.completed_booking(fare=10000)      # 5% of the fare, not the deposit
        self.assertEqual(commission.totals(self.driver), Decimal("500.00"))
        self.assertEqual(commission.settled(self.driver), Decimal("0.00"))
        self.assertEqual(commission.outstanding(self.driver), Decimal("500.00"))

        commission.record_settlement(self.driver, "200", "cash", recorded_by=self.staff)
        db.session.commit()
        self.assertEqual(commission.outstanding(self.driver), Decimal("300.00"))

    def test_each_driver_stands_alone(self):
        self.completed_booking(self.driver, fare=10000)
        self.completed_booking(self.other, fare=20000)
        commission.record_settlement(self.driver, "500", "cash", recorded_by=self.staff)
        db.session.commit()

        self.assertEqual(commission.outstanding(self.driver), Decimal("0.00"))
        self.assertEqual(commission.outstanding(self.other), Decimal("1000.00"))
        self.assertEqual(commission.outstanding(), Decimal("1000.00"))

    def test_paying_more_than_owed_shows_as_overpaid_not_as_a_debt(self):
        self.completed_booking(fare=10000)
        commission.record_settlement(self.driver, "800", "cash", recorded_by=self.staff)
        db.session.commit()
        self.assertEqual(commission.outstanding(self.driver), Decimal("-300.00"))

    def test_a_cancelled_booking_leaves_nothing_owed(self):
        booking = self.completed_booking(fare=10000)
        booking.status = "cancelled"
        commission.sync_for(booking)
        db.session.commit()
        self.assertEqual(commission.outstanding(self.driver), Decimal("0.00"))


class RecordingAPayment(LedgerCase):
    def test_staff_record_a_payment_and_the_page_shows_it(self):
        self.completed_booking(fare=10000)
        client = self.admin()
        page = self.settle(client, self.driver, amount="500", method="mobile money",
                           reference="MM-4471").get_data(as_text=True)
        self.assertIn("Recorded a payment", page)
        payment = CommissionSettlement.query.one()
        self.assertEqual((payment.amount, payment.method, payment.reference),
                         (Decimal("500.00"), "mobile money", "MM-4471"))
        self.assertEqual(payment.recorded_by_id, self.staff.id)
        self.assertIn("MM-4471", client.get("/admin/commission").get_data(as_text=True))

    def test_a_correction_is_a_negative_entry_and_needs_a_note(self):
        client = self.admin()
        refused = self.settle(client, self.driver, amount="-100", note="")
        self.assertIn("correction needs a note", refused.get_data(as_text=True))
        self.assertEqual(CommissionSettlement.query.count(), 0)

        self.settle(client, self.driver, amount="-100", note="Recorded 600 instead of 500")
        correction = CommissionSettlement.query.one()
        self.assertEqual(correction.amount, Decimal("-100.00"))
        self.assertTrue(correction.is_correction)

    def test_nothing_is_ever_edited_or_deleted(self):
        """The trail is the point: there is no route that changes a row."""
        self.settle(self.admin(), self.driver, amount="500")
        payment = CommissionSettlement.query.one()
        routes = [str(rule) for rule in self.app.url_map.iter_rules()
                  if "commission" in str(rule)]
        self.assertEqual(sorted(routes),
                         ["/admin/commission", "/admin/commission/<int:operator_id>/settle"])
        self.assertEqual(db.session.get(CommissionSettlement, payment.id).amount,
                         Decimal("500.00"))

    def test_amounts_that_make_no_sense_are_refused(self):
        client = self.admin()
        for amount, message in [("", "as a number"), ("abc", "as a number"),
                                ("0", "other than zero"), ("99999999999", "looks wrong")]:
            page = self.settle(client, self.driver, amount=amount, note="x")
            self.assertIn(message, page.get_data(as_text=True), amount)
        self.assertIn("Choose how the payment was made",
                      self.settle(client, self.driver, method="bitcoin").get_data(as_text=True))
        self.assertEqual(CommissionSettlement.query.count(), 0)

    def test_money_is_rounded_to_the_penny_half_up(self):
        commission.record_settlement(self.driver, "10.005", "cash", recorded_by=self.staff)
        db.session.commit()
        self.assertEqual(CommissionSettlement.query.one().amount, Decimal("10.01"))

    def test_a_thousands_separator_is_understood(self):
        commission.record_settlement(self.driver, "1,500", "cash", recorded_by=self.staff)
        db.session.commit()
        self.assertEqual(CommissionSettlement.query.one().amount, Decimal("1500.00"))


class WhoMaySeeIt(LedgerCase):
    def test_the_ledger_is_staff_only(self):
        anonymous = self.app.test_client()
        page = anonymous.get("/admin/commission", follow_redirects=True)
        self.assertIn("Please sign in to continue", page.get_data(as_text=True))

        # A post without the session token never reaches the view at all, and
        # signing in is where the redirect chain ends.
        posted = anonymous.post(f"/admin/commission/{self.driver.id}/settle",
                                data={"amount": "500", "method": "cash"},
                                follow_redirects=True)
        self.assertIn("Sign in", posted.get_data(as_text=True))
        self.assertEqual(CommissionSettlement.query.count(), 0)

    def test_a_driver_sees_their_own_ledger_and_nobody_else_s(self):
        self.completed_booking(self.driver, fare=10000)
        self.completed_booking(self.other, fare=40000)
        commission.record_settlement(self.driver, "200", "cash", note="first instalment",
                                     recorded_by=self.staff)
        commission.record_settlement(self.other, "1234", "bank transfer", recorded_by=self.staff)
        db.session.commit()

        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = self.driver.id
        page = client.get("/operator/").get_data(as_text=True)
        self.assertIn("first instalment", page)
        self.assertIn("You still owe", page)
        self.assertNotIn("1234", page)          # the other driver's payment
        self.assertNotIn("Musa Touray", page)

    def test_a_driver_with_nothing_recorded_is_told_so_plainly(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = self.driver.id
        self.assertIn("Nothing recorded yet", client.get("/operator/").get_data(as_text=True))


class TheSetupChecklist(LedgerCase):
    def test_it_asks_for_what_is_outstanding_not_for_what_was_earned(self):
        self.completed_booking(fare=10000)
        client = self.admin()
        self.assertIn("500.00 of commission is owed by drivers",
                      client.get("/admin/checklist").get_data(as_text=True))

        self.settle(client, self.driver, amount="500")
        page = client.get("/admin/checklist").get_data(as_text=True)
        self.assertNotIn("of commission is owed by drivers", page)


if __name__ == "__main__":
    unittest.main()
