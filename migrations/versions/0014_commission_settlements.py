"""Record what drivers have actually paid of the commission they owe.

Commission earned was already recorded; what came back was not, so the amount
outstanding could only be worked out on paper. This adds the settlement ledger:
one row per payment, negative for a correction, never edited or deleted.

The table is closed to the Supabase API roles as it is created, the same as
every other table here: it says what each driver owes.
"""
import importlib

from sqlalchemy import inspect

VERSION = "0014"
NAME = "commission settlement ledger"

TABLE = "commission_settlements"


def upgrade(connection, metadata):
    if TABLE not in inspect(connection).get_table_names():
        metadata.tables[TABLE].create(bind=connection)
    lockdown = importlib.import_module(
        "migrations.versions.0010_public_api_lockdown_and_indexes")
    lockdown.lock_tables(connection, [TABLE])
