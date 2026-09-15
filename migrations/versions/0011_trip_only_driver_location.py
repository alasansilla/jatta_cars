"""Keep a driver's position only while they are on an accepted trip.

Adds `driver_states.location_at`, the time of the driver's last position, kept
apart from `updated_at`, which becomes a location-free heartbeat for "online".
Positions held by drivers who are not on an active trip are cleared: Driving
mode no longer sends location before a trip is accepted, so nothing should be
kept from before.
"""
from sqlalchemy import inspect, text

VERSION = "0011"
NAME = "driver location only during an accepted trip"


def upgrade(connection, metadata):
    columns = {column["name"] for column in inspect(connection).get_columns("driver_states")}
    if "location_at" not in columns:
        connection.execute(text("ALTER TABLE driver_states ADD COLUMN location_at TIMESTAMP"))
    connection.execute(text("""
        UPDATE driver_states SET lat = NULL, lng = NULL, location_at = NULL
        WHERE active_booking_id IS NULL
           OR active_booking_id NOT IN (
                SELECT id FROM bookings
                WHERE status IN ('accepted', 'confirmed', 'arriving', 'in_progress'))
    """))
