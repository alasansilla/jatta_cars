"""Database models for the Jatta Cars transport marketplace."""
import secrets
import string
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Booking states that occupy a vehicle for their date range.
BLOCKING_STATUSES = ("pending", "confirmed")

# The life of an on-demand ride. The customer picks a driver and the request
# waits for that driver ("pending"). The driver accepts, drives to the pickup,
# starts and finishes the trip. If the driver says no, or does not answer in
# time, the trip is "declined" or "expired" and the customer chooses again —
# nobody else is ever put in the chosen driver's place without the customer
# picking them.
RIDE_WAITING = ("pending",)
RIDE_ACTIVE = ("accepted", "confirmed", "arriving", "in_progress")
RIDE_REOPENABLE = ("declined", "expired")
RIDE_STATUSES = ("pending", "accepted", "arriving", "in_progress", "completed",
                 "declined", "expired", "cancelled")

# What a booking is for. A rental holds a car for a range of days; a ride and an
# airport transfer are a single journey at a point in time. They share one table
# so that privacy, references and commission have exactly one implementation
# rather than three that drift apart.
RENTAL = "rental"
RIDE = "ride"
TRANSFER = "transfer"
BOOKING_TYPES = (RENTAL, RIDE, TRANSFER)
JOURNEY_TYPES = (RIDE, TRANSFER)

# An operator is a separate business. Nothing they own is visible to the public
# until someone has approved them.
OPERATOR_STATUSES = ("pending", "approved", "suspended", "rejected")

# How an operator prices a route, and how a booking ended up with its number.
FIXED_PRICE = "fixed"
DISTANCE_PRICE = "distance"
MANUAL_QUOTE = "manual"
PRICING_MODELS = (FIXED_PRICE, DISTANCE_PRICE)

CATEGORIES = ["Economy", "Compact", "Estate", "SUV", "Van", "Luxury"]
TRANSMISSIONS = ["Manual", "Automatic"]
FUELS = ["Petrol", "Diesel", "Hybrid", "Electric"]


class Operator(db.Model):
    """An approved transport business listing on the marketplace.

    Rates and terms belong to the operator, not to the marketplace, so nothing
    here carries a price the site invented.
    """

    __tablename__ = "operators"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), nullable=False, unique=True, index=True)

    contact_name = db.Column(db.String(120), nullable=True)
    # Optional. Drivers who join with their phone number never give one; older
    # accounts that applied by email keep theirs and can still sign in with it.
    email = db.Column(db.String(160), nullable=True, unique=True, index=True)
    # A contact number as typed, by the applicant or by staff. It has never been
    # proved to belong to anyone, so nothing treats it as a way in.
    phone = db.Column(db.String(40), nullable=True)

    # The driver's sign-in number, in E.164 form (+2207701234), set only after
    # the driver has typed back a code texted to it. Unique, so one number can
    # only ever open one account, whatever spacing or prefix it was typed with.
    phone_e164 = db.Column(db.String(16), nullable=True, unique=True, index=True)
    phone_verified_at = db.Column(db.DateTime, nullable=True)

    # Set when the operator is approved and given a sign-in. Null means they
    # applied but cannot sign in yet.
    password_hash = db.Column(db.String(255), nullable=True)

    status = db.Column(db.String(20), nullable=False, default="pending", index=True)

    # Overrides the marketplace-wide rate for this operator when set. Null means
    # "use the configured default", so changing the default moves everyone who
    # has not been given their own deal.
    commission_rate = db.Column(db.Numeric(5, 2), nullable=True)

    service_area = db.Column(db.String(200), nullable=True)
    terms = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)  # staff-only, never shown publicly

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)

    vehicles = db.relationship("Vehicle", back_populates="operator")
    bookings = db.relationship("Booking", back_populates="operator")
    fares = db.relationship("OperatorFare", back_populates="operator",
                            cascade="all, delete-orphan")

    @property
    def is_approved(self):
        return self.status == "approved"

    @property
    def can_sign_in(self):
        """Whether the older email-and-password sign-in works for this account."""
        return self.is_approved and bool(self.password_hash) and bool(self.email)

    @property
    def display_name(self):
        """What customers see: the person's name, falling back to the listing name."""
        return self.contact_name or self.name

    def set_password(self, password):
        # pbkdf2 rather than Werkzeug's scrypt default: some Python builds are
        # compiled without the OpenSSL support that hashlib.scrypt needs.
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def make_slug(name, existing=None):
        base = "".join(c.lower() if c.isalnum() else "-" for c in (name or "operator"))
        base = "-".join(part for part in base.split("-") if part)[:100] or "operator"
        candidate, suffix = base, 2
        taken = existing if existing is not None else set()
        while candidate in taken or Operator.query.filter_by(slug=candidate).first():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def __repr__(self):
        return f"<Operator {self.slug} {self.status}>"


