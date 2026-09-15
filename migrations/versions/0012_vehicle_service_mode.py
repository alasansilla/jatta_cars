"""Separate taxi and rental vehicle availability; preserve existing listings."""
from sqlalchemy import inspect, text
VERSION = "0012"
NAME = "vehicle taxi and rental service mode"
def upgrade(connection, metadata):
    columns = {c["name"] for c in inspect(connection).get_columns("vehicles")}
    if "service_mode" not in columns:
        connection.execute(text("ALTER TABLE vehicles ADD COLUMN service_mode VARCHAR(10) NOT NULL DEFAULT 'both' CHECK (service_mode IN ('taxi', 'rental', 'both'))"))
