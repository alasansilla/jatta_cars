"""Record the licence and identification check made before a driver is approved.

The public pages say no driver is listed until their licence and identification
have been seen. This is where that is written down: when it was done, by which
staff account, and what was seen. Drivers approved before this existed keep
their approval, and show as unchecked until someone confirms it.
"""
from sqlalchemy import inspect, text

VERSION = "0015"
NAME = "driver licence and identification checks"

COLUMNS = {
    "checks_confirmed_at": "TIMESTAMP",
    "checks_confirmed_by_id": "INTEGER",
    "checks_note": "TEXT",
}


def upgrade(connection, metadata):
    existing = {column["name"] for column in inspect(connection).get_columns("operators")}
    for name, kind in COLUMNS.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE operators ADD COLUMN {name} {kind}"))