class OperatorFare(db.Model):
    """A price an operator publishes for a journey.

    The marketplace never sets these. An empty table means the site shows an
    empty state, not a made-up price.
    """

    __tablename__ = "operator_fares"

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("operators.id"), nullable=False,
                            index=True)
    operator = db.relationship("Operator", back_populates="fares")

    kind = db.Column(db.String(20), nullable=False, default=RIDE, index=True)
    title = db.Column(db.String(160), nullable=False)
    from_location = db.Column(db.String(120), nullable=False)
    to_location = db.Column(db.String(120), nullable=False)

    vehicle_class = db.Column(db.String(60), nullable=True)
    seats = db.Column(db.Integer, nullable=True)

    # How this route is priced. "fixed" is one price for the journey; "distance"
    # is a base plus a rate per kilometre, which needs a measured route.
    pricing_model = db.Column(db.String(20), nullable=False, default=FIXED_PRICE)

    # The fixed price. Null on a distance fare, which has no single number.
    price = db.Column(db.Numeric(10, 2), nullable=True)

    base_price = db.Column(db.Numeric(10, 2), nullable=True)
    per_km = db.Column(db.Numeric(10, 2), nullable=True)
    minimum_price = db.Column(db.Numeric(10, 2), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def is_bookable(self):
        return bool(self.is_active and self.operator and self.operator.is_approved)

    @property
    def is_distance_based(self):
        return self.pricing_model == DISTANCE_PRICE

    @property
    def display_price(self):
        """What to show on a card before any route is known.

        A distance fare has no single number, so it says so rather than
        inventing one.
        """
        if self.is_distance_based:
            return None
        return self.price

    def quote(self, distance_m=None):
        """Price this journey. Returns (amount, basis).

        `amount` is None when the fare cannot be worked out — a distance fare
        with no measured route. The caller must then ask the operator rather
        than guess, which is why this returns None instead of zero.
        """
        if not self.is_distance_based:
            if self.price is None:
                return None, MANUAL_QUOTE
            return Decimal(str(self.price)), FIXED_PRICE

        if distance_m is None:
            return None, MANUAL_QUOTE

        base = Decimal(str(self.base_price or 0))
        rate = Decimal(str(self.per_km or 0))
        kilometres = Decimal(str(distance_m)) / Decimal("1000")
        amount = (base + rate * kilometres).quantize(Decimal("0.01"),
                                                     rounding=ROUND_HALF_UP)
        if self.minimum_price is not None:
            amount = max(amount, Decimal(str(self.minimum_price)))
        return amount, DISTANCE_PRICE

    def __repr__(self):
        return f"<OperatorFare {self.id} {self.title}>"


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

    # Null means the marketplace's own car, from before operators existed.
    operator_id = db.Column(db.Integer, db.ForeignKey("operators.id"), nullable=True,
                            index=True)
    operator = db.relationship("Operator", back_populates="vehicles")

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
            Booking.booking_type == RENTAL,
            Booking.status.in_(BLOCKING_STATUSES),
            Booking.start_date < end,
            Booking.end_date > start,
        )
        if ignore_booking_id is not None:
            query = query.filter(Booking.id != ignore_booking_id)
        return query.first() is None

    @property
    def is_bookable(self):
        """Listed, and belonging to nobody or to an approved operator.

        Suspending an operator takes their cars off the site immediately,
        without anyone having to remember to unlist each one.
        """
        if not self.is_active:
            return False
        if self.operator_id is None:
            return True
        return bool(self.operator and self.operator.is_approved)

    def __repr__(self):
        return f"<Vehicle {self.id} {self.name}>"


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(12), nullable=False, unique=True, index=True)

    # What was booked. A rental holds a car for a range of days; a ride or an
    # airport transfer is one journey at a point in time.
    booking_type = db.Column(db.String(20), nullable=False, default=RENTAL, index=True)

    # Null for a journey the operator has not yet put a car against. A rental
    # always names one.
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)
    vehicle = db.relationship("Vehicle", back_populates="bookings")

    # Who fulfils it. Null on the marketplace's own rentals from before
    # operators existed.
    operator_id = db.Column(db.Integer, db.ForeignKey("operators.id"), nullable=True,
                            index=True)
    operator = db.relationship("Operator", back_populates="bookings")

    fare_id = db.Column(db.Integer, db.ForeignKey("operator_fares.id"), nullable=True)
    fare = db.relationship("OperatorFare")

    customer_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(40), nullable=True)

    pickup_location = db.Column(db.String(120), nullable=False)
    dropoff_location = db.Column(db.String(120), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)

    # Journey details. Unused by a rental, which works in whole days.
    pickup_at = db.Column(db.DateTime, nullable=True)
    pickup_address = db.Column(db.String(240), nullable=True)
    dropoff_address = db.Column(db.String(240), nullable=True)
    passengers = db.Column(db.Integer, nullable=True)
    luggage_count = db.Column(db.Integer, nullable=True)
    flight_number = db.Column(db.String(20), nullable=True)

    # Where the journey runs, when a geocoder could place it. The typed
    # addresses above are always kept: they are what the customer actually
    # wrote, and they stand on their own when no coordinates were found.
    pickup_lat = db.Column(db.Numeric(9, 6), nullable=True)
    pickup_lng = db.Column(db.Numeric(9, 6), nullable=True)
    dropoff_lat = db.Column(db.Numeric(9, 6), nullable=True)
    dropoff_lng = db.Column(db.Numeric(9, 6), nullable=True)

    # The measured route, and who measured it. Recorded so a fare can be
    # explained later, and so a provider change is visible in the history.
    route_distance_m = db.Column(db.Integer, nullable=True)
    route_duration_s = db.Column(db.Integer, nullable=True)
    route_provider = db.Column(db.String(80), nullable=True)
    route_meta = db.Column(db.Text, nullable=True)

    # How total_price was arrived at: a fixed fare, a distance calculation, or
    # not yet priced at all.
    quote_basis = db.Column(db.String(20), nullable=True)

    # The fare, and what commission is charged on.
    #
    # Null means nobody has been able to price it yet — a distance fare with no
    # measured route. Storing zero instead would read as "free" to a customer
    # and would quietly earn the marketplace nothing, so the column allows null
    # and the pages say the operator will confirm.
    total_price = db.Column(db.Numeric(10, 2), nullable=True)

    # The refundable deposit as it stood when the booking was made, kept
    # separately so commission can exclude it even if the car's deposit is
    # edited afterwards. It is never part of total_price.
    deposit_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    # On-demand rides only. The one-time token from the customer's estimate,
    # unique so a double-tapped "Request" can never create two trips; and when
    # the chosen driver was last asked, which is what their time to answer runs
    # from.
    request_token = db.Column(db.String(64), nullable=True, unique=True, index=True)
    requested_at = db.Column(db.DateTime, nullable=True)

    commission = db.relationship("CommissionEntry", back_populates="booking",
                                 uselist=False, cascade="all, delete-orphan")

    @property
    def is_on_demand(self):
        """A ride requested now from a chosen driver, rather than booked ahead."""
        return self.request_token is not None

    @property
    def is_journey(self):
        """A scheduled ride or an airport transfer, rather than a hire."""
        return self.booking_type in JOURNEY_TYPES

    @property
    def days(self):
        return (self.end_date - self.start_date).days

    @property
    def has_route(self):
        return self.route_distance_m is not None and self.route_duration_s is not None

    @property
    def route_distance_km(self):
        if self.route_distance_m is None:
            return None
        return round(self.route_distance_m / 1000, 1)

    @property
    def route_duration_label(self):
        if self.route_duration_s is None:
            return None
        minutes = int(round(self.route_duration_s / 60))
        if minutes < 60:
            return f"{minutes} min"
        hours, rest = divmod(minutes, 60)
        return f"{hours}h {rest:02d}m"

    @property
    def needs_quote(self):
        """True when the operator still has to say what this costs."""
        return self.total_price is None

    @property
    def commissionable_amount(self):
        """What commission is charged on: the fare, never the deposit.

        The deposit is the customer's money held against damage and handed back,
        so taking a cut of it would be charging for something nobody earned.
        """
        return float(self.total_price or 0)

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
        return f"<Booking {self.reference} {self.booking_type} {self.status}>"


