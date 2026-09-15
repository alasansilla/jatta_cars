"""python -m migrations [--status | --check]

The one command that prepares a database, run before any web worker starts
(Render: `python -m migrations && gunicorn ...`):

1. An empty Postgres database gets the whole schema from supabase/bootstrap.sql
   in one transaction.
2. Pending migration steps are applied, one transaction each, under an advisory
   lock so two deploys cannot run them at the same time.
3. If JATTA_ADMIN_USER and JATTA_ADMIN_PASSWORD are set and there is no staff
   account yet, the first one is created.

Any failure exits non-zero, so the start command stops before gunicorn and the
host keeps the previous version running. The database URL is never printed.

--status lists steps. --check changes nothing and exits 1 when steps are pending.
"""
import argparse
import sys

from app import create_app, schema_setup
from app.models import db
from migrations.runner import pending, status, upgrade


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true",
                        help="list steps and whether each has run")
    parser.add_argument("--check", action="store_true",
                        help="change nothing; exit 1 if any step is pending")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        # Never print the URL: it carries the database password.
        print(f"Database: {uri.split('://', 1)[0]}")

        if args.status:
            for version, name, done in status(db.engine):
                print(f"  [{'x' if done else ' '}] {version}  {name}")
            return 0

        if args.check:
            with db.engine.connect() as connection:
                waiting = [step.VERSION for step in pending(connection)]
            print("  pending: " + ", ".join(waiting) if waiting else "  up to date")
            return 1 if waiting else 0

        created = schema_setup.apply_bootstrap_if_empty(app, db.engine)
        if created == "created":
            print("  empty database: schema created from supabase/bootstrap.sql")
        upgrade(db.engine, db.metadata)

        state = schema_setup.schema_state(db.engine)
        if db.engine.dialect.name == "postgresql" and state != "ok":
            print(f"  schema is not current after upgrade: {state}", file=sys.stderr)
            return 1

        outcome = schema_setup.create_first_admin(app)
        if outcome == "created":
            print("  first staff account created; remove JATTA_ADMIN_PASSWORD now")
        elif outcome == "too short":
            print("  JATTA_ADMIN_PASSWORD is too short; no staff account created",
                  file=sys.stderr)
        print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
