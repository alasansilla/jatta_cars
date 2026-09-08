"""python -m migrations [--status]"""
import argparse
import sys

from app import create_app
from app.models import db
from migrations.runner import status, upgrade


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true",
                        help="list steps and whether each has run")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        # Never print the URL: it carries the database password.
        backend = uri.split("://", 1)[0]
        print(f"Database: {backend}")

        if args.status:
            for version, name, done in status(db.engine):
                print(f"  [{'x' if done else ' '}] {version}  {name}")
            return 0

        upgrade(db.engine, db.metadata)
        print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
