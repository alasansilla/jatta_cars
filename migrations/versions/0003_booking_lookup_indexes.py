"""Indexes for the queries the site actually runs.

Availability is checked on every fleet search and every booking, and the staff
dashboard filters by status and date. Without these, both become a full scan of
the bookings table as it grows.
"""
from sqlalchemy import text

VERSION = "0003"
NAME = "booking lookup indexes"

STATEMENTS = [
    # The availability check: vehicle plus overlapping dates, blocking statuses.
    "CREATE INDEX IF NOT EXISTS ix_bookings_vehicle_dates "
    "ON bookings (vehicle_id, start_date, end_date)",
    # The dashboard: upcoming bookings by status.
    "CREATE INDEX IF NOT EXISTS ix_bookings_status_start "
    "ON bookings (status, start_date)",
    # Fleet listing filters on what is public.
    "CREATE INDEX IF NOT EXISTS ix_vehicles_active "
    "ON vehicles (is_active)",
]


def upgrade(connection, metadata):
    for statement in STATEMENTS:
        connection.execute(text(statement))
