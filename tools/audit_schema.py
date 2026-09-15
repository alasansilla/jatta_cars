"""Check a database against the application's models. Changes nothing.

    python tools/audit_schema.py          # against JATTA_DATABASE_URL

Reports, without printing the connection string or any row of data:

* migration steps not yet applied;
* tables, columns and indexes the models declare but the database lacks;
* on Postgres: application tables without row-level security, and any table or
  sequence the Supabase API roles (`anon`, `authenticated`) can still use;
* on Postgres: whether the rental double-booking constraint exists.

Exit status 0 when everything matches, 1 when anything needs attention.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text  # noqa: E402

from app import create_app  # noqa: E402
from app.models import db  # noqa: E402
from migrations.runner import pending  # noqa: E402

API_ROLES = ("anon", "authenticated")


def audit(engine, metadata):
    problems = []
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())

        if "schema_migrations" in tables:
            waiting = [step.VERSION for step in pending(connection)]
            if waiting:
                problems.append("migrations not applied: " + ", ".join(waiting))
        else:
            problems.append("no schema_migrations table: this database was never migrated")

        for table in metadata.sorted_tables:
            if table.name not in tables:
                problems.append(f"missing table: {table.name}")
                continue
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in columns:
                    problems.append(f"missing column: {table.name}.{column.name}")
            indexes = {index["name"] for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in indexes:
                    problems.append(f"missing index: {index.name}")

        if connection.dialect.name == "postgresql":
            app_tables = [t.name for t in metadata.sorted_tables if t.name in tables]
            app_tables += ["schema_migrations"] if "schema_migrations" in tables else []
            unprotected = connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND NOT rowsecurity AND tablename = ANY(:names)"), {"names": app_tables}).scalars().all()
            for name in unprotected:
                problems.append(f"row-level security is off: {name}")

            roles = connection.execute(text(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
                {"names": list(API_ROLES)}).scalars().all()
            for role in roles:
                exposed = connection.execute(text(
                    "WITH t AS MATERIALIZED (SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)) "
                    "SELECT tablename FROM t WHERE has_table_privilege("
                    ":role, format('public.%I', tablename), 'SELECT,INSERT,UPDATE,DELETE')"),
                    {"names": app_tables, "role": role}).scalars().all()
                for name in exposed:
                    problems.append(f"API role {role} has privileges on: {name}")
                sequences = connection.execute(text(
                    "SELECT count(*) FROM information_schema.role_usage_grants "
                    "WHERE grantee = :role AND object_schema = 'public'"), {"role": role}).scalar()
                if sequences:
                    problems.append(f"API role {role} can use {sequences} sequence(s)")

            overlap = connection.execute(text(
                "SELECT 1 FROM pg_constraint WHERE conname = 'bookings_no_overlap'")).first()
            if overlap is None:
                problems.append("missing constraint: bookings_no_overlap (rental double-booking)")
    return problems


def main():
    app = create_app()
    with app.app_context():
        backend = app.config["SQLALCHEMY_DATABASE_URI"].split("://", 1)[0]
        print(f"Database: {backend}")
        problems = audit(db.engine, db.metadata)
    if problems:
        print("Needs attention:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Schema, indexes and API lockdown match the application.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
