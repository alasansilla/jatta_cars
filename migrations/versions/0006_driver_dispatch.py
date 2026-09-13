from sqlalchemy import text
VERSION = "0006"
NAME = "driver availability and dispatch"
def upgrade(connection, metadata):
    metadata.tables["driver_states"].create(connection, checkfirst=True)
    if connection.dialect.name == "postgresql":
        connection.execute(text("ALTER TABLE driver_states ENABLE ROW LEVEL SECURITY"))
        connection.execute(text("REVOKE ALL ON driver_states FROM anon, authenticated"))
