"""Database models for the Jatta Cars rental site."""
import secrets
import string
from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Booking states that occupy a vehicle for their date range.
BLOCKING_STATUSES = ("pending", "confirmed")

CATEGORIES = ["Economy", "Compact", "Estate", "SUV", "Van", "Luxury"]
TRANSMISSIONS = ["Manual", "Automatic"]
FUELS = ["Petrol", "Diesel", "Hybrid", "Electric"]


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    make = db.Column(db.String(60), nullable=False)
    model = db.Column(db.String(60), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(30), nullable=False, default="Economy")

    transmission = db.Column(db.String(20), nullable=False, default="Manual")
    fuel = db.Column(db.String(20), nullable=False, default="Petrol")
    seats = db.Column(db.Integer, nullable=False, default=5)
    doors = db.Column(db.Integer, nullable=False, default=5)
    luggage = db.Column(db.Integer, nullable=False, default=2)

    daily_rate = db.Column(db.Numeric(10, 2), nullable=False)
    weekly_rate = db.Column(db.Numeric(10, 2), nullable=True)
    deposit = db.Column(db.Numeric(10, 2), nullable=False, default=250)

    image = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    features = db.Column(db.Text, nullable=True)  # one per line

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    bookings = db.relationship("Booking", back_populates="vehicle", cascade="all, delete-orphan")

    @property
    def name(self):
        return f"{self.make} {self.model}"

    @property
    def feature_list(self):
        if not self.features:
            return []
        return [line.strip() for line in self.features.splitlines() if line.strip()]

    @property
    def image_url(self):
        return self.image or f"img/car-{self.category.lower()}.svg"

    def quote(self, days):
        """Total price for `days` days.

        Whole weeks are charged at the weekly rate when one is set, which is
        cheaper than the daily rate; leftover days are charged daily. The
        straight daily price is used if it happens to work out lower.
        """
        days = max(int(days), 1)
        daily = float(self.daily_rate)
        straight = daily * days
        if not self.weekly_rate:
            return round(straight, 2)
        weeks, remainder = divmod(days, 7)
        tiered = weeks * float(self.weekly_rate) + remainder * daily
        return round(min(straight, tiered), 2)

    def is_available(self, start, end, ignore_booking_id=None):
        """True if nothing blocks this vehicle between `start` and `end`.

        Ranges are inclusive of the pickup day and exclusive of the return day,
        so one customer may return on the morning another collects.
        """
        query = Booking.query.filter(
            Booking.vehicle_id == self.id,
            Booking.status.in_(BLOCKING_STATUSES),
            Booking.start_date < end,
            Booking.end_date > start,
        )
        if ignore_booking_id is not None:
            query = query.filter(Booking.id != ignore_booking_id)
        return query.first() is None

    def __repr__(self):
        return f"<Vehicle {self.id} {self.name}>"


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(12), nullable=False, unique=True, index=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    vehicle = db.relationship("Vehicle", back_populates="bookings")

    customer_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(40), nullable=True)

    pickup_location = db.Column(db.String(120), nullable=False)
    dropoff_location = db.Column(db.String(120), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)

    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def days(self):
        return (self.end_date - self.start_date).days

    @property
    def is_past(self):
        return self.end_date < date.today()

    @staticmethod
    def new_reference():
        """A short, human-readable booking reference such as JC-7QK4M2."""
        alphabet = string.ascii_uppercase + string.digits
        while True:
            ref = "JC-" + "".join(secrets.choice(alphabet) for _ in range(6))
            if not Booking.query.filter_by(reference=ref).first():
                return ref

    def __repr__(self):
        return f"<Booking {self.reference} {self.status}>"


class AdminUser(db.Model):
    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, password):
        # pbkdf2 rather than Werkzeug's scrypt default: some Python builds are
        # compiled without the OpenSSL support that hashlib.scrypt needs.
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<AdminUser {self.username}>"


class Enquiry(db.Model):
    """A message sent through the contact form.

    The site does not send email, so enquiries are stored here and read in the
    admin area. Wire up SMTP later if you want them forwarded to an inbox.
    """

    __tablename__ = "enquiries"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(40), nullable=True)
    subject = db.Column(db.String(160), nullable=True)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Enquiry {self.id} from {self.email}>"


class Setting(db.Model):
    """One editable site setting. Absent rows fall back to the schema default."""

    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Setting {self.key}>"


class MediaAsset(db.Model):
    """An image uploaded through the staff area."""

    __tablename__ = "media_assets"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False, unique=True)
    original_name = db.Column(db.String(200), nullable=False)
    alt_text = db.Column(db.String(200), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def path(self):
        """Path relative to app/static/, which is how images are referenced."""
        return f"uploads/{self.filename}"

    @property
    def size_label(self):
        kb = self.size_bytes / 1024
        return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"

    def __repr__(self):
        return f"<MediaAsset {self.filename}>"
