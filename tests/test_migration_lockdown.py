"""Migration 0010 and the pre-worker migration command.

The Postgres half — row-level security on every table, API roles stripped of
grants, and two simultaneous upgrades applying the step once — was run against
a real Postgres shaped like the live database (tables unlocked by 0004, grants
to anon/authenticated, indexes missing). These tests cover what SQLite can show.
"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest

from sqlalchemy import create_engine, inspect, text

from app.models import db
from migrations import runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
lockdown = importlib.import_module("migrations.versions.0010_public_api_lockdown_and_indexes")


class LockdownStepTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.engine = create_engine(f"sqlite:///{self.directory}/drift.db")
        with self.engine.begin() as connection:
            db.metadata.create_all(connection)
            for name in ("ix_bookings_booking_type", "ix_bookings_operator_id",
                         "ix_vehicles_operator_id"):
                connection.execute(text(f"DROP INDEX {name}"))

    def tearDown(self):
        self.engine.dispose()

    def test_missing_model_indexes_are_created_and_rerun_is_harmless(self):
        with self.engine.begin() as connection:
            lockdown.upgrade(connection, db.metadata)
        with self.engine.begin() as connection:
            lockdown.upgrade(connection, db.metadata)
        inspector = inspect(self.engine)
        for table in db.metadata.sorted_tables:
            names = {index["name"] for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                self.assertIn(index.name, names)

    def test_rls_statements_are_postgres_only(self):
        with self.engine.begin() as connection:
            lockdown.lock_tables(connection, ["operators"])   # no error on SQLite

    def test_the_lock_is_a_no_op_on_sqlite(self):
        with runner._MigrationLock(self.engine) as held:
            self.assertIsNone(held.connection)

    def test_bootstrap_records_0010(self):
        with open(os.path.join(ROOT, "supabase", "bootstrap.sql"), encoding="utf-8") as handle:
            self.assertIn("'0010'", handle.read())


class MigrateCommandTests(unittest.TestCase):
    def run_command(self, *args, database):
        env = dict(os.environ, JATTA_DATABASE_URL=f"sqlite:///{database}",
                   JATTA_ENV="development")
        env.pop("JATTA_ADMIN_PASSWORD", None)
        return subprocess.run([sys.executable, "-m", "migrations", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_upgrade_then_check_reports_up_to_date(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "fresh.db")
            first = self.run_command("--check", database=database)
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            upgraded = self.run_command(database=database)
            self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
            self.assertIn("applying 0010", upgraded.stdout)
            again = self.run_command("--check", database=database)
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertNotIn(directory, upgraded.stdout + upgraded.stderr)


if __name__ == "__main__":
    unittest.main()


class AttributionTests(unittest.TestCase):
    def test_powered_by_geoapify_follow_link_when_its_data_is_used(self):
        from unittest.mock import patch
        from app import create_app
        from app.settings import save_settings
        from tests.test_marketplace import TestConfig

        class Mapped(TestConfig):
            GEOCODER_URL = "https://example.invalid/geocode?text={query}"
            ROUTER_URL = "https://example.invalid/route?{coords}"

        for config, expected in ((Mapped, True), (TestConfig, False)):
            app = create_app(config)
            with app.app_context():
                db.create_all()
                save_settings({"site_live": True})
                body = app.test_client().get("/ride").get_data(as_text=True)
                has_link = '<a href="https://www.geoapify.com/" target="_blank">Powered by Geoapify</a>' in body
                self.assertEqual(has_link, expected, config.__name__)
                self.assertNotIn('rel="nofollow"', body)
                db.session.remove()
                db.drop_all()
