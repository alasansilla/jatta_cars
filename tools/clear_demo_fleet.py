"""Remove the European demo cars from an existing database.

The sample fleet that used to ship with `seed.py` is priced in euros and has
nothing to do with a Gambian operation. This removes it, but only when doing so
is safe:

  * a car with any booking against it is never touched
  * a car whose photo has been changed, or whose description has been edited
    away from the seeded text, is treated as yours and kept

Run it, read what it plans to do, then run it again with --yes:

    python tools/clear_demo_fleet.py
    python tools/clear_demo_fleet.py --yes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import Booking, Vehicle, db  # noqa: E402
from seed import DEMO_FLEET  # noqa: E402

DEMO_KEYS = {(spec["make"], spec["model"]) for spec in DEMO_FLEET}
DEMO_DESCRIPTIONS = {spec["description"] for spec in DEMO_FLEET}


def classify(vehicle):
    """Return None if the vehicle can go, otherwise why it is being kept."""
    if (vehicle.make, vehicle.model) not in DEMO_KEYS:
        return "not part of the demo fleet"
    if Booking.query.filter_by(vehicle_id=vehicle.id).count():
        return "has bookings against it"
    if vehicle.image:
        return "has a photo you uploaded"
    if vehicle.description and vehicle.description not in DEMO_DESCRIPTIONS:
        return "the description has been edited"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        removable, kept = [], []
        for vehicle in Vehicle.query.order_by(Vehicle.id).all():
            reason = classify(vehicle)
            (kept if reason else removable).append((vehicle, reason))

        for vehicle, reason in kept:
            print(f"  keep    {vehicle.name:28s} — {reason}")
        for vehicle, _ in removable:
            print(f"  remove  {vehicle.name}")

        if not removable:
            print("\nNothing to remove.")
            return

        if not args.yes:
            print(f"\n{len(removable)} car(s) would be removed. "
                  f"Re-run with --yes to go ahead.")
            return

        for vehicle, _ in removable:
            db.session.delete(vehicle)
        db.session.commit()
        print(f"\nRemoved {len(removable)} demo car(s). "
              f"{Vehicle.query.count()} left in the fleet.")


if __name__ == "__main__":
    main()
