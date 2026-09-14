"""Five demo drivers for trying the marketplace on a local computer.

An empty marketplace can't be judged: with no drivers there is nothing to
compare, no profile to open and no price to weigh. This fills the local database
with five obviously fake driver accounts so the pages can be clicked through.

It is development scaffolding, never content:

* Refuses to run in production, and refuses any database that is not a local
  SQLite file — so it can never write to Supabase.
* Every name says "Demo driver", every profile says it is not a real driver,
  and every car description says it is not a real car for hire.
* No invented people: no personal names, no phone numbers (a made-up Gambian
  number could belong to a real person), emails only at example.com, which can
  never receive mail.
* No fake activity: no reviews, no bookings, no positions. The drivers start
  offline. A location appears only when someone signs in as a demo driver and
  switches on Driving mode in a browser that shares a real location.
* The car makes and models are real, because that is a fact about the world.
  The prices are placeholders for trying the pages, not anybody's rates.

    python tools/seed_demo_drivers.py            # show what it would do
    python tools/seed_demo_drivers.py --yes      # create them
    python tools/seed_demo_drivers.py --remove --yes   # take them out again
"""
import argparse
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import (  # noqa: E402
    Booking, BookingReview, CommissionEntry, DriverState, Operator, OperatorFare, Vehicle, db,
)

# Every demo row carries this in a staff-only notes field. Removal keys on it,
# so nothing a real driver creates can be swept up by accident.
MARKER = "[DEMO DATA - created by tools/seed_demo_drivers.py - not a real driver]"
DEMO_DOMAIN = "example.com"
PUBLIC_NOTE = "Demo account for trying the site locally. This is not a real driver."
CAR_NOTE = "Demo listing for trying the site locally. This is not a real car for hire."

DRIVERS = [
    {"area": "Kololi and Senegambia",
     "car": dict(make="Toyota", model="Corolla", year=2012, category="Economy",
                 transmission="Manual", fuel="Petrol", seats=4, doors=4, luggage=2,
                 daily_rate=Decimal("2500"), deposit=Decimal("5000")),
     "ride": dict(base_price=Decimal("150"), per_km=Decimal("45"), minimum_price=Decimal("300")),
     "transfer": dict(title="Demo price: Banjul airport to Kololi", from_location="Banjul International Airport",
                      to_location="Kololi", price=Decimal("1200"), seats=4)},
    {"area": "Serrekunda and Westfield",
     "car": dict(make="Toyota", model="HiAce", year=2011, category="Van",
                 transmission="Manual", fuel="Diesel", seats=14, doors=4, luggage=10,
                 daily_rate=Decimal("6000"), deposit=Decimal("10000")),
     "ride": dict(base_price=Decimal("250"), per_km=Decimal("60"), minimum_price=Decimal("500")),
     "transfer": dict(title="Demo price: airport transfer for groups", from_location="Banjul International Airport",
                      to_location="Serrekunda", price=Decimal("3000"), seats=14)},
    {"area": "Brikama and the South Bank road",
     "car": dict(make="Toyota", model="RAV4", year=2015, category="SUV",
                 transmission="Automatic", fuel="Petrol", seats=5, doors=5, luggage=4,
                 daily_rate=Decimal("4500"), deposit=Decimal("12000")),
     "ride": dict(base_price=Decimal("200"), per_km=Decimal("55"), minimum_price=Decimal("400")),
     "transfer": None},
    {"area": "Bakau and Fajara",
     "car": dict(make="Nissan", model="Almera", year=2016, category="Economy",
                 transmission="Manual", fuel="Petrol", seats=4, doors=4, luggage=2,
                 daily_rate=Decimal("2300"), deposit=Decimal("5000")),
     "ride": dict(base_price=Decimal("120"), per_km=Decimal("40"), minimum_price=Decimal("250")),
     "transfer": dict(title="Demo price: Banjul airport to Bakau", from_location="Banjul International Airport",
                      to_location="Bakau", price=Decimal("1000"), seats=4)},
    {"area": "Kotu and Cape Point",
     "car": dict(make="Mercedes-Benz", model="C-Class", year=2010, category="Luxury",
                 transmission="Automatic", fuel="Diesel", seats=4, doors=4, luggage=3,
                 daily_rate=Decimal("4000"), deposit=Decimal("15000")),
     "ride": dict(base_price=Decimal("300"), per_km=Decimal("70"), minimum_price=Decimal("600")),
     "transfer": None},
]


def demo_drivers():
    return Operator.query.filter(Operator.notes == MARKER).all()


