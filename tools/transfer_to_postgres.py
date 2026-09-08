"""Move an existing SQLite site — content, settings and pictures — to Supabase.

Reads the local SQLite database and the local upload folder, and writes them to
whatever JATTA_DATABASE_URL and the SUPABASE_* variables point at.

    python tools/transfer_to_postgres.py            # show what it would do
    python tools/transfer_to_postgres.py --commit   # actually do it

Rules it keeps to:

  * The source is only ever read. Nothing is deleted or changed locally.
  * A row that already exists at the destination is left alone, matched on the
    column that identifies it (a booking reference, a setting key, a filename).
    So the transfer can be run twice, or resumed after a failure.
  * Staff accounts are not copied. Password hashes are for one site; create the
    production account with tools/create_admin.py instead.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import create_app  # noqa: E402
from app.models import (  # noqa: E402
    Booking, Enquiry, MediaAsset, Setting, Vehicle, db,
)
from app.storage import UPLOAD_PREFIX, StorageError, get_storage  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SQLITE = "sqlite:///" + os.path.join(ROOT, "instance", "jatta.db")
UPLOADS = os.path.join(ROOT, "app", "static", "uploads")

# Each table, and the column that says "this is the same record".
TABLES = [
    (Setting, "key"),
    (Vehicle, None),          # no natural key; matched on make+model+year below
    (MediaAsset, "filename"),
    (Booking, "reference"),
    (Enquiry, None),
]


def columns(model):
    return [c.name for c in model.__table__.columns]


def row_to_dict(instance, model):
    return {name: getattr(instance, name) for name in columns(model)}


def identity(instance, model, key):
    if key:
        return (getattr(instance, key),)
    if model is Vehicle:
        return (instance.make, instance.model, instance.year)
    return (instance.created_at, getattr(instance, "email", None),
            getattr(instance, "message", None))


def transfer_rows(source_session, commit):
    """Copy table contents. Returns a list of (table, copied, skipped)."""
    report = []
    for model, key in TABLES:
        existing = {identity(row, model, key) for row in model.query.all()}
        copied = skipped = 0
        for row in source_session.query(model).all():
            if identity(row, model, key) in existing:
                skipped += 1
                continue
            if commit:
                values = row_to_dict(row, model)
                values.pop("id", None)  # let the destination assign its own
                db.session.add(model(**values))
            copied += 1
        if commit:
            db.session.commit()
        report.append((model.__tablename__, copied, skipped))
    return report


def transfer_files(commit):
    """Upload local files to the configured storage. Returns (sent, missing)."""
    if not os.path.isdir(UPLOADS):
        return [], []

    storage = get_storage()
    sent, missing = [], []
    for asset in MediaAsset.query.all():
        source = os.path.join(UPLOADS, asset.filename)
        if not os.path.exists(source):
            missing.append(asset.filename)
            continue
        if commit:
            with open(source, "rb") as handle:
                storage.save(UPLOAD_PREFIX + asset.filename, handle.read())
        sent.append(asset.filename)
    return sent, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is changed")
    parser.add_argument("--source", default=os.environ.get("JATTA_SOURCE_URL", DEFAULT_SQLITE),
                        help="database to read from (default: the local SQLite file)")
    args = parser.parse_args()

    app = create_app()
    destination = app.config["SQLALCHEMY_DATABASE_URI"]

    if destination.startswith("sqlite") and args.source.startswith("sqlite"):
        print("Source and destination are both SQLite. Set JATTA_DATABASE_URL to "
              "the Supabase connection string first.", file=sys.stderr)
        return 1

    print(f"From: {args.source.split('://')[0]}  (read only)")
    print(f"To:   {destination.split('://')[0]}")
    print("Mode: WRITING\n" if args.commit else "Mode: dry run — nothing will change\n")

    source_engine = create_engine(args.source)
    with app.app_context(), Session(source_engine) as source_session:
        for table, copied, skipped in transfer_rows(source_session, args.commit):
            print(f"  {table:16s} {copied:4d} to copy   {skipped:4d} already there")

        try:
            sent, missing = transfer_files(args.commit)
        except StorageError as error:
            print(f"\nStorage error: {error}", file=sys.stderr)
            return 1
        print(f"  {'pictures':16s} {len(sent):4d} to upload {len(missing):4d} missing locally")
        for name in missing:
            print(f"      no local file for {name}")

    if not args.commit:
        print("\nDry run. Re-run with --commit to apply.")
    else:
        print("\nDone. Staff accounts were not copied — "
              "create one with tools/create_admin.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
