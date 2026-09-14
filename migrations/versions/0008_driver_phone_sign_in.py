"""Phone-number sign-in for drivers, and one request per estimate.

Four changes:

* `operators.phone_e164` and `operators.phone_verified_at`. The number a driver
  has proved they hold by typing back a texted code. A UNIQUE index enforces
  that one number opens exactly one account.
* `operators.email` becomes optional. A driver who joins with their phone number
  gives no email. Postgres drops the constraint in place; SQLite cannot, so the
  table is rebuilt from the model metadata and every row copied across — the
  same approach as 0004.
* `bookings.request_token` (UNIQUE) and `bookings.requested_at`, so a
  double-submitted ride request cannot create two trips, and a chosen driver's
  time to answer can be measured.
* `phone_codes` and `auth_events`: hashed one-time codes and the counters that
  rate-limit sending and guessing them.

Nothing existing is copied into `phone_e164`. The old `operators.phone` column
holds numbers that were typed in and never verified; treating them as proof of
ownership would let whoever typed a number claim it. Owners link a number by
signing in and verifying it. `tools/audit_driver_phones.py` reports clashes.
"""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

VERSION = "0008"
NAME = "driver phone sign-in and one request per estimate"

NEW_TABLES = ("phone_codes", "auth_events")


def _columns(connection, table):
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _indexes(connection, table):
    return {index["name"] for index in inspect(connection).get_indexes(table)}


def _rebuild_operators_for_sqlite(connection, metadata):
    """Copy operators through the current model definition.

    Compiled from the live metadata, so the new columns, the relaxed email and
    every index come from one place. Only the table name in the CREATE is
    rewritten; other tables keep referring to `operators`, which is the name the
    rebuilt table ends up with.
    """
    columns = _columns(connection, "operators")
    if "phone_e164" in columns and columns["email"]["nullable"]:
        return

    source = metadata.tables["operators"]
    ddl = str(CreateTable(source).compile(dialect=connection.dialect))
    ddl = ddl.replace("CREATE TABLE operators", "CREATE TABLE operators_rebuilt", 1)

    connection.execute(text("DROP TABLE IF EXISTS operators_rebuilt"))
    connection.execute(text(ddl))

    carried = sorted(set(columns) & set(source.c.keys()))
    names = ", ".join(f'"{name}"' for name in carried)
    connection.execute(text(
        f"INSERT INTO operators_rebuilt ({names}) SELECT {names} FROM operators"))

    # Foreign keys from other tables name `operators`; with SQLite's default of
    # not enforcing them during DDL, dropping and renaming keeps them pointing at
    # the rebuilt table.
    connection.execute(text("DROP TABLE operators"))
    connection.execute(text("ALTER TABLE operators_rebuilt RENAME TO operators"))
    for index in source.indexes:
        connection.execute(CreateIndex(index))


def _bookings_columns(connection, metadata):
    present = _columns(connection, "bookings")
    if "request_token" not in present:
        connection.execute(text("ALTER TABLE bookings ADD COLUMN request_token VARCHAR(64)"))
    if "requested_at" not in present:
        connection.execute(text("ALTER TABLE bookings ADD COLUMN requested_at TIMESTAMP"))
    if "ix_bookings_request_token" not in _indexes(connection, "bookings"):
        index = next(index for index in metadata.tables["bookings"].indexes
                     if index.name == "ix_bookings_request_token")
        connection.execute(CreateIndex(index))


def upgrade(connection, metadata):
    for name in NEW_TABLES:
        metadata.tables[name].create(connection, checkfirst=True)

    _bookings_columns(connection, metadata)

    if connection.dialect.name == "sqlite":
        _rebuild_operators_for_sqlite(connection, metadata)
        return

    if connection.dialect.name != "postgresql":
        return

    connection.execute(text(
        "ALTER TABLE operators ADD COLUMN IF NOT EXISTS phone_e164 VARCHAR(16)"))
    connection.execute(text(
        "ALTER TABLE operators ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMP"))
    connection.execute(text("ALTER TABLE operators ALTER COLUMN email DROP NOT NULL"))
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_operators_phone_e164 "
        "ON operators (phone_e164)"))

    for name in NEW_TABLES:
        connection.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON {name} FROM anon, authenticated"))
