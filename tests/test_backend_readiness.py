"""Configuration, storage, migrations and the booking race.

These cover the parts that only behave differently once the app is on Postgres
and object storage, which is exactly where a laptop stops telling you the truth.
"""
import importlib
import io
import logging
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from sqlalchemy.exc import IntegrityError

from app import create_app
from app.models import AdminUser, Booking, MediaAsset, Setting, Vehicle, db
from app.storage import (
    LocalStorage, StorageError, SupabaseStorage, build_storage, media_url,
)
from config import Config, _postgres_driver

PG = f"postgresql+{_postgres_driver()}"

POSTGRES_TEST_URL = os.environ.get("JATTA_TEST_POSTGRES_URL")


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    SECRET_KEY = "test-only"
    ENV_NAME = "testing"


# --- configuration ----------------------------------------------------------

class DatabaseUrlTests(unittest.TestCase):
    """Whatever a host hands us has to become a URL SQLAlchemy accepts."""

    def _config_for(self, url):
        os.environ["JATTA_DATABASE_URL"] = url
        try:
            import config as config_module
            importlib.reload(config_module)
            return config_module.Config
        finally:
            os.environ.pop("JATTA_DATABASE_URL", None)
            import config as config_module
            importlib.reload(config_module)

    def test_legacy_postgres_scheme_is_rewritten(self):
        """SQLAlchemy dropped support for postgres:// entirely."""
        cfg = self._config_for("postgres://u:p@host:5432/postgres")
        self.assertTrue(cfg.SQLALCHEMY_DATABASE_URI.startswith(PG + "://"))

    def test_a_driver_is_always_named(self):
        """Bare postgresql:// leaves SQLAlchemy to guess; name the driver."""
        cfg = self._config_for("postgresql://u:p@host:5432/postgres")
        self.assertIn("+psycopg", cfg.SQLALCHEMY_DATABASE_URI)

    def test_psycopg3_is_told_not_to_prepare_on_the_transaction_pooler(self):
        """Supavisor cannot carry a prepared statement between checkouts."""
        if _postgres_driver() != "psycopg":
            self.skipTest("psycopg 3 not installed")
        cfg = self._config_for(
            "postgresql://u:p@aws-1-eu-west-1.pooler.supabase.com:6543/postgres")
        self.assertIsNone(
            cfg.SQLALCHEMY_ENGINE_OPTIONS["connect_args"]["prepare_threshold"])

    def test_a_normal_connection_still_prepares(self):
        if _postgres_driver() != "psycopg":
            self.skipTest("psycopg 3 not installed")
        cfg = self._config_for("postgresql://u:p@host:5432/postgres")
        self.assertNotIn("prepare_threshold", cfg.SQLALCHEMY_ENGINE_OPTIONS["connect_args"])

    def test_transaction_pooler_uses_no_client_side_pool(self):
        """Supavisor owns the connections on 6543; pooling on top pins them."""
        from sqlalchemy.pool import NullPool

        cfg = self._config_for("postgresql://u:p@aws-1-eu-west-1.pooler.supabase.com:6543/postgres")
        self.assertIs(cfg.SQLALCHEMY_ENGINE_OPTIONS.get("poolclass"), NullPool)
        self.assertNotIn("pool_size", cfg.SQLALCHEMY_ENGINE_OPTIONS)

    def test_session_pooler_keeps_a_normal_pool(self):
        cfg = self._config_for("postgresql://u:p@aws-1-eu-west-1.pooler.supabase.com:5432/postgres")
        self.assertNotIn("poolclass", cfg.SQLALCHEMY_ENGINE_OPTIONS)
        self.assertIn("pool_size", cfg.SQLALCHEMY_ENGINE_OPTIONS)

    def test_postgres_connections_recover_from_a_dropped_socket(self):
        cfg = self._config_for("postgresql://u:p@host:5432/postgres")
        self.assertTrue(cfg.SQLALCHEMY_ENGINE_OPTIONS.get("pool_pre_ping"))

    def test_sqlite_gets_no_postgres_options(self):
        import config as config_module
        importlib.reload(config_module)
        self.assertEqual(config_module.Config.SQLALCHEMY_ENGINE_OPTIONS,
                         {"hide_parameters": True})


