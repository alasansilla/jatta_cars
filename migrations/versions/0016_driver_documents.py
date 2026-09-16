"""Hold the licence and identification a driver sends before they are approved.

The row records what is on file and where the bytes are; the bytes themselves
live in private storage, never under a public URL. One current document per
kind, so a replacement takes the place of what it supersedes.
"""
import importlib

from sqlalchemy import inspect

VERSION = "0016"
NAME = "driver identity documents"

TABLE = "driver_documents"


def upgrade(connection, metadata):
    if TABLE not in inspect(connection).get_table_names():
        metadata.tables[TABLE].create(bind=connection)
    lockdown = importlib.import_module(
        "migrations.versions.0010_public_api_lockdown_and_indexes")
    lockdown.lock_tables(connection, [TABLE])
