"""Turn the single-operator rental site into a marketplace.

Adds operators, the fares they publish and the commission ledger, and widens
bookings to carry a journey as well as a hire.

Two things here are more than a CREATE TABLE:

* `bookings.vehicle_id` has to become nullable, because a ride request arrives
  before anyone has put a car against it. Postgres drops the constraint in
  place; SQLite cannot, so the table is rebuilt from the model metadata and the
  rows copied across.
* The overlap constraint from 0002 has to stop applying to journeys. Two taxi
  rides on the same car on the same day are normal; two hires of it are not.
"""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

VERSION = "0004"
NAME = "marketplace: operators, journeys and commission"

CONSTRAINT = "bookings_no_overlap"

# Columns added to tables that already exist. ADD COLUMN works on both SQLite
# and Postgres, so these need no special handling.
ADDED = {
    "vehicles": [
        ("operator_id", "INTEGER"),
    ],
    "bookings": [
        ("booking_type", "VARCHAR(20)"),
        ("operator_id", "INTEGER"),
        ("fare_id", "INTEGER"),
        ("pickup_at", "TIMESTAMP"),
        ("pickup_address", "VARCHAR(240)"),
        ("dropoff_address", "VARCHAR(240)"),
        ("passengers", "INTEGER"),
        ("luggage_count", "INTEGER"),
        ("flight_number", "VARCHAR(20)"),
        ("deposit_amount", "NUMERIC(10, 2)"),
        ("completed_at", "TIMESTAMP"),
    ],
}


def _existing_columns(connection, table):
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _rebuild_bookings_for_sqlite(connection, metadata):
    """SQLite cannot drop NOT NULL, so copy the table through a new definition.

    The DDL is compiled from the live metadata, where the foreign keys to
    vehicles, operators and operator_fares resolve, and only the table name in
    the CREATE is rewritten. Copying the table into a bare MetaData instead
    would leave those keys pointing at tables that are not there.
    """
    source = metadata.tables["bookings"]
    if source.c.vehicle_id.nullable is False:
        return

    ddl = str(CreateTable(source).compile(dialect=connection.dialect))
    # Only the table being created is renamed; the foreign keys name other
    # tables, so nothing else in the statement matches.
    ddl = ddl.replace("CREATE TABLE bookings", "CREATE TABLE bookings_rebuilt", 1)

    connection.execute(text("DROP TABLE IF EXISTS bookings_rebuilt"))
    connection.execute(text(ddl))

    carried = sorted(_existing_columns(connection, "bookings") & set(source.c.keys()))
    columns = ", ".join(f'"{name}"' for name in carried)
    connection.execute(text(
        f"INSERT INTO bookings_rebuilt ({columns}) SELECT {columns} FROM bookings"
    ))

    # The old table's indexes go with it; they are recreated by name afterwards.
    connection.execute(text("DROP TABLE bookings"))
    connection.execute(text("ALTER TABLE bookings_rebuilt RENAME TO bookings"))

    for index in source.indexes:
        connection.execute(CreateIndex(index))


def upgrade(connection, metadata):
    # New tables: operators, operator_fares, commission_entries.
    metadata.create_all(connection, checkfirst=True)

    for table, columns in ADDED.items():
        present = _existing_columns(connection, table)
        for name, sql_type in columns:
            if name in present:
                continue
            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}'))

    # Everything that existed before the marketplace is a rental.
    connection.execute(text(
        "UPDATE bookings SET booking_type = 'rental' WHERE booking_type IS NULL"
    ))
    connection.execute(text(
        "UPDATE bookings SET deposit_amount = 0 WHERE deposit_amount IS NULL"
    ))

    if connection.dialect.name == "sqlite":
        _rebuild_bookings_for_sqlite(connection, metadata)
        return

    if connection.dialect.name != "postgresql":
        return

    connection.execute(text("ALTER TABLE bookings ALTER COLUMN vehicle_id DROP NOT NULL"))
    connection.execute(text(
        "ALTER TABLE bookings ALTER COLUMN booking_type SET DEFAULT 'rental'"))
    connection.execute(text(
        "ALTER TABLE bookings ALTER COLUMN booking_type SET NOT NULL"))
    connection.execute(text(
        "ALTER TABLE bookings ALTER COLUMN deposit_amount SET DEFAULT 0"))
    connection.execute(text(
        "ALTER TABLE bookings ALTER COLUMN deposit_amount SET NOT NULL"))

    # A journey does not hold a car for a date range; only a hire does.
    connection.execute(text(f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    connection.execute(text(f"""
        ALTER TABLE bookings
        ADD CONSTRAINT {CONSTRAINT}
        EXCLUDE USING gist (
            vehicle_id WITH =,
            daterange(start_date, end_date, '[)') WITH &&
        )
        WHERE (status IN ('pending', 'confirmed') AND booking_type = 'rental')
    """))