class ProductionGuardTests(unittest.TestCase):
    """Production must fail closed, loudly, on anything half-configured."""

    def setUp(self):
        # These tests deliberately trip the production warnings; do not print them.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _production(self, **overrides):
        attrs = {
            "ENV_NAME": "production",
            "SECRET_KEY": "a" * 48,
            "SQLALCHEMY_DATABASE_URI": PG + "://u:p@h:6543/postgres",
            "SQLALCHEMY_ENGINE_OPTIONS": {},
            "STORAGE_BACKEND": "supabase",
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
        }
        attrs.update(overrides)
        return type("Cfg", (TestConfig,), attrs)

    def test_production_refuses_a_blank_secret_key(self):
        """An unset variable arrives as an empty string, not as a missing one."""
        for blank in ("", "   ", "\t\n "):
            with self.assertRaises(RuntimeError, msg=repr(blank)) as caught:
                create_app(self._production(SECRET_KEY=blank))
            self.assertIn("empty", str(caught.exception))

    def test_production_refuses_a_short_secret_key(self):
        with self.assertRaises(RuntimeError) as caught:
            create_app(self._production(SECRET_KEY="hunter2hunter2"))
        self.assertIn("at least", str(caught.exception))

    def test_production_refuses_the_development_secret_key(self):
        """Signing sessions with a public constant lets anyone forge a login."""
        import config as config_module

        class Unsafe(TestConfig):
            ENV_NAME = "production"
            SECRET_KEY = config_module.DEV_SECRET

        with self.assertRaises(RuntimeError) as caught:
            create_app(Unsafe)
        self.assertIn("JATTA_SECRET_KEY", str(caught.exception))

    def test_production_refuses_sqlite(self):
        """A serverless host discards the file, so every booking would vanish."""
        class OnSqlite(TestConfig):
            ENV_NAME = "production"
            SECRET_KEY = "a" * 48

        with self.assertRaises(RuntimeError) as caught:
            create_app(OnSqlite)
        self.assertIn("SQLite", str(caught.exception))

    def test_production_refuses_local_file_storage(self):
        class NoStorage(TestConfig):
            ENV_NAME = "production"
            SECRET_KEY = "a" * 48
            SQLALCHEMY_DATABASE_URI = PG + "://u:p@h:6543/postgres"

        with self.assertRaises(RuntimeError) as caught:
            create_app(NoStorage)
        self.assertIn("JATTA_STORAGE", str(caught.exception))

    def test_production_refuses_supabase_without_a_key(self):
        class HalfConfigured(TestConfig):
            ENV_NAME = "production"
            SECRET_KEY = "a" * 48
            SQLALCHEMY_DATABASE_URI = PG + "://u:p@h:6543/postgres"
            STORAGE_BACKEND = "supabase"
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = ""

        with self.assertRaises(RuntimeError) as caught:
            create_app(HalfConfigured)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", str(caught.exception))

    def test_a_fully_configured_production_app_starts(self):
        class Good(TestConfig):
            ENV_NAME = "production"
            SMS_BACKEND = None
            SECRET_KEY = "a" * 48
            SQLALCHEMY_DATABASE_URI = PG + "://u:p@h:6543/postgres"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            STORAGE_BACKEND = "supabase"
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = "service-key"

        self.assertIsNotNone(create_app(Good))

    def test_development_is_left_alone(self):
        self.assertEqual(create_app(TestConfig).config["ENV_NAME"], "testing")


# --- storage ----------------------------------------------------------------