class CommissionEntry(db.Model):
    """What the marketplace earned on one completed booking.

    Written once, when a booking is marked completed, and then left alone. The
    rate is copied in rather than looked up later, so changing the commission
    setting never rewrites what was already earned.
    """

    __tablename__ = "commission_entries"

    id = db.Column(db.Integer, primary_key=True)

    # One entry per booking, enforced by the database rather than by whoever
    # remembers to check first.
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False,
                           unique=True, index=True)
    booking = db.relationship("Booking", back_populates="commission")

    operator_id = db.Column(db.Integer, db.ForeignKey("operators.id"), nullable=True,
                            index=True)
    operator = db.relationship("Operator")

    rate_percent = db.Column(db.Numeric(5, 2), nullable=False)
    base_amount = db.Column(db.Numeric(10, 2), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)

    recorded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<CommissionEntry booking={self.booking_id} {self.amount}>"


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


class BookingReview(db.Model):
    __tablename__ = 'booking_reviews'
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, unique=True)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    booking = db.relationship('Booking')
    __table_args__ = (db.CheckConstraint('rating >= 1 AND rating <= 5', name='review_rating_range'),)


class DriverState(db.Model):
    """A signed-in driver's Driving mode: online or not, which car, which trip.

    `updated_at` is the driver's heartbeat: Driving mode sends one while it is
    open, with no location in it. A driver whose heartbeat is older than two
    minutes is treated as offline.

    `lat`, `lng` and `location_at` are the driver's position, and exist only
    while the driver holds an accepted trip. Driving mode starts sending it when
    the driver accepts, and every way a trip ends (finish, cancel, decline,
    expiry, going offline) clears it. Nothing records where a driver has been.
    """
    __tablename__ = "driver_states"
    operator_id = db.Column(db.Integer, db.ForeignKey("operators.id"), primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)
    available = db.Column(db.Boolean, nullable=False, default=False)
    active_booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True, unique=True)
    lat = db.Column(db.Float, nullable=True)
    lng = db.Column(db.Float, nullable=True)
    location_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)

    def forget_location(self):
        self.lat = self.lng = self.location_at = None


