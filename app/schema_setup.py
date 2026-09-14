"""Set up an empty production database when the app starts, and nothing more.

Render's free plan has no shell and no pre-deploy step, so a brand-new Supabase
database would otherwise need someone to paste supabase/bootstrap.sql into the
SQL Editor by hand before a single page could load. Instead, on start:

* If the database has **no application tables at all**, the app runs that same
  generated file. It is one transaction (the file has its own BEGIN/COMMIT), so
  it either creates everything or nothing.
* If the database already has tables, it is **never touched**. Pending
  migrations are only reported in the log and on /healthz; an existing database
  is upgraded deliberately, after a backup, with `python -m migrations`.
* A Postgres advisory lock makes sure two web workers starting together cannot
  both run it.

Optionally, the first staff account is created from JATTA_ADMIN_USER and
JATTA_ADMIN_PASSWORD, but only while no staff account exists. Set them in the
host's secret settings, deploy once, then remove JATTA_ADMIN_PASSWORD. The
values are never logged.
"""
import os

from sqlalchemy import inspect, text

from .models import AdminUser, db

BOOTSTRAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "supabase", "bootstrap.sql")
LOCK_ID = 72419009
MINIMUM_ADMIN_PASSWORD = 12

# The media-bucket section of bootstrap.sql works on Supabase's own `storage`
# schema, which the database role an app connects as may not be allowed to
# change. It is run on its own afterwards, so a refusal there can never stop the
# application tables being created.
STORAGE_START = "-- 4. Media bucket"
STORAGE_END = "-- 5. Record these as applied migrations"


def split_script(script):
    """(application part, storage part) of bootstrap.sql."""
    start = script.rfind("-- -----", 0, script.index(STORAGE_START))
    end = script.rfind("-- -----", 0, script.index(STORAGE_END))
    return script[:start] + script[end:], script[start:end]


def _pending_versions(connection):
    from migrations.runner import discover

    rows = connection.execute(text("SELECT version FROM schema_migrations")).fetchall()
    done = {row[0] for row in rows}
    return [step.VERSION for step in discover() if step.VERSION not in done]


def schema_state(engine):
    """"ok", "empty", or "pending: 0008, 0009". Read-only."""
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if not tables & {"bookings", "schema_migrations", "operators"}:
            return "empty"
        if "schema_migrations" not in tables:
            return "unversioned"
        pending = _pending_versions(connection)
    return "ok" if not pending else "pending: " + ", ".join(pending)


def apply_bootstrap_if_empty(app, engine):
    """Run supabase/bootstrap.sql on an empty Postgres database. Returns what happened."""
    if engine.dialect.name != "postgresql":
        return "skipped"
    with open(BOOTSTRAP, encoding="utf-8") as handle:
        script, storage = split_script(handle.read())

    raw = engine.raw_connection()
    try:
        driver = raw.driver_connection
        driver.autocommit = True
        with driver.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
            try:
                cursor.execute(
                    "SELECT to_regclass('public.bookings') IS NULL "
                    "AND to_regclass('public.schema_migrations') IS NULL "
                    "AND to_regclass('public.operators') IS NULL")
                empty = cursor.fetchone()[0]
                if not empty:
                    return "existing"
                # No parameters, so the driver sends the whole script as one
                # simple query; its own BEGIN/COMMIT make it all-or-nothing.
                try:
                    cursor.execute(script)
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
                app.logger.warning("Database was empty: created the schema from "
                                   "supabase/bootstrap.sql")
                try:
                    cursor.execute("BEGIN;\n" + storage + "\nCOMMIT;")
                except Exception as error:  # noqa: BLE001 — pictures only, not the site
                    cursor.execute("ROLLBACK")
                    app.logger.warning(
                        "Could not set up the 'media' storage bucket from SQL (%s). Create a "
                        "public bucket named 'media' in the Supabase dashboard for uploads.",
                        type(error).__name__)
                return "created"
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    finally:
        try:
            driver.autocommit = False
        except Exception:  # noqa: BLE001 — the connection is being discarded anyway
            pass
        raw.close()


def create_first_admin(app):
    """The first staff account, from environment variables, only if there is none."""
    username = os.environ.get("JATTA_ADMIN_USER", "").strip()
    password = os.environ.get("JATTA_ADMIN_PASSWORD", "")
    if not username or not password:
        return "not requested"
    if AdminUser.query.count():
        if password:
            app.logger.warning("JATTA_ADMIN_PASSWORD is still set but a staff account "
                               "already exists. Remove it from the host's settings.")
        return "exists"
    if len(password) < MINIMUM_ADMIN_PASSWORD:
        app.logger.error("JATTA_ADMIN_PASSWORD is shorter than %s characters; no staff "
                         "account was created.", MINIMUM_ADMIN_PASSWORD)
        return "too short"
    admin = AdminUser(username=username[:60])
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    app.logger.warning("Created the first staff account from environment variables. "
                       "Remove JATTA_ADMIN_PASSWORD from the host's settings now.")
    return "created"


def prepare(app):
    """Called once per process at start-up, in production on Postgres only."""
    if not app.config.get("AUTO_BOOTSTRAP"):
        return
    with app.app_context():
        engine = db.engine
        if engine.dialect.name != "postgresql":
            return
        try:
            apply_bootstrap_if_empty(app, engine)
            state = schema_state(engine)
            if state != "ok":
                app.logger.error("Database schema is not current (%s). Back up, then run "
                                 "`python -m migrations` against it.", state)
                return
            create_first_admin(app)
        except Exception as error:  # noqa: BLE001 — start anyway; /healthz reports it
            db.session.rollback()
            app.logger.error("Database setup failed: %s", type(error).__name__)