class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = create_app(TestConfig)
        self.storage = LocalStorage(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_round_trip(self):
        self.storage.save("uploads/car-abcd.png", b"bytes")
        self.assertTrue(os.path.exists(os.path.join(self.root, "car-abcd.png")))
        self.storage.delete("uploads/car-abcd.png")
        self.assertFalse(os.path.exists(os.path.join(self.root, "car-abcd.png")))

    def test_deleting_something_already_gone_is_not_an_error(self):
        self.storage.delete("uploads/never-existed.png")

    def test_a_key_cannot_escape_the_upload_directory(self):
        for key in ("uploads/../../etc/passwd", "uploads/nested/file.png", "uploads/.."):
            with self.assertRaises(StorageError, msg=key):
                self.storage.save(key, b"x")


class SupabaseStorageTests(unittest.TestCase):
    def setUp(self):
        self.storage = SupabaseStorage(
            "https://khisfholynmqvmrqlrij.supabase.co/", "service-key", "media", 999)

    def test_public_url_matches_supabases_format(self):
        self.assertEqual(
            self.storage.url("uploads/car-abcd.png"),
            "https://khisfholynmqvmrqlrij.supabase.co"
            "/storage/v1/object/public/media/uploads/car-abcd.png",
        )

    def test_upload_posts_to_the_object_endpoint_with_the_service_key(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return mock.MagicMock(
                __enter__=lambda s: mock.Mock(status=200, read=lambda: b"{}"),
                __exit__=lambda *a: False)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.storage.save("uploads/car-abcd.png", b"imagebytes", "image/png")

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/storage/v1/object/media/uploads/car-abcd.png"))
        self.assertEqual(captured["body"], b"imagebytes")
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer service-key")
        self.assertEqual(headers["content-type"], "image/png")
        self.assertIn("max-age=999", headers["cache-control"])

    def test_a_storage_failure_is_reported_not_swallowed(self):
        import urllib.error

        def fail(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {},
                io.BytesIO(b'{"message":"new row violates row-level security policy"}'))

        with mock.patch("urllib.request.urlopen", fail):
            with self.assertRaises(StorageError) as caught:
                self.storage.save("uploads/x.png", b"x", "image/png")
        self.assertIn("row-level security", str(caught.exception))

    def test_deleting_a_missing_object_is_tolerated(self):
        import urllib.error

        def missing(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"Not found"}'))

        with mock.patch("urllib.request.urlopen", missing):
            self.storage.delete("uploads/gone.png")  # must not raise

    def test_it_refuses_to_start_without_credentials(self):
        with self.assertRaises(StorageError):
            SupabaseStorage("https://x.supabase.co", "", "media")


class StorageSelectionTests(unittest.TestCase):
    def test_supabase_is_used_when_configured(self):
        class WithSupabase(TestConfig):
            STORAGE_BACKEND = None
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = "key"

        self.assertEqual(build_storage(create_app(WithSupabase)).name, "supabase")

    def test_local_disk_is_the_fallback(self):
        class NoSupabase(TestConfig):
            STORAGE_BACKEND = None
            SUPABASE_URL = ""

        self.assertEqual(build_storage(create_app(NoSupabase)).name, "local")

    def test_an_explicit_choice_wins(self):
        class Forced(TestConfig):
            STORAGE_BACKEND = "local"
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = "key"

        self.assertEqual(build_storage(create_app(Forced)).name, "local")


class MediaUrlTests(unittest.TestCase):
    """One helper has to serve both bundled artwork and uploaded photos."""

    def setUp(self):
        class WithSupabase(TestConfig):
            STORAGE_BACKEND = "supabase"
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = "key"
            SUPABASE_STORAGE_BUCKET = "media"

        self.app = create_app(WithSupabase)

    def test_an_upload_resolves_to_the_storage_backend(self):
        with self.app.test_request_context():
            self.assertEqual(
                media_url("uploads/car-abcd.png"),
                "https://x.supabase.co/storage/v1/object/public/media/uploads/car-abcd.png")

    def test_bundled_artwork_still_comes_from_static(self):
        with self.app.test_request_context():
            self.assertEqual(media_url("img/car-economy.svg"), "/static/img/car-economy.svg")

    def test_an_empty_field_is_not_a_broken_image(self):
        with self.app.test_request_context():
            self.assertEqual(media_url(""), "")
            self.assertEqual(media_url(None), "")


# --- migrations -------------------------------------------------------------

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_running_them_creates_the_schema_and_records_the_versions(self):
        from migrations.runner import status, upgrade

        ran = upgrade(db.engine, db.metadata, log=lambda *a: None)
        self.assertIn("0001", ran)
        self.assertTrue(all(done for _, _, done in status(db.engine)))

    def test_running_them_twice_changes_nothing(self):
        from migrations.runner import upgrade

        upgrade(db.engine, db.metadata, log=lambda *a: None)
        self.assertEqual(upgrade(db.engine, db.metadata, log=lambda *a: None), [])

    def test_the_postgres_only_step_is_a_no_op_on_sqlite(self):
        """It must not fail the run on a development database."""
        from migrations.runner import upgrade

        upgrade(db.engine, db.metadata, log=lambda *a: None)
        db.session.add(Vehicle(make="A", model="B", year=2020, daily_rate=1))
        db.session.commit()
        self.assertEqual(Vehicle.query.count(), 1)

    def test_versions_are_unique_and_ordered(self):
        from migrations.runner import discover

        versions = [step.VERSION for step in discover()]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(versions), len(set(versions)))


# --- booking overlap and the race ------------------------------------------

class BookingOverlapTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.car = Vehicle(make="Sample", model="Car", year=2022,
                           daily_rate=10000, is_active=True)
        db.session.add(self.car)
        db.session.commit()
        self.client = self.app.test_client()
        from app.settings import current_settings
        self.location = current_settings()["locations"][0]
        self.start = date.today() + timedelta(days=10)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _book(self, start, end, email="a@b.com"):
        return self.client.post(f"/fleet/{self.car.id}/book", data={
            "start": start.isoformat(), "end": end.isoformat(),
            "customer_name": "Test Customer", "email": email,
            "pickup_location": self.location, "dropoff_location": self.location,
        }, follow_redirects=True)

    def test_an_overlapping_request_is_refused(self):
        self._book(self.start, self.start + timedelta(days=3))
        response = self._book(self.start + timedelta(days=1), self.start + timedelta(days=4))
        self.assertIn("already booked", response.get_data(as_text=True))
        self.assertEqual(Booking.query.count(), 1)

    def test_a_booking_starting_the_day_another_ends_is_allowed(self):
        """Return day is exclusive: one customer hands back as another collects."""
        self._book(self.start, self.start + timedelta(days=3))
        self._book(self.start + timedelta(days=3), self.start + timedelta(days=5))
        self.assertEqual(Booking.query.count(), 2)

    def test_a_booking_fully_inside_another_is_refused(self):
        self._book(self.start, self.start + timedelta(days=7))
        self._book(self.start + timedelta(days=2), self.start + timedelta(days=4))
        self.assertEqual(Booking.query.count(), 1)

    def test_a_booking_spanning_another_is_refused(self):
        self._book(self.start + timedelta(days=2), self.start + timedelta(days=4))
        self._book(self.start, self.start + timedelta(days=7))
        self.assertEqual(Booking.query.count(), 1)

    def test_a_cancelled_booking_frees_the_car(self):
        self._book(self.start, self.start + timedelta(days=3))
        booking = Booking.query.first()
        booking.status = "cancelled"
        db.session.commit()
        self._book(self.start, self.start + timedelta(days=3), email="c@d.com")
        self.assertEqual(Booking.query.filter_by(status="pending").count(), 1)

    def test_a_race_the_availability_check_missed_is_handled_not_crashed(self):
        """Two requests can pass the read-then-write check at the same moment.

        Postgres refuses the second write with the exclusion constraint from
        migration 0002. Whatever the database says no with, the customer must
        get a sentence rather than a 500, and no booking may be stored.
        """
        real_commit = db.session.commit
        calls = {"n": 0}

        def commit_once_then_conflict():
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT", {}, Exception("bookings_no_overlap"))
            return real_commit()

        with mock.patch.object(db.session, "commit", commit_once_then_conflict):
            response = self._book(self.start, self.start + timedelta(days=3))

        body = response.get_data(as_text=True)
        self.assertIn("a moment before you", body)
        self.assertNotIn("Internal Server Error", body)
        self.assertEqual(Booking.query.count(), 0)

    @unittest.skipUnless(POSTGRES_TEST_URL,
                         "set JATTA_TEST_POSTGRES_URL to exercise the real constraint")
    def test_postgres_refuses_an_overlap_at_the_database_level(self):
        """The constraint, not the application, is the last word.

        Skipped unless a Postgres URL is provided, because SQLite has no
        exclusion constraints and cannot enforce this at all.
        """
        from sqlalchemy import create_engine

        from migrations.runner import upgrade as run_migrations

        engine = create_engine(POSTGRES_TEST_URL)
        run_migrations(engine, db.metadata, log=lambda *a: None)

        end = self.start + timedelta(days=3)
        insert = (
            "INSERT INTO bookings (reference, vehicle_id, customer_name, email, "
            "pickup_location, dropoff_location, start_date, end_date, total_price, "
            "status, created_at) VALUES (:ref, :vid, 'X', 'x@y.z', 'a', 'a', "
            ":start, :end, 1, 'pending', now())")
        from sqlalchemy import text
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO vehicles (make, model, year, category, transmission, fuel, "
                "seats, doors, luggage, daily_rate, deposit, is_active, created_at) "
                "VALUES ('T','C',2020,'Economy','Manual','Petrol',5,5,2,1,0,true,now()) "
            ))
            vid = connection.execute(text("SELECT id FROM vehicles ORDER BY id DESC LIMIT 1")).scalar()
            connection.execute(text(insert), {"ref": "JC-RACE01", "vid": vid,
                                              "start": self.start, "end": end})
        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(insert), {
                    "ref": "JC-RACE02", "vid": vid,
                    "start": self.start + timedelta(days=1), "end": end})
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM bookings WHERE reference LIKE 'JC-RACE%'"))
            connection.execute(text("DELETE FROM vehicles WHERE id = :v"), {"v": vid})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()


