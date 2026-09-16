"""No driver is listed until their licence and identification have been seen.

The About page and every driver profile say so, which is only honest if the
site refuses to approve anybody without recording the check: when it was made,
by which staff account, and what was seen. Approving is still a person's
decision — this just refuses to let that decision be made silently.
"""
import html
import unittest
from datetime import datetime

from app import create_app
from app.models import AdminUser, Operator, Vehicle, db
from app.settings import save_settings
from config import Config


class ChecksCase(unittest.TestCase):
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
        save_settings({"site_live": True})
        self.staff = AdminUser(username="admin")
        self.staff.set_password("admin-password-long")
        self.applicant = Operator(name="Awa Jallow", slug="awa-jallow", status="pending",
                                  phone_e164="+2208770001", phone_verified_at=datetime.utcnow())
        db.session.add_all([self.staff, self.applicant])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def admin(self):
        client = self.app.test_client()
        client.post("/admin/login", data={"username": "admin", "password": "admin-password-long"})
        client.get("/admin/operators")
        return client

    def decide(self, client, status="approved", **fields):
        with client.session_transaction() as session:
            token = session["_csrf_token"]
        data = {"csrf_token": token, "status": status}
        data.update(fields)
        return client.post(f"/admin/operators/{self.applicant.id}/decision", data=data,
                           follow_redirects=True)

    def refreshed(self):
        return db.session.get(Operator, self.applicant.id)


class ApprovingNeedsTheCheck(ChecksCase):
    def test_approval_is_refused_until_the_check_is_recorded(self):
        page = self.decide(self.admin())
        self.assertIn("seen Awa Jallow's driving licence and identification",
                      html.unescape(page.get_data(as_text=True)))
        self.assertEqual(self.refreshed().status, "pending")
        self.assertFalse(self.refreshed().checks_done)

    def test_confirming_records_when_who_and_what(self):
        self.decide(self.admin(), checks_confirmed="1",
                    checks_note="Gambian licence B, expires 2029; passport seen")
        driver = self.refreshed()
        self.assertEqual(driver.status, "approved")
        self.assertTrue(driver.checks_done)
        self.assertEqual(driver.checks_confirmed_by_id, self.staff.id)
        self.assertEqual(driver.checks_note,
                         "Gambian licence B, expires 2029; passport seen")

    def test_the_note_is_optional_but_the_confirmation_is_not(self):
        self.decide(self.admin(), checks_confirmed="1")
        self.assertTrue(self.refreshed().checks_done)
        self.assertIsNone(self.refreshed().checks_note)

    def test_a_driver_already_checked_is_not_asked_again(self):
        """Suspending and reinstating someone does not re-open the question."""
        client = self.admin()
        self.decide(client, checks_confirmed="1")
        first_check = self.refreshed().checks_confirmed_at

        self.decide(client, status="suspended")
        self.assertEqual(self.refreshed().status, "suspended")
        self.decide(client, status="approved")           # no confirmation sent
        self.assertEqual(self.refreshed().status, "approved")
        self.assertEqual(self.refreshed().checks_confirmed_at, first_check)

    def test_rejecting_needs_no_check(self):
        self.decide(self.admin(), status="rejected")
        self.assertEqual(self.refreshed().status, "rejected")
        self.assertFalse(self.refreshed().checks_done)

    def test_the_staff_page_says_which_drivers_are_unchecked(self):
        client = self.admin()
        page = client.get("/admin/operators").get_data(as_text=True)
        self.assertIn("Licence and identification not checked yet", page)
        self.decide(client, checks_confirmed="1", checks_note="Licence seen")
        page = client.get("/admin/operators").get_data(as_text=True)
        self.assertIn("licence and ID checked", page)
        self.assertIn("Licence seen", page)


class WhatCustomersAreTold(ChecksCase):
    def approve(self):
        self.decide(self.admin(), checks_confirmed="1")
        db.session.add(Vehicle(make="Toyota", model="Yaris", year=2020, daily_rate=2500,
                               deposit=7500, operator_id=self.applicant.id, is_active=True,
                               service_mode="rental"))
        db.session.commit()

    def test_the_profile_shows_the_check_with_its_date(self):
        self.approve()
        page = self.app.test_client().get(f"/drivers/{self.applicant.id}").get_data(as_text=True)
        self.assertIn("Licence and ID checked", page)
        self.assertIn(self.refreshed().checks_confirmed_at.strftime("%b %Y"), page)

    def test_the_about_page_describes_the_check_that_is_enforced(self):
        body = self.app.test_client().get("/about").get_data(as_text=True)
        self.assertIn("we ask for their driving licence and identification", body)
        self.assertIn("recorded against the account", body)
        # No claim of anything the site does not do.
        for overclaim in ["criminal record", "police check", "guarantee", "vetted", "insured by us"]:
            self.assertNotIn(overclaim, body.lower(), overclaim)

    def test_an_older_driver_shows_no_badge_until_someone_checks(self):
        """Approved before this existed: still listed, but nothing is claimed."""
        legacy = Operator(name="Musa Touray", slug="musa-touray", status="approved")
        db.session.add(legacy)
        db.session.commit()
        page = self.app.test_client().get(f"/drivers/{legacy.id}").get_data(as_text=True)
        self.assertNotIn("Licence and ID checked", page)


if __name__ == "__main__":
    unittest.main()
