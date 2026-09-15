"""Applies pending migration steps and records what has run."""
import importlib
import os
import pkgutil
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select

VERSIONS_PACKAGE = "migrations.versions"

_tracking_metadata = MetaData()
schema_migrations = Table(
    "schema_migrations", _tracking_metadata,
    Column("version", String(20), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)


def discover():
    """Every step module, in version order."""
    package = importlib.import_module(VERSIONS_PACKAGE)
    steps = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{VERSIONS_PACKAGE}.{module_info.name}")
        steps.append(module)
    return sorted(steps, key=lambda module: module.VERSION)


def applied_versions(connection):
    if not inspect(connection).has_table("schema_migrations"):
        return set()
    rows = connection.execute(select(schema_migrations.c.version)).fetchall()
    return {row[0] for row in rows}


def pending(connection):
    done = applied_versions(connection)
    return [step for step in discover() if step.VERSION not in done]


LOCK_ID = 72419010


class _MigrationLock:
    """A Postgres session-level advisory lock held for the whole upgrade.

    Two deploys starting at once (or a deploy and a manual run) would otherwise
    both see step N as pending and both try to apply it. The lock is held on its
    own connection, so each step can still commit in its own transaction.
    """

    def __init__(self, engine):
        self.engine = engine
        self.connection = None

    def __enter__(self):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy import text

            self.connection = self.engine.connect()
            self.connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": LOCK_ID})
            self.connection.commit()
        return self

    def __exit__(self, *exc):
        if self.connection is not None:
            from sqlalchemy import text

            try:
                self.connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                self.connection.commit()
            finally:
                self.connection.close()
        return False


def upgrade(engine, metadata, log=print):
    """Run everything not yet applied, one step per transaction. Returns the versions run."""
    with _MigrationLock(engine):
        return _upgrade(engine, metadata, log)


def _upgrade(engine, metadata, log):
    with engine.begin() as connection:
        _tracking_metadata.create_all(connection)

    ran = []
    for step in discover():
        with engine.begin() as connection:
            if step.VERSION in applied_versions(connection):
                continue
            log(f"  applying {step.VERSION}  {step.NAME}")
            step.upgrade(connection, metadata)
            connection.execute(schema_migrations.insert().values(
                version=step.VERSION,
                name=step.NAME,
                applied_at=datetime.now(timezone.utc),
            ))
            ran.append(step.VERSION)

    if not ran:
        log("  database already up to date")
    return ran


def status(engine):
    with engine.connect() as connection:
        done = applied_versions(connection)
    return [(step.VERSION, step.NAME, step.VERSION in done) for step in discover()]
