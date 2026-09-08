"""Application configuration.

Values that differ between machines come from environment variables so that
nothing secret has to live in the repository.
"""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Change this in production: export JATTA_SECRET_KEY="..."
    SECRET_KEY = os.environ.get("JATTA_SECRET_KEY", "dev-only-not-for-production")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "JATTA_DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "jatta.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Business details shown across the site.
    COMPANY_NAME = "Jatta Cars"
    COMPANY_TAGLINE = "Car hire, made simple."
    COMPANY_EMAIL = "hello@jattacars.com"
    COMPANY_PHONE = "+49 30 123 4567"
    COMPANY_ADDRESS = "Hauptstrasse 1, 10827 Berlin, Germany"

    # Currency symbol used for every price on the site.
    CURRENCY = "€"

    # Where customers can collect and return a vehicle.
    LOCATIONS = [
        "Berlin City Centre",
        "Berlin Brandenburg Airport (BER)",
        "Berlin Hauptbahnhof",
        "Potsdam",
    ]

    # Shown as a ribbon on the home page booking panel. Set to "" to hide it.
    PROMO_MESSAGE = os.environ.get(
        "JATTA_PROMO", "Free cancellation up to 24 hours before pick-up"
    )

    # Insurance excess quoted on the home page.
    DEFAULT_EXCESS = 750

    # Booking rules.
    MIN_RENTAL_DAYS = 1
    MAX_RENTAL_DAYS = 90
    # How far ahead of today a booking may start.
    MAX_ADVANCE_DAYS = 365
