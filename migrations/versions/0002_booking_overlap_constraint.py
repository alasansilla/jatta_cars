"""Stop two people booking the same car for overlapping dates.

The application checks availability before writing, but that is a read followed
by a write: two requests can both pass the check and both insert. On a laptop
you would never see it; on a host running several instances, two people hitting
"book" on the last car at the same moment is exactly when it matters.

Postgres can enforce this properly with an exclusion constraint, so the database
refuses the second write no matter how the race is timed. The ranges match the
application's rule exactly — inclusive of the pick-up day, exclusive of the
return day — and only pending and confirmed bookings hold a car.

SQLite has no equivalent, so on a development database this step does nothing
and the application-level check stands alone.
"""
from sqlalchemy import text

VERSION = "0002"
NAME = "no overlapping bookings for one vehicle (postgres)"

CONSTRAINT = "bookings_no_overlap"


def upgrade(connection, metadata):
    if connection.dialect.name != "postgresql":
        return

    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))

    already = connection.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = :name"
    ), {"name": CONSTRAINT}).first()
    if already:
        return

    connection.execute(text(f"""
        ALTER TABLE bookings
        ADD CONSTRAINT {CONSTRAINT}
        EXCLUDE USING gist (
            vehicle_id WITH =,
            daterange(start_date, end_date, '[)') WITH &&
        )
        WHERE (status IN ('pending', 'confirmed'))
    """))