# --- customer data on a shared host ----------------------------------------

class CustomerPrivacyTests(unittest.TestCase):
    """Booking details are the one place personal data lives. It stays there."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from app.settings import save_settings

        save_settings({"site_live": True})
        self.car = Vehicle(make="Sample", model="Car", year=2022,
                           daily_rate=10000, is_active=True)
        admin = AdminUser(username="admin")
        admin.set_password("test-password")
        db.session.add_all([self.car, admin])
        db.session.commit()

        start = date.today() + timedelta(days=6)
        self.booking = Booking(
            reference="JC-PRIV01", vehicle=self.car, customer_name="Awa Ceesay",
            email="awa@example.com", phone="+220 700 4321",
            pickup_location="Kololi", dropoff_location="Kololi",
            start_date=start, end_date=start + timedelta(days=3), total_price=30000)
        db.session.add(self.booking)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _assert_no_pii(self, body, where):
        for secret in ("Awa Ceesay", "awa@example.com", "+220 700 4321"):
            self.assertNotIn(secret, body, f"{secret!r} leaked on {where}")

    def test_no_public_page_mentions_a_customer(self):
        for path in ("/", "/fleet", f"/fleet/{self.car.id}", "/about", "/contact",
                     "/booking"):
            self._assert_no_pii(self.client.get(path).get_data(as_text=True), path)

    def test_a_stranger_with_the_reference_still_sees_nothing(self):
        response = self.client.get("/booking/JC-PRIV01", follow_redirects=True)
        self._assert_no_pii(response.get_data(as_text=True), "a guessed reference")

    def test_the_owner_sees_their_own_booking_and_it_is_not_cached(self):
        self.client.post("/booking", data={"reference": "JC-PRIV01",
                                           "email": "awa@example.com"})
        response = self.client.get("/booking/JC-PRIV01")
        self.assertIn("Awa Ceesay", response.get_data(as_text=True))
        # A shared machine or a proxy must not keep this page.
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_signing_out_of_a_booking_is_not_possible_for_someone_else(self):
        """One session unlocking a booking must not unlock it for everyone."""
        self.client.post("/booking", data={"reference": "JC-PRIV01",
                                           "email": "awa@example.com"})
        other = self.app.test_client()
        self._assert_no_pii(
            other.get("/booking/JC-PRIV01", follow_redirects=True).get_data(as_text=True),
            "a second visitor")

    def test_staff_endpoints_give_a_stranger_nothing(self):
        for path in ("/admin/", "/admin/bookings", "/admin/enquiries", "/admin/media"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self._assert_no_pii(response.get_data(as_text=True), path)

    def test_the_service_role_key_never_reaches_a_page(self):
        """It bypasses row-level security, so it is server-only, always."""
        class WithSupabase(TestConfig):
            STORAGE_BACKEND = "supabase"
            SUPABASE_URL = "https://x.supabase.co"
            SUPABASE_SERVICE_ROLE_KEY = "super-secret-service-key"

        app = create_app(WithSupabase)
        with app.app_context():
            db.create_all()
            db.session.add(Vehicle(make="A", model="B", year=2020,
                                   daily_rate=1, is_active=True))
            db.session.commit()
            client = app.test_client()
            for path in ("/", "/fleet"):
                self.assertNotIn("super-secret-service-key",
                                 client.get(path).get_data(as_text=True), path)
            db.drop_all()


# --- editor operations against real storage ---------------------------------

class EditorStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

        class WithLocalStorage(TestConfig):
            STORAGE_BACKEND = "local"
            UPLOAD_FOLDER = self.root

        self.app = create_app(WithLocalStorage)
        self.app.config["UPLOAD_FOLDER"] = self.root
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = AdminUser(username="admin")
        admin.set_password("test-password")
        db.session.add(admin)
        db.session.commit()
        self.client = self.app.test_client()
        self.client.post("/admin/login",
                         data={"username": "admin", "password": "test-password"})
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.headers = {"X-CSRF-Token": session["_csrf_token"]}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _upload(self, name="car.png", data=b"\x89PNG\r\n\x1a\nfake"):
        return self.client.post("/admin/api/upload", headers=self.headers,
                                data={"file": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")

    def test_an_upload_is_stored_and_addressed_by_its_key(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["path"].startswith("uploads/"))
        self.assertTrue(body["url"].endswith(body["path"]))
        asset = MediaAsset.query.one()
        self.assertEqual(asset.path, body["path"])
        self.assertTrue(os.path.exists(os.path.join(self.root, asset.filename)))

    def test_two_uploads_of_one_filename_do_not_collide(self):
        first = self._upload().get_json()["path"]
        second = self._upload().get_json()["path"]
        self.assertNotEqual(first, second)
        self.assertEqual(MediaAsset.query.count(), 2)

    def test_a_storage_failure_leaves_no_orphan_record(self):
        """If the bytes did not land, the library must not claim they did."""
        logging.disable(logging.CRITICAL)  # the failure is logged on purpose
        try:
            with mock.patch("app.storage.LocalStorage.save",
                            side_effect=StorageError("bucket is full")):
                response = self._upload()
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(response.status_code, 400)
        self.assertIn("bucket is full", response.get_json()["error"])
        self.assertEqual(MediaAsset.query.count(), 0)

    def test_an_empty_file_is_rejected(self):
        self.assertEqual(self._upload(data=b"").status_code, 400)
        self.assertEqual(MediaAsset.query.count(), 0)

    def test_a_picture_in_use_is_not_deleted_from_storage(self):
        path = self._upload().get_json()["path"]
        asset = MediaAsset.query.one()
        db.session.add(Vehicle(make="A", model="B", year=2020,
                               daily_rate=1, image=path))
        db.session.commit()

        with mock.patch("app.storage.LocalStorage.delete") as delete:
            response = self.client.post(f"/admin/media/{asset.id}/delete",
                                        data={"csrf_token": self.headers["X-CSRF-Token"]},
                                        follow_redirects=True)
        delete.assert_not_called()
        self.assertIn("Still in use", response.get_data(as_text=True))
        self.assertEqual(MediaAsset.query.count(), 1)

    def test_deleting_an_unused_picture_removes_the_file_too(self):
        self._upload()
        asset = MediaAsset.query.one()
        filename = asset.filename
        self.client.post(f"/admin/media/{asset.id}/delete",
                         data={"csrf_token": self.headers["X-CSRF-Token"]},
                         follow_redirects=True)
        self.assertEqual(MediaAsset.query.count(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, filename)))

    def test_an_uploaded_picture_can_be_attached_to_a_car_inline(self):
        path = self._upload().get_json()["path"]
        vehicle_id = self.client.post("/admin/api/vehicle/new",
                                      headers=self.headers).get_json()["id"]
        response = self.client.post("/admin/api/save", headers=self.headers, json={
            "records": {f"vehicle:{vehicle_id}:image": path}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(Vehicle, vehicle_id).image, path)

    def test_uploading_requires_a_signed_in_staff_member(self):
        anonymous = self.app.test_client()
        response = anonymous.post("/admin/api/upload",
                                  data={"file": (io.BytesIO(b"x"), "x.png")},
                                  content_type="multipart/form-data")
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(MediaAsset.query.count(), 0)


# --- moving an existing site to Supabase ------------------------------------

class TransferTests(unittest.TestCase):
    """The transfer must survive a non-empty destination and a duplicate fleet."""

    def setUp(self):
        tools = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools")
        if tools not in os.sys.path:
            os.sys.path.insert(0, tools)

        self.source_file = tempfile.mktemp(suffix=".db")
        self.source_url = f"sqlite:///{self.source_file}"
        self.map_file = tempfile.mktemp(suffix=".json")

        class SourceConfig(TestConfig):
            SQLALCHEMY_DATABASE_URI = self.source_url

        source_app = create_app(SourceConfig)
        with source_app.app_context():
            db.create_all()
            # Two identical cars: a real fleet has these, and matching on
            # make/model/year would fold them into one.
            first = Vehicle(make="Toyota", model="Corolla", year=2021,
                            daily_rate=10000, is_active=True)
            second = Vehicle(make="Toyota", model="Corolla", year=2021,
                             daily_rate=10000, is_active=True)
            other = Vehicle(make="Suzuki", model="Vitara", year=2020,
                            daily_rate=12000, is_active=True)
            admin = AdminUser(username="local-admin")
            admin.set_password("local-password")
            db.session.add_all([first, second, other, admin])
            db.session.commit()
            self.source_ids = (first.id, second.id, other.id)

            start = date.today() + timedelta(days=5)
            # One booking against each identical car, so a mix-up is visible.
            db.session.add_all([
                Booking(reference="JC-CAR1", vehicle_id=first.id, customer_name="Awa",
                        email="awa@example.com", pickup_location="Kololi",
                        dropoff_location="Kololi", start_date=start,
                        end_date=start + timedelta(days=2), total_price=20000),
                Booking(reference="JC-CAR2", vehicle_id=second.id, customer_name="Binta",
                        email="binta@example.com", pickup_location="Kololi",
                        dropoff_location="Kololi", start_date=start,
                        end_date=start + timedelta(days=2), total_price=20000),
                Booking(reference="JC-CAR3", vehicle_id=other.id, customer_name="Modou",
                        email="modou@example.com", pickup_location="Kololi",
                        dropoff_location="Kololi", start_date=start,
                        end_date=start + timedelta(days=2), total_price=24000),
            ])
            db.session.add(MediaAsset(filename="car-abcd.png",
                                      original_name="car.png", size_bytes=5))
            db.session.add(Setting(key="company_phone", value="+220 700 0000"))
            db.session.commit()

        self.dest_root = tempfile.mkdtemp()

        class DestConfig(TestConfig):
            STORAGE_BACKEND = "local"

        self.dest_app = create_app(DestConfig)
        self.dest_app.config["UPLOAD_FOLDER"] = self.dest_root
        self.ctx = self.dest_app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.dest_root, ignore_errors=True)
        for path in (self.source_file, self.map_file):
            if os.path.exists(path):
                os.remove(path)

    def _fill_destination(self):
        """Put unrelated rows in first, so destination ids do not line up."""
        for n in range(4):
            db.session.add(Vehicle(make="Pre-existing", model=f"Car{n}", year=2019,
                                   daily_rate=1, is_active=True))
        db.session.commit()

    def _run(self, commit=True):
        import transfer_to_postgres as transfer
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        mapping = transfer.load_map(self.map_file)
        engine = create_engine(self.source_url)
        result = {}
        with Session(engine) as source:
            result["settings"] = transfer.transfer_simple(Setting, "key", source, commit)
            result["vehicles"] = transfer.transfer_vehicles(source, mapping, commit)
            result["media"] = transfer.transfer_simple(MediaAsset, "filename", source, commit)
            result["bookings"] = transfer.transfer_bookings(source, mapping, commit)
        engine.dispose()
        if commit:
            transfer.save_map(mapping, self.map_file)
        result["map"] = mapping
        return result

    def test_bookings_follow_their_own_car_into_a_nonempty_destination(self):
        """The bug this replaces: vehicle_id was copied verbatim.

        With rows already at the destination the ids do not line up, so every
        booking silently attached to somebody else's car.
        """
        self._fill_destination()
        self._run()

        by_reference = {b.reference: b for b in Booking.query.all()}
        self.assertEqual(len(by_reference), 3)

        source_first, source_second, source_other = self.source_ids
        mapping = self._run(commit=False)["map"]["vehicles"]

        self.assertEqual(by_reference["JC-CAR1"].vehicle_id, mapping[str(source_first)])
        self.assertEqual(by_reference["JC-CAR2"].vehicle_id, mapping[str(source_second)])
        self.assertEqual(by_reference["JC-CAR3"].vehicle_id, mapping[str(source_other)])

        # And none of them landed on a car that was already there.
        pre_existing = {v.id for v in Vehicle.query.filter_by(make="Pre-existing").all()}
        for booking in by_reference.values():
            self.assertNotIn(booking.vehicle_id, pre_existing)

        self.assertEqual(by_reference["JC-CAR3"].vehicle.model, "Vitara")

    def test_two_identical_cars_stay_two_cars(self):
        self._run()
        corollas = Vehicle.query.filter_by(make="Toyota", model="Corolla").all()
        self.assertEqual(len(corollas), 2)
        # And they are not the same row wearing two bookings.
        self.assertNotEqual(
            Booking.query.filter_by(reference="JC-CAR1").one().vehicle_id,
            Booking.query.filter_by(reference="JC-CAR2").one().vehicle_id,
        )

    def test_running_it_twice_copies_nothing_the_second_time(self):
        self._run()
        second = self._run()
        self.assertEqual(second["vehicles"], (0, 3))
        self.assertEqual(second["bookings"][0], 0)
        self.assertEqual(Vehicle.query.count(), 3)
        self.assertEqual(Booking.query.count(), 3)

    def test_a_dry_run_writes_nothing(self):
        result = self._run(commit=False)
        self.assertEqual(result["vehicles"][0], 3)
        self.assertEqual(Vehicle.query.count(), 0)
        self.assertFalse(os.path.exists(self.map_file))

    def test_a_booking_whose_car_did_not_transfer_is_reported_not_misattached(self):
        import transfer_to_postgres as transfer
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        self._fill_destination()
        mapping = {"vehicles": {}, "media": []}  # as if vehicles had not run
        engine = create_engine(self.source_url)
        with Session(engine) as source:
            copied, skipped, orphaned = transfer.transfer_bookings(source, mapping, True)
        engine.dispose()

        self.assertEqual(copied, 0)
        self.assertEqual(sorted(orphaned), ["JC-CAR1", "JC-CAR2", "JC-CAR3"])
        self.assertEqual(Booking.query.count(), 0)

    def test_staff_accounts_are_not_carried_over(self):
        self._run()
        self.assertEqual(AdminUser.query.count(), 0)

    def test_the_source_is_left_untouched(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        self._fill_destination()
        self._run()
        engine = create_engine(self.source_url)
        with Session(engine) as source:
            self.assertEqual(source.query(Vehicle).count(), 3)
            self.assertEqual(source.query(AdminUser).count(), 1)
            self.assertEqual(source.query(Booking).count(), 3)
        engine.dispose()

    def test_pictures_upload_once_and_are_skipped_on_a_rerun(self):
        import transfer_to_postgres as transfer

        self._run()
        source_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(source_dir, "car-abcd.png"), "wb") as handle:
                handle.write(b"imagebytes")
            mapping = transfer.load_map(self.map_file)

            sent, already, missing = transfer.transfer_files(mapping, True, source_dir)
            self.assertEqual((sent, already, missing), (["car-abcd.png"], [], []))
            self.assertTrue(os.path.exists(os.path.join(self.dest_root, "car-abcd.png")))

            sent, already, missing = transfer.transfer_files(mapping, True, source_dir)
            self.assertEqual(sent, [], "re-uploaded a picture it had already sent")
            self.assertEqual(already, ["car-abcd.png"])
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)

    def test_a_resumed_upload_overwrites_rather_than_colliding(self):
        """A half-written object from a failed run must not 409 the retry."""
        import transfer_to_postgres as transfer

        self._run()
        source_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(source_dir, "car-abcd.png"), "wb") as handle:
                handle.write(b"imagebytes")
            with mock.patch("app.storage.LocalStorage.save") as save:
                transfer.transfer_files({"vehicles": {}, "media": []}, True, source_dir)
            self.assertTrue(save.call_args.kwargs.get("overwrite"),
                            "transfer must upload with overwrite")
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)

    def test_pictures_with_no_local_file_are_reported(self):
        import transfer_to_postgres as transfer

        self._run()
        sent, already, missing = transfer.transfer_files(
            {"vehicles": {}, "media": []}, False, self.dest_root)
        self.assertEqual(missing, ["car-abcd.png"])


# --- bootstrap SQL and the models must describe the same database -----------

class BootstrapParityTests(unittest.TestCase):
    """The SQL Editor script and the migrations have to agree.

    The first version of bootstrap.sql was written from CreateTable alone,
    which does not emit indexes declared with `index=True`. That quietly cost
    `bookings.reference` its uniqueness: nothing in the database stopped two
    bookings sharing a reference, and a reference is what a customer uses to
    reach their own booking.
    """

    def setUp(self):
        tools = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools")
        if tools not in os.sys.path:
            os.sys.path.insert(0, tools)
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        import generate_bootstrap
        self.generator = generate_bootstrap
        self.sql = open(generate_bootstrap.TARGET, encoding="utf-8").read()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_the_committed_file_is_not_stale(self):
        """Change a model without regenerating and this fails."""
        self.assertEqual(
            self.sql, self.generator.render(),
            "supabase/bootstrap.sql is out of date — run "
            "python tools/generate_bootstrap.py")

    def test_every_table_is_created(self):
        for table in db.metadata.sorted_tables:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table.name} (", self.sql,
                          f"{table.name} is missing from bootstrap.sql")

    def test_every_index_the_models_declare_is_created(self):
        missing = []
        for table in db.metadata.sorted_tables:
            for index in table.indexes:
                if index.name not in self.sql:
                    missing.append(index.name)
        self.assertEqual(missing, [], f"indexes missing from bootstrap.sql: {missing}")

    def test_unique_indexes_are_created_unique(self):
        """A unique index emitted without UNIQUE enforces nothing."""
        for table in db.metadata.sorted_tables:
            for index in table.indexes:
                if not index.unique:
                    continue
                self.assertIn(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index.name} ", self.sql,
                    f"{index.name} must be created UNIQUE")

    def test_booking_reference_is_unique_in_the_database(self):
        """Named directly: this is the one that protects a customer's booking."""
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS ix_bookings_reference "
                      "ON bookings (reference)", self.sql)

    def test_it_carries_the_same_migrations_the_runner_knows_about(self):
        from migrations.runner import discover

        for step in discover():
            self.assertIn(f"('{step.VERSION}'", self.sql,
                          f"migration {step.VERSION} is not recorded by bootstrap.sql")

    def test_the_overlap_constraint_and_query_indexes_are_there(self):
        for fragment in ("bookings_no_overlap", "btree_gist",
                         "ix_bookings_vehicle_dates", "ix_bookings_status_start",
                         "ix_vehicles_active"):
            self.assertIn(fragment, self.sql)

    def test_every_application_table_is_closed_to_the_rest_api(self):
        """Supabase exposes public schema tables through PostgREST by default."""
        for table in db.metadata.sorted_tables:
            self.assertIn(f"'{table.name}'", self.sql,
                          f"{table.name} is not in the RLS list")
        self.assertIn("enable row level security", self.sql)
        self.assertIn("revoke all on table public.%I from anon, authenticated", self.sql)

    def test_the_media_bucket_is_read_only_to_the_public(self):
        self.assertIn('create policy "media_public_read"', self.sql)
        self.assertIn("for select", self.sql)
        # No write policy may exist: uploads go through the server.
        self.assertNotIn("for insert", self.sql)
        self.assertNotIn("for update", self.sql)
        self.assertNotIn("for delete", self.sql)

    def test_it_can_be_run_twice(self):
        """Every creating statement has to be guarded."""
        # Whole lines, not just the keyword: findall on a group returns the group.
        for line in self.sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            upper = stripped.upper()
            if upper.startswith("CREATE TABLE") or "CREATE INDEX" in upper \
                    or "CREATE UNIQUE INDEX" in upper:
                self.assertIn("IF NOT EXISTS", upper, stripped[:90])

    def test_the_generator_check_flag_agrees(self):
        self.assertEqual(self.generator.render(), self.sql)


