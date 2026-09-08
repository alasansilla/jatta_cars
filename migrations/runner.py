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


def upgrade(engine, metadata, log=print):
    """Run everything not yet applied. Returns the versions it ran."""
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
