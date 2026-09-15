"""Make submitted cars visible in an explicit staff review queue."""
from sqlalchemy import inspect, text
VERSION = "0013"
NAME = "driver car review queue"
def upgrade(connection, metadata):
    columns = {c["name"] for c in inspect(connection).get_columns("vehicles")}
    if "review_pending" not in columns:
        connection.execute(text("ALTER TABLE vehicles ADD COLUMN review_pending BOOLEAN NOT NULL DEFAULT FALSE"))
        connection.execute(text("UPDATE vehicles SET review_pending = TRUE WHERE is_active = FALSE AND operator_id IS NOT NULL"))