# --- what the host checks ---------------------------------------------------

class HealthCheckTests(unittest.TestCase):
    """Render gates a deployment on this, so it has to mean something."""

    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_a_working_deployment_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")

    def test_an_unreachable_database_fails_the_check(self):
        """Otherwise a deployment with the wrong URL goes live and loses bookings."""
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(db.session, "execute",
                                   side_effect=Exception("could not connect")):
                response = self.client.get("/healthz")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["database"], "unreachable")

    def test_the_failure_body_does_not_leak_the_connection_string(self):
        """Exception text from a driver often contains host, user and password."""
        secret = "postgresql://postgres.ref:SUPERSECRET@host:5432/postgres"
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(db.session, "execute",
                                   side_effect=Exception(f"failed for {secret}")):
                response = self.client.get("/healthz")
        finally:
            logging.disable(logging.NOTSET)
        body = response.get_data(as_text=True)
        self.assertNotIn("SUPERSECRET", body)
        self.assertNotIn("postgresql://", body)

    def test_it_answers_even_while_the_site_is_a_draft(self):
        """The host must be able to tell a broken deploy from an unpublished one."""
        from app.settings import save_settings

        save_settings({"site_live": False})
        self.assertEqual(self.client.get("/").status_code, 200)  # holding page
        self.assertNotIn("nearly ready",
                         self.client.get("/healthz").get_data(as_text=True))
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_it_needs_no_sign_in(self):
        self.assertEqual(self.app.test_client().get("/healthz").status_code, 200)