def refuse_unless_local(app):
    if str(app.config.get("ENV_NAME", "")).lower() in ("production", "prod"):
        return "this is a production environment"
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"):
        return "the database is not a local SQLite file"
    return None


def seed(commit, password):
    if demo_drivers():
        print("Demo drivers already exist. Remove them first with --remove --yes.")
        return 1

    print(f"Would create {len(DRIVERS)} demo drivers (approved, offline, no reviews, no bookings):")
    for number, spec in enumerate(DRIVERS, 1):
        car = spec["car"]
        print(f"  Demo driver {number}: {car['make']} {car['model']} {car['year']}, "
              f"{spec['area']}, sign in with demo-driver-{number}@{DEMO_DOMAIN}")
    if not commit:
        print("\nDry run. Add --yes to create them.")
        return 0

    for number, spec in enumerate(DRIVERS, 1):
        name = f"Demo driver {number}"
        driver = Operator(
            name=name, contact_name=name, slug=Operator.make_slug(f"demo-driver-{number}"),
            email=f"demo-driver-{number}@{DEMO_DOMAIN}", phone=None, phone_e164=None,
            status="approved", service_area=spec["area"], terms=PUBLIC_NOTE, notes=MARKER,
        )
        driver.set_password(password)
        db.session.add(driver)
        db.session.flush()

        car = Vehicle(operator_id=driver.id, is_active=True, description=CAR_NOTE, **spec["car"])
        db.session.add(car)
        db.session.flush()

        db.session.add(OperatorFare(
            operator_id=driver.id, kind="ride", pricing_model="distance",
            title="Demo price: rides by the kilometre", from_location="Pickup",
            to_location="Destination", seats=car.seats if car.seats <= 8 else 8,
            notes=CAR_NOTE, is_active=True, **spec["ride"]))
        if spec["transfer"]:
            db.session.add(OperatorFare(
                operator_id=driver.id, kind="transfer", pricing_model="fixed",
                notes=CAR_NOTE, is_active=True, **spec["transfer"]))

        # Offline, no position. Nothing here pretends a driver is out on the road.
        db.session.add(DriverState(operator_id=driver.id, vehicle_id=car.id, available=False,
                                   active_booking_id=None, lat=None, lng=None, updated_at=None))
    db.session.commit()

    print(f"\nCreated {len(DRIVERS)} demo drivers.")
    print("Sign in as one at /operator/login with its example.com email and the password "
          "you passed with --password (or the default in this script).")
    print("They are offline until someone switches on Driving mode in a browser.")
    return 0


def remove(commit):
    drivers = demo_drivers()
    if not drivers:
        print("No demo drivers found.")
        return 0
    ids = [driver.id for driver in drivers]
    bookings = Booking.query.filter(Booking.operator_id.in_(ids)).all()
    booking_ids = [booking.id for booking in bookings]
    print(f"Would remove {len(drivers)} demo driver(s), their cars, prices and "
          f"{len(bookings)} booking(s) made with them while testing.")
    if not commit:
        print("\nDry run. Add --yes to remove them.")
        return 0

    if booking_ids:
        BookingReview.query.filter(BookingReview.booking_id.in_(booking_ids)).delete(synchronize_session=False)
        CommissionEntry.query.filter(CommissionEntry.booking_id.in_(booking_ids)).delete(synchronize_session=False)
    DriverState.query.filter(DriverState.operator_id.in_(ids)).delete(synchronize_session=False)
    DriverState.query.filter(DriverState.active_booking_id.in_(booking_ids or [0])).update(
        {"active_booking_id": None}, synchronize_session=False)
    Booking.query.filter(Booking.id.in_(booking_ids or [0])).delete(synchronize_session=False)
    OperatorFare.query.filter(OperatorFare.operator_id.in_(ids)).delete(synchronize_session=False)
    Vehicle.query.filter(Vehicle.operator_id.in_(ids)).delete(synchronize_session=False)
    Operator.query.filter(Operator.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    print(f"Removed {len(drivers)} demo driver(s).")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="actually write")
    parser.add_argument("--remove", action="store_true", help="remove the demo drivers")
    parser.add_argument("--password", default="demo-driver-local-only",
                        help="email sign-in password for the demo accounts")
    args = parser.parse_args()

    app = create_app()
    problem = refuse_unless_local(app)
    if problem:
        print(f"Refusing to run: {problem}. Demo drivers are for a local database only.",
              file=sys.stderr)
        return 2
    with app.app_context():
        return remove(args.yes) if args.remove else seed(args.yes, args.password)


if __name__ == "__main__":
    sys.exit(main())
