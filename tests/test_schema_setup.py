"""Start-up schema setup. The Postgres behaviour itself (empty database gets the
schema, a database with tables is never touched, two workers cannot both run it,
a broken script rolls back) was exercised against a real local Postgres; these
tests pin down the parts that do not need one."""
import os
import unittest
from unittest.mock import patch

from app import create_app, schema_setup
from app.models import AdminUser, db
from tests.test_marketplace import TestConfig


class SchemaSetupTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_the_media_bucket_is_split_from_the_application_tables(self):
        with open(schema_setup.BOOTSTRAP, encoding="utf-8") as handle:
            application, storage = schema_setup.split_script(handle.read())
        before_checks = application.split("Check it worked")[0]
        self.assertNotIn("storage.", before_checks)
        self.assertIn("storage.buckets", storage)
        self.assertIn("create table if not exists public.schema_migrations", application)
        self.assertIn("commit;", application)
        self.assertNotIn("commit;", storage)

    def test_sqlite_is_never_bootstrapped(self):
        self.assertEqual(schema_setup.apply_bootstrap_if_empty(self.app, db.engine), "skipped")

    def test_web_start_only_reports_and_never_changes_the_schema(self):
        self.app.config["SCHEMA_REPORT_ON_START"] = True
        with patch.object(schema_setup, "apply_bootstrap_if_empty") as apply, \
                patch("migrations.runner.upgrade") as upgrade:
            schema_setup.report(self.app)
        apply.assert_not_called()
        upgrade.assert_not_called()
        self.assertFalse(TestConfig.SCHEMA_REPORT_ON_START)

    def test_first_admin_only_when_asked_long_enough_and_none_exists(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JATTA_ADMIN_USER", None)
            os.environ.pop("JATTA_ADMIN_PASSWORD", None)
            self.assertEqual(schema_setup.create_first_admin(self.app), "not requested")
        with patch.dict(os.environ, {"JATTA_ADMIN_USER": "owner", "JATTA_ADMIN_PASSWORD": "short"}):
            self.assertEqual(schema_setup.create_first_admin(self.app), "too short")
        secret = "long-enough-password-for-tests"
        with patch.dict(os.environ, {"JATTA_ADMIN_USER": "owner", "JATTA_ADMIN_PASSWORD": secret}), \
                self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.assertEqual(schema_setup.create_first_admin(self.app), "created")
            self.assertEqual(schema_setup.create_first_admin(self.app), "exists")
        self.assertEqual(AdminUser.query.count(), 1)
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertNotIn("owner", "\n".join(logs.output))

    def test_healthz_reports_schema_field(self):
        self.assertEqual(self.app.test_client().get("/healthz").get_json()["schema"], "not checked")


if __name__ == "__main__":
    unittest.main()