class RenderBlueprintTests(unittest.TestCase):
    """render.yaml has to match what Render and this app actually require."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.text = open(os.path.join(root, "render.yaml"), encoding="utf-8").read()

    def test_it_binds_the_port_render_provides(self):
        """Render requires 0.0.0.0 and supplies PORT; a fixed port never serves."""
        self.assertIn("--bind 0.0.0.0:$PORT", self.text)

    def test_it_starts_the_wsgi_entrypoint(self):
        self.assertIn("gunicorn wsgi:app", self.text)

    def test_gunicorn_is_pinned_in_requirements(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        requirements = open(os.path.join(root, "requirements.txt"), encoding="utf-8").read()
        self.assertRegex(requirements, r"gunicorn==\d+\.\d+")

    def test_the_health_check_is_the_endpoint_that_exists(self):
        self.assertIn("healthCheckPath: /healthz", self.text)

    def test_a_long_lived_host_is_told_to_use_tls(self):
        self.assertIn("JATTA_DB_SSLMODE", self.text)
        self.assertIn("require", self.text)

    def test_every_secret_is_left_to_the_dashboard(self):
        for key in ("JATTA_SECRET_KEY", "JATTA_DATABASE_URL", "SUPABASE_URL",
                    "SUPABASE_SERVICE_ROLE_KEY"):
            block = self.text.split(f"key: {key}", 1)
            self.assertEqual(len(block), 2, f"{key} is not declared")
            self.assertIn("sync: false", block[1][:120],
                          f"{key} must be set in the dashboard, not in the file")

    def test_no_credential_is_committed_in_it(self):
        # <angle-bracket> placeholders in the comments are documentation, not
        # secrets, so they are excluded rather than matched.
        for pattern in (r"eyJ[A-Za-z0-9_-]{20,}",
                        r"postgres(ql)?://[^\s<>]*:[^\s@<>]{8,}@"):
            self.assertIsNone(re.search(pattern, self.text),
                              "render.yaml appears to contain a credential")

    def test_the_vercel_config_is_still_there(self):
        """Render is the documented path; Vercel stays a working option."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("vercel.json", "wsgi.py"):
            self.assertTrue(os.path.exists(os.path.join(root, name)), name)
