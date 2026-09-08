"""Move an existing SQLite site — content, settings and pictures — to Supabase.

Reads the local SQLite database and the local upload folder, and writes them to
whatever JATTA_DATABASE_URL and the SUPABASE_* variables point at.

    python tools/transfer_to_postgres.py            # show what it would do
    python tools/transfer_to_postgres.py --commit   # actually do it

Rules it keeps to:

  * The source is only ever read. Nothing is deleted or changed locally.
  * Destination ids are assigned by the destination, never carried over, so a
    non-empty destination is fine. A map of source id -> destination id is kept
    in a file beside the source database, and bookings are rewritten through it.
    Copying `vehicle_id` verbatim would silently attach bookings to the wrong
    car the moment the destination already had rows in it.
  * Two identical cars stay two cars. They are told apart by source id, not by
    make and model, so a fleet with two of the same Corolla does not collapse
    into one.
  * Re-runnable. Anything already transferred is skipped, and pictures are
    uploaded with overwrite so a half-finished run can simply be run again.
  * Staff accounts are not copied. Password hashes belong to one site; create
    the production account with tools/create_admin.py.
"""
import argparse
import json
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
MAP_FILE = os.path.join(ROOT, "instance", "transfer-map.json")


# --- the source-to-destination id map ---------------------------------------

def load_map(path=None):
    path = path or MAP_FILE
    if not os.path.exists(path):
        return {"vehicles": {}, "media": []}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("vehicles", {})
    data.setdefault("media", [])
    return data


def save_map(mapping, path=None):
    path = path or MAP_FILE
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, indent=2, sort_keys=True)


def columns(model):
    return [column.name for column in model.__table__.columns]


def row_values(instance, model):
    values = {name: getattr(instance, name) for name in columns(model)}
    values.pop("id", None)  # the destination assigns its own
    return values


# --- the transfer -----------------------------------------------------------

def transfer_vehicles(source_session, mapping, commit):
    """Copy cars, recording source id -> destination id.

    Identity is the source id, not the make and model, so two identical cars
    remain two cars and a re-run does not create a third.
    """
    known = mapping["vehicles"]
    live_ids = {vehicle.id for vehicle in Vehicle.query.all()}
    copied = skipped = 0

    for vehicle in source_session.query(Vehicle).order_by(Vehicle.id).all():
        source_id = str(vehicle.id)
        if source_id in known and known[source_id] in live_ids:
            skipped += 1
            continue
        if commit:
            created = Vehicle(**row_values(vehicle, Vehicle))
            db.session.add(created)
            db.session.flush()          # assigns the id without ending the transaction
            known[source_id] = created.id
        copied += 1

    if commit:
        db.session.commit()
    return copied, skipped


def transfer_bookings(source_session, mapping, commit):
    """Copy bookings, rewriting vehicle_id through the map.

    A booking whose car did not make it across is skipped and reported rather
    than pointed at whatever vehicle happens to hold that id at the destination.
    """
    known = mapping["vehicles"]
    existing = {booking.reference for booking in Booking.query.all()}
    copied = skipped = 0
    orphaned = []

    for booking in source_session.query(Booking).order_by(Booking.id).all():
        if booking.reference in existing:
            skipped += 1
            continue
        destination_vehicle = known.get(str(booking.vehicle_id))
        if destination_vehicle is None:
            orphaned.append(booking.reference)
            continue
        if commit:
            values = row_values(booking, Booking)
            values["vehicle_id"] = destination_vehicle
            db.session.add(Booking(**values))
        copied += 1

    if commit:
        db.session.commit()
    return copied, skipped, orphaned


def transfer_simple(model, key, source_session, commit):
    """Tables with no foreign keys, matched on a column that identifies a row."""
    existing = {getattr(row, key) for row in model.query.all()} if key else None
    seen_count = model.query.count()
    copied = skipped = 0

    for row in source_session.query(model).all():
        if key is not None and getattr(row, key) in existing:
            skipped += 1
            continue
        if key is None and seen_count:
            # Enquiries have nothing unique about them. Once the destination has
            # any, assume a previous run brought them and leave it alone rather
            # than duplicating the lot.
            skipped += 1
            continue
        if commit:
            db.session.add(model(**row_values(row, model)))
        copied += 1

    if commit:
        db.session.commit()
    return copied, skipped


def transfer_files(mapping, commit, uploads=None):
    """Upload local files. Returns (sent, skipped, missing).

    Uploads overwrite, so re-running after a failure is safe and does not
    collide with a half-written object from the previous attempt.
    """
    uploads = uploads or UPLOADS
    if not os.path.isdir(uploads):
        return [], [], []

    storage = get_storage()
    done = set(mapping.get("media", []))
    sent, skipped, missing = [], [], []

    for asset in MediaAsset.query.all():
        source = os.path.join(uploads, asset.filename)
        if not os.path.exists(source):
            missing.append(asset.filename)
            continue
        if asset.filename in done:
            skipped.append(asset.filename)
            continue
        if commit:
            with open(source, "rb") as handle:
                storage.save(UPLOAD_PREFIX + asset.filename, handle.read(), overwrite=True)
            done.add(asset.filename)
        sent.append(asset.filename)

    mapping["media"] = sorted(done)
    return sent, skipped, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="write; without it nothing is changed")
    parser.add_argument("--source", default=os.environ.get("JATTA_SOURCE_URL", DEFAULT_SQLITE),
                        help="database to read from (default: the local SQLite file)")
    parser.add_argument("--map", default=MAP_FILE,
                        help="where the source-to-destination id map is kept")
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

    mapping = load_map(args.map)
    source_engine = create_engine(args.source)

    with app.app_context(), Session(source_engine) as source_session:
        copied, skipped = transfer_simple(Setting, "key", source_session, args.commit)
        print(f"  {'settings':16s} {copied:4d} to copy   {skipped:4d} already there")

        copied, skipped = transfer_vehicles(source_session, mapping, args.commit)
        print(f"  {'vehicles':16s} {copied:4d} to copy   {skipped:4d} already there")

        copied, skipped = transfer_simple(MediaAsset, "filename", source_session, args.commit)
        print(f"  {'media_assets':16s} {copied:4d} to copy   {skipped:4d} already there")

        copied, skipped, orphaned = transfer_bookings(source_session, mapping, args.commit)
        print(f"  {'bookings':16s} {copied:4d} to copy   {skipped:4d} already there")
        for reference in orphaned:
            print(f"      SKIPPED {reference}: its car is not in the map")

        copied, skipped = transfer_simple(Enquiry, None, source_session, args.commit)
        print(f"  {'enquiries':16s} {copied:4d} to copy   {skipped:4d} already there")

        try:
            sent, already, missing = transfer_files(mapping, args.commit)
        except StorageError as error:
            if args.commit:
                save_map(mapping, args.map)
            print(f"\nStorage error: {error}\nRe-run to continue where it stopped.",
                  file=sys.stderr)
            return 1
        print(f"  {'pictures':16s} {len(sent):4d} to upload {len(already):4d} already there")
        for name in missing:
            print(f"      no local file for {name}")

    if args.commit:
        save_map(mapping, args.map)
        print(f"\nDone. Id map saved to {os.path.relpath(args.map, ROOT)} — keep it if "
              f"you intend to run this again.")
        print("Staff accounts were not copied; create one with tools/create_admin.py.")
    else:
        print("\nDry run. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
