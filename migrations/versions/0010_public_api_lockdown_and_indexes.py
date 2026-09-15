"""Lock every application table away from Supabase's public API, and add the
indexes the models declare.

The live database was built from an early bootstrap.sql (which locked the tables
it knew about) and then upgraded by these migration steps. Migration 0004
created `operators`, `operator_fares` and `commission_entries` without turning
on row-level security or revoking the `anon` and `authenticated` roles, and the
columns 0004 added to existing tables got no indexes on Postgres. A database
made by bootstrap.sql and one made by migrations must end up the same; this
step makes them so.

Idempotent: every index is created only if missing, and enabling RLS or
revoking privileges twice changes nothing. Tables that do not exist yet are
skipped; the step that creates a later table locks it itself.
"""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex

VERSION = "0010"
NAME = "public API lockdown and index parity"

PUBLIC_ROLES = ("anon", "authenticated")


def lock_tables(connection, table_names):
    """Enable RLS (no policies: nothing for API roles) and revoke their grants."""
    if connection.dialect.name != "postgresql":
        return
    roles = [row[0] for row in connection.execute(
        text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
        {"names": list(PUBLIC_ROLES)})]
    present = set(inspect(connection).get_table_names())
    for name in table_names:
        if name not in present:
            continue
        connection.execute(text(f'ALTER TABLE public."{name}" ENABLE ROW LEVEL SECURITY'))
        if roles:
            connection.execute(text(
                f'REVOKE ALL ON TABLE public."{name}" FROM {", ".join(roles)}'))
    if roles:
        who = ", ".join(roles)
        connection.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {who}"))
        connection.execute(text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {who}"))
        connection.execute(text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {who}"))


def upgrade(connection, metadata):
    present = set(inspect(connection).get_table_names())
    for table in metadata.sorted_tables:
        if table.name not in present:
            continue
        existing = {index["name"] for index in inspect(connection).get_indexes(table.name)}
        columns = {column["name"] for column in inspect(connection).get_columns(table.name)}
        for index in table.indexes:
            if index.name in existing:
                continue
            if not {column.name for column in index.columns} <= columns:
                continue  # a later step adds the column, and its index with it
            connection.execute(CreateIndex(index))

    lock_tables(connection, [table.name for table in metadata.sorted_tables]
                + ["schema_migrations"])
