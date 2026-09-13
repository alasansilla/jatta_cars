"""Record where a journey actually goes, and how its fare was worked out.

Adds coordinates, the measured route and the provider that measured it to
bookings, and gives operator fares a choice between one fixed price and a base
plus a rate per kilometre.

One column changes rather than appears: `bookings.total_price` becomes nullable.
A distance fare with no measured route cannot be priced, and storing zero would
read as "free" to the customer and earn the marketplace nothing. Null means "the
operator still has to say", which the pages show plainly.

`operator_fares.price` becomes nullable for the same reason: a distance fare has
no single number to put in it.

SQLite cannot drop NOT NULL, so both tables are rebuilt from the model metadata
and their rows copied across, exactly as migration 0004 did for bookings.
"""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

VERSION = "0005"
NAME = "route planning: coordinates, distance and distance-based fares"

ADDED = {
    "bookings": [
        ("pickup_lat", "NUMERIC(9, 6)"),
        ("pickup_lng", "NUMERIC(9, 6)"),
        ("dropoff_lat", "NUMERIC(9, 6)"),
        ("dropoff_lng", "NUMERIC(9, 6)"),
        ("route_distance_m", "INTEGER"),
        ("route_duration_s", "INTEGER"),
        ("route_provider", "VARCHAR(80)"),
        ("route_meta", "TEXT"),
        ("quote_basis", "VARCHAR(20)"),
    ],
    "operator_fares": [
        ("pricing_model", "VARCHAR(20)"),
        ("base_price", "NUMERIC(10, 2)"),
        ("per_km", "NUMERIC(10, 2)"),
        ("minimum_price", "NUMERIC(10, 2)"),
    ],
}


def _existing_columns(connection, table):
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _rebuild_for_sqlite(connection, metadata, table_name):
    """Copy a table through its current model definition to relax NOT NULL.

    The DDL is compiled from the live metadata so foreign keys resolve; only the
    table name in the CREATE is rewritten.
    """
    source = metadata.tables[table_name]
    staging = f"{table_name}_rebuilt"

    ddl = str(CreateTable(source).compile(dialect=connection.dialect))
    ddl = ddl.replace(f"CREATE TABLE {table_name}", f"CREATE TABLE {staging}", 1)

    connection.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    connection.execute(text(ddl))

    carried = sorted(_existing_columns(connection, table_name) & set(source.c.keys()))
    columns = ", ".join(f'"{name}"' for name in carried)
    connection.execute(text(
        f"INSERT INTO {staging} ({columns}) SELECT {columns} FROM {table_name}"))

    connection.execute(text(f"DROP TABLE {table_name}"))
    connection.execute(text(f"ALTER TABLE {staging} RENAME TO {table_name}"))

    for index in source.indexes:
        connection.execute(CreateIndex(index))


def upgrade(connection, metadata):
    metadata.create_all(connection, checkfirst=True)

    for table, columns in ADDED.items():
        present = _existing_columns(connection, table)
        for name, sql_type in columns:
            if name in present:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    # Everything priced before today was a single fixed fare.
    connection.execute(text(
        "UPDATE operator_fares SET pricing_model = 'fixed' WHERE pricing_model IS NULL"))
    connection.execute(text(
        "UPDATE bookings SET quote_basis = 'fixed' "
        "WHERE quote_basis IS NULL AND total_price IS NOT NULL"))

    if connection.dialect.name == "sqlite":
        # Rebuild only if the constraint is actually still there.
        if metadata.tables["bookings"].c.total_price.nullable:
            _rebuild_for_sqlite(connection, metadata, "bookings")
        if metadata.tables["operator_fares"].c.price.nullable:
            _rebuild_for_sqlite(connection, metadata, "operator_fares")
        return

    if connection.dialect.name != "postgresql":
        return

    connection.execute(text("ALTER TABLE bookings ALTER COLUMN total_price DROP NOT NULL"))
    connection.execute(text("ALTER TABLE operator_fares ALTER COLUMN price DROP NOT NULL"))
    connection.execute(text(
        "ALTER TABLE operator_fares ALTER COLUMN pricing_model SET DEFAULT 'fixed'"))
    connection.execute(text(
        "ALTER TABLE operator_fares ALTER COLUMN pricing_model SET NOT NULL"))