class PhoneCode(db.Model):
    """A one-time code texted to a phone number.

    Only a keyed hash of the code is stored, so reading this table does not let
    anyone sign in. A code is closed the moment it is used, replaced by a newer
    one, or guessed wrongly too often; `closed_at` is set once and never cleared.
    """

    __tablename__ = "phone_codes"

    id = db.Column(db.Integer, primary_key=True)
    phone_e164 = db.Column(db.String(16), nullable=False, index=True)
    # What proving the number is for: signing in or joining ("sign_in"), or
    # putting a number on an account that is already signed in ("add_phone").
    purpose = db.Column(db.String(20), nullable=False, default="sign_in")
    code_hash = db.Column(db.String(64), nullable=False)
    # Ties the code to the browser that asked for it.
    session_hash = db.Column(db.String(64), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)
    outcome = db.Column(db.String(20), nullable=True)  # used, replaced, locked

    def __repr__(self):
        return f"<PhoneCode {self.id} {self.purpose} {self.outcome or 'open'}>"


class AuthEvent(db.Model):
    """One counted event for rate limiting sign-in: a text sent, a wrong code.

    The subject is a keyed hash of a phone number or an IP address, never the
    number or address itself, so the table can be counted without being a list
    of who tried to sign in.
    """

    __tablename__ = "auth_events"
    __table_args__ = (
        db.Index("ix_auth_events_kind_subject_time", "kind", "subject_hash", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    subject_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
