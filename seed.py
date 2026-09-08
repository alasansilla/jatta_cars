"""Create the database and fill it with a starting fleet.

Run once after cloning:

    python seed.py

It is safe to run again: existing vehicles and the admin account are left alone.
Set JATTA_ADMIN_PASSWORD beforehand to choose the password yourself, otherwise a
random one is generated and printed.
"""
import os
import secrets
import string

from app import create_app
from app.models import AdminUser, Vehicle, db

FLEET = [
    {
        "make": "Fiat", "model": "500", "year": 2023, "category": "Economy",
        "transmission": "Manual", "fuel": "Petrol",
        "seats": 4, "doors": 3, "luggage": 1,
        "daily_rate": 34, "weekly_rate": 205, "deposit": 200,
        "description": (
            "The one to take if you are staying inside the Ringbahn. Tiny turning "
            "circle, fits in parking spaces nobody else wants, and cheap to run all week."
        ),
        "features": "Air conditioning\nBluetooth audio\nUSB charging\nParking sensors",
    },
    {
        "make": "Volkswagen", "model": "Polo", "year": 2024, "category": "Economy",
        "transmission": "Manual", "fuel": "Petrol",
        "seats": 5, "doors": 5, "luggage": 2,
        "daily_rate": 39, "weekly_rate": 235, "deposit": 250,
        "description": (
            "A sensible small hatchback that does everything asked of it. Five doors, "
            "room for two suitcases, and comfortable enough for a run down to Dresden."
        ),
        "features": "Air conditioning\nApple CarPlay & Android Auto\nCruise control\nReversing camera",
    },
    {
        "make": "Volkswagen", "model": "Golf", "year": 2024, "category": "Compact",
        "transmission": "Manual", "fuel": "Diesel",
        "seats": 5, "doors": 5, "luggage": 3,
        "daily_rate": 52, "weekly_rate": 315, "deposit": 300,
        "description": (
            "The default answer to most hire questions. Quiet on the Autobahn, frugal "
            "on a long run, and big enough in the back for four adults who like each other."
        ),
        "features": "Air conditioning\nApple CarPlay & Android Auto\nAdaptive cruise control\nReversing camera\nHeated seats",
    },
    {
        "make": "Toyota", "model": "Corolla Hybrid", "year": 2024, "category": "Compact",
        "transmission": "Automatic", "fuel": "Hybrid",
        "seats": 5, "doors": 5, "luggage": 3,
        "daily_rate": 58, "weekly_rate": 350, "deposit": 300,
        "description": (
            "Automatic, hybrid and genuinely relaxing in city traffic, where it spends "
            "much of its time on the electric motor. Around 4.5 l/100km in real use."
        ),
        "features": "Automatic gearbox\nHybrid drivetrain\nApple CarPlay & Android Auto\nAdaptive cruise control\nLane assist\nReversing camera",
    },
    {
        "make": "Škoda", "model": "Octavia Estate", "year": 2023, "category": "Estate",
        "transmission": "Automatic", "fuel": "Diesel",
        "seats": 5, "doors": 5, "luggage": 5,
        "daily_rate": 64, "weekly_rate": 385, "deposit": 350,
        "description": (
            "640 litres of boot before you drop the seats. The one people book when the "
            "hire involves a house move, a dog, or a family that overpacks."
        ),
        "features": "Automatic gearbox\nHuge boot\nApple CarPlay & Android Auto\nAdaptive cruise control\nRoof rails\nTowbar on request",
    },
    {
        "make": "Volkswagen", "model": "Tiguan", "year": 2024, "category": "SUV",
        "transmission": "Automatic", "fuel": "Diesel",
        "seats": 5, "doors": 5, "luggage": 4,
        "daily_rate": 79, "weekly_rate": 475, "deposit": 400,
        "description": (
            "Higher up, four-wheel drive, and happiest on a long trip with the boot full. "
            "A good choice for the Baltic coast in winter or the Alps at any time of year."
        ),
        "features": "Automatic gearbox\n4MOTION all-wheel drive\nApple CarPlay & Android Auto\nAdaptive cruise control\nHeated seats\nRoof rails\n360° camera",
    },
    {
        "make": "BMW", "model": "320i", "year": 2024, "category": "Luxury",
        "transmission": "Automatic", "fuel": "Petrol",
        "seats": 5, "doors": 4, "luggage": 3,
        "daily_rate": 89, "weekly_rate": 535, "deposit": 500,
        "description": (
            "For the trip where turning up matters. Leather, an eight-speed automatic and "
            "the sort of long-distance composure that makes 600km feel like 300."
        ),
        "features": "Automatic gearbox\nLeather upholstery\nHeated seats\nAdaptive cruise control\nWireless phone charging\nHarman Kardon audio\nSat nav",
    },
    {
        "make": "Tesla", "model": "Model 3", "year": 2024, "category": "Luxury",
        "transmission": "Automatic", "fuel": "Electric",
        "seats": 5, "doors": 4, "luggage": 3,
        "daily_rate": 95, "weekly_rate": 570, "deposit": 500,
        "description": (
            "Around 500km of range and access to the Supercharger network, which makes "
            "long trips straightforward. Handed over charged; bring it back above 20%."
        ),
        "features": "Electric — around 500km range\nSupercharger access included\nAutomatic\nHeated seats\nSat nav with charge planning\nGlass roof",
    },
    {
        "make": "Volkswagen", "model": "Multivan", "year": 2023, "category": "Van",
        "transmission": "Automatic", "fuel": "Diesel",
        "seats": 7, "doors": 5, "luggage": 6,
        "daily_rate": 119, "weekly_rate": 715, "deposit": 600,
        "description": (
            "Seven proper seats and space for everyone's luggage behind them. The usual "
            "booking is a family holiday, a band, or a wedding party that needs one vehicle."
        ),
        "features": "Seven seats\nAutomatic gearbox\nSliding doors both sides\nAir conditioning front and rear\nApple CarPlay & Android Auto\nReversing camera\nIsofix throughout",
    },
]


def make_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(14))


def main():
    app = create_app()
    with app.app_context():
        db.create_all()

        added = 0
        for spec in FLEET:
            exists = Vehicle.query.filter_by(make=spec["make"], model=spec["model"]).first()
            if exists:
                continue
            db.session.add(Vehicle(**spec))
            added += 1
        db.session.commit()

        username = os.environ.get("JATTA_ADMIN_USER", "admin")
        admin = AdminUser.query.filter_by(username=username).first()
        created_password = None
        if admin is None:
            created_password = os.environ.get("JATTA_ADMIN_PASSWORD") or make_password()
            admin = AdminUser(username=username)
            admin.set_password(created_password)
            db.session.add(admin)
            db.session.commit()

        print(f"Fleet: {added} vehicle(s) added, {Vehicle.query.count()} in total.")
        if created_password:
            print()
            print("  Staff login created")
            print(f"  username: {username}")
            print(f"  password: {created_password}")
            print("  Sign in at /admin/login — store this somewhere safe, it is not shown again.")
        else:
            print(f"Staff login '{username}' already exists; password left unchanged.")


if __name__ == "__main__":
    main()
