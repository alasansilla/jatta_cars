"""The signed-in area for an approved transport operator.

Every query here is filtered to the operator holding the session. An operator
must never be able to see another operator's vehicles, fares, bookings or
earnings, so the filter is applied in one place — `_current()` plus `owned()` —
rather than remembered at each call site.

Customer contact details are shown only once a booking is confirmed. Before
that the operator sees the journey and the fare, which is what they need to
decide, but not the person's email and phone.
"""
import math
import secrets
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, session,
    url_for,
)

from . import commission, routing
from .models import (
    BOOKING_TYPES, DISTANCE_PRICE, FIXED_PRICE, JOURNEY_TYPES, PRICING_MODELS,
    RIDE, TRANSFER, Booking, DriverState, Operator, OperatorFare, Vehicle, db,
)
from .settings import current_settings
from .models import CATEGORIES, TRANSMISSIONS, FUELS
from .media import save_upload

bp = Blueprint("operator", __name__)

FARE_KINDS = (RIDE, TRANSFER)

# Statuses at which a driver may see who the customer is. Before a booking is
# accepted there is no reason for them to hold a stranger's contact details.
CONTACT_VISIBLE_AT = ("confirmed", "accepted", "arriving", "in_progress", "completed")

# Session keys that belong to a signed-in driver.
DRIVER_SESSION_KEYS = ("operator_id", "operator_name", "driver_signed_in_at",
                       "driver_sign_in_method")


@bp.before_request
def check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint == "operator.login":
        return None
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or supplied != expected:
        if request.path.startswith("/operator/api/"):
            return jsonify({"error": "Your session expired. Sign in again."}), 400
        flash("That form expired. Please try again.", "error")
        return redirect(request.referrer or url_for("operator.dashboard"))
    return None


def _account():
    """The driver account this session belongs to, whatever its status, or None.

    A driver who has just joined is signed in but not yet approved: they can see
    their application status and nothing else.
    """
    operator_id = session.get("operator_id")
    if not operator_id:
        return None
    return db.session.get(Operator, operator_id)


def _current():
    """The *approved* driver this session belongs to, or None.

    A suspended or rejected driver loses access immediately, without anyone
    having to clear their session.
    """
    operator = _account()
    if operator is None or not operator.is_approved:
        return None
    return operator


def start_session(operator, method):
    """Sign a driver in. Used by both phone and email sign-in.

    The session is emptied first so nothing from before sign-in — a half-finished
    code, another person's leftovers — carries into the signed-in session. A
    customer's own booking reference is kept, and a fresh CSRF token issued.
    """
    kept = {key: session[key] for key in ("booking_reference",) if key in session}
    session.clear()
    session.update(kept)
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session["operator_id"] = operator.id
    session["operator_name"] = operator.display_name
    session["driver_signed_in_at"] = int(time.time())
    session["driver_sign_in_method"] = method
    session.permanent = True


def end_session():
    for key in DRIVER_SESSION_KEYS + ("phone_flow", "phone_verified"):
        session.pop(key, None)


def signed_in_recently(seconds):
    stamp = session.get("driver_signed_in_at")
    return bool(stamp) and time.time() - int(stamp) <= seconds


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _current() is None:
            if _account() is not None:
                # Signed in, just not approved (yet, or any more).
                return redirect(url_for("driver_auth.status"))
            session.pop("operator_id", None)
            flash("Please sign in to continue.", "error")
            return redirect(url_for("driver_auth.sign_in"))
        return view(*args, **kwargs)

    return wrapped


@bp.context_processor
def inject_operator():
    operator = _current()
    if operator is None:
        return {}
    return {
        "operator": operator,
        "operator_rate": commission.rate_for(operator),
        "contact_visible_at": CONTACT_VISIBLE_AT,
    }


def owned(query, model=Booking):
    """Narrow a query to the signed-in operator. The one place this is decided."""
    operator = _current()
    if operator is None:
        abort(403)
    return query.filter(model.operator_id == operator.id)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        operator = Operator.query.filter(
            db.func.lower(Operator.email) == email
        ).first()

        if operator is not None and operator.can_sign_in and \
                operator.check_password(password):
            start_session(operator, "email")
            target = request.args.get("next")
            if target and target.startswith("/operator") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("operator.dashboard"))

        # Deliberately one message: telling an unapproved applicant that their
        # password was right would confirm the account exists.
        flash("That email and password don't match a driver account that can "
              "sign in this way.", "error")
    return render_template("operator/login.html")


@bp.route("/logout")
def logout():
    end_session()
    flash("You are signed out.", "success")
    return redirect(url_for("public.index"))


@bp.route("/")
@login_required
def dashboard():
    operator = _current()
    upcoming = owned(Booking.query).filter(
        Booking.status.in_(("pending", "confirmed", "accepted", "arriving", "in_progress"))
    ).order_by(Booking.start_date.asc()).limit(10).all()

    counts = {
        "pending": owned(Booking.query).filter_by(status="pending").count(),
        "confirmed": owned(Booking.query).filter(
            Booking.status.in_(("confirmed", "accepted", "arriving", "in_progress"))).count(),
        "completed": owned(Booking.query).filter_by(status="completed").count(),
        "fares": OperatorFare.query.filter_by(operator_id=operator.id).count(),
        "vehicles": Vehicle.query.filter_by(operator_id=operator.id).count(),
        "ready_vehicles": Vehicle.query.filter_by(operator_id=operator.id, is_active=True).count(),
    }
    return render_template(
        "operator/dashboard.html",
        upcoming=upcoming,
        counts=counts,
        statement=commission.statement(operator),
        payout_note=current_settings()["operator_payout_note"],
    )


@bp.route("/bookings")
@login_required
def bookings():
    status = (request.args.get("status") or "").strip()
    query = owned(Booking.query)
    if status:
        query = query.filter(Booking.status == status)
    return render_template(
        "operator/bookings.html",
        bookings=query.order_by(Booking.start_date.desc()).all(),
        status=status,
    )


@bp.route("/bookings/<int:booking_id>")
@login_required
def booking_detail(booking_id):
    # first_or_404 on an *owned* query: another operator's booking is not
    # "forbidden", it simply does not exist as far as this account is concerned,
    # which also stops the 403/404 difference confirming that it is real.
    booking = owned(Booking.query).filter(Booking.id == booking_id).first_or_404()
    return render_template(
        "operator/booking_detail.html",
        booking=booking,
        show_contact=booking.status in CONTACT_VISIBLE_AT,
    )


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@login_required
def update_status(booking_id):
    booking = owned(Booking.query).filter(Booking.id == booking_id).first_or_404()
    if booking.is_on_demand or DriverState.query.filter_by(active_booking_id=booking.id).first():
        # A ride requested now is answered in Driving mode, where the time limit,
        # the location check and the trip stages are enforced.
        flash("Answer ride requests in Driving mode.", "error")
        return redirect(url_for("dispatch.drive"))
    new_status = request.form.get("status")

    # An operator may accept, decline or finish their own work. They may not
    # reopen a completed booking, because that would take back commission the
    # marketplace has already recorded.
    allowed = {
        "pending": ("confirmed", "cancelled"),
        "confirmed": ("completed", "cancelled"),
    }.get(booking.status, ())
    if new_status not in allowed:
        flash("That is not a change you can make to this booking.", "error")
        return redirect(url_for("operator.booking_detail", booking_id=booking.id))

    if new_status == "completed" and booking.needs_quote:
        # Completing is what earns commission. With no fare agreed there is
        # nothing to take a share of, and recording zero would file the job as
        # worth nothing rather than as unpriced.
        flash("Set the fare for this journey before completing it.", "error")
        return redirect(url_for("operator.booking_detail", booking_id=booking.id))

    booking.status = new_status
    commission.sync_for(booking)
    db.session.commit()
    flash(f"Booking {booking.reference} is now {new_status}.", "success")
    return redirect(url_for("operator.booking_detail", booking_id=booking.id))


@bp.route("/fares", methods=["GET", "POST"])
@login_required
def fares():
    operator = _current()

    if request.method == "POST":
        errors = []
        title = (request.form.get("title") or "").strip()
        from_location = (request.form.get("from_location") or "").strip()
        to_location = (request.form.get("to_location") or "").strip()
        kind = request.form.get("kind")
        pricing_model = request.form.get("pricing_model") or FIXED_PRICE

        if len(title) < 3:
            errors.append("Give the route a name.")
        if not from_location or not to_location:
            errors.append("Enter where the journey starts and ends.")
        if kind not in FARE_KINDS:
            errors.append("Choose whether this is a ride or an airport transfer.")
        if pricing_model not in PRICING_MODELS:
            errors.append("Choose how this route is priced.")

        def money(field, label, required):
            raw = (request.form.get(field) or "").strip().replace(",", "")
            if not raw:
                if required:
                    errors.append(f"Enter {label}.")
                return None
            try:
                value = float(raw)
            except ValueError:
                errors.append(f"{label.capitalize()} has to be a number.")
                return None
            if not math.isfinite(value) or value < 0:
                errors.append(f"{label.capitalize()} cannot be negative.")
                return None
            return value

        # A fixed route needs one price; a distance route needs a rate. Only the
        # fields belonging to the chosen model are required, so switching does
        # not demand numbers that make no sense for it.
        fixed = pricing_model == FIXED_PRICE
        price = money("price", "the fare for this journey", fixed) if fixed else None
        base_price = None if fixed else money("base_price", "the base fare", True)
        per_km = None if fixed else money("per_km", "the rate per kilometre", True)
        minimum_price = None if fixed else money("minimum_price", "a minimum fare", False)

        if fixed and price is not None and price <= 0:
            errors.append("Enter the fare you charge for this journey.")
        if not fixed and per_km is not None and per_km <= 0:
            errors.append("A distance fare needs a rate per kilometre above zero.")

        seats = (request.form.get("seats") or "").strip()
        seat_count = int(seats) if seats.isdigit() else None

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            db.session.add(OperatorFare(
                operator_id=operator.id,
                kind=kind,
                title=title,
                from_location=from_location,
                to_location=to_location,
                vehicle_class=(request.form.get("vehicle_class") or "").strip() or None,
                seats=seat_count,
                pricing_model=pricing_model,
                price=price,
                base_price=base_price,
                per_km=per_km,
                minimum_price=minimum_price,
                notes=(request.form.get("notes") or "").strip() or None,
                is_active=True,
            ))
            db.session.commit()
            flash(f"Added “{title}”.", "success")
            return redirect(url_for("operator.fares"))

    return render_template(
        "operator/fares.html",
        fares=OperatorFare.query.filter_by(operator_id=operator.id)
        .order_by(OperatorFare.kind, OperatorFare.title).all(),
        kinds=FARE_KINDS,
        pricing_models=PRICING_MODELS,
        routing_on=routing.routing_available(),
    )


@bp.route("/fares/<int:fare_id>/toggle", methods=["POST"])
@login_required
def toggle_fare(fare_id):
    fare = owned(OperatorFare.query, OperatorFare).filter(
        OperatorFare.id == fare_id).first_or_404()
    fare.is_active = not fare.is_active
    db.session.commit()
    flash(f"“{fare.title}” is now {'listed' if fare.is_active else 'hidden'}.", "success")
    return redirect(url_for("operator.fares"))


@bp.route("/fares/<int:fare_id>/delete", methods=["POST"])
@login_required
def delete_fare(fare_id):
    fare = owned(OperatorFare.query, OperatorFare).filter(
        OperatorFare.id == fare_id).first_or_404()
    if Booking.query.filter_by(fare_id=fare.id).count():
        flash("That route has bookings against it. Hide it instead.", "error")
        return redirect(url_for("operator.fares"))
    title = fare.title
    db.session.delete(fare)
    db.session.commit()
    flash(f"Removed “{title}”.", "success")
    return redirect(url_for("operator.fares"))



@bp.route("/bookings/<int:booking_id>/fare", methods=["POST"])
@login_required
def set_fare(booking_id):
    """Agree a fare on a journey the site could not price automatically."""
    booking = owned(Booking.query).filter(Booking.id == booking_id).first_or_404()

    if booking.is_on_demand:
        # The customer chose this driver at the price the driver published.
        # Changing it afterwards would charge them something they never agreed to.
        flash("The price of a ride requested now was set when the customer chose you, "
              "so it can't be changed.", "error")
        return redirect(url_for("operator.booking_detail", booking_id=booking.id))

    if booking.status in ("completed", "cancelled", "declined", "expired"):
        flash("That booking is closed.", "error")
        return redirect(url_for("operator.booking_detail", booking_id=booking.id))

    raw = (request.form.get("total_price") or "").strip().replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        amount = -1
    if not math.isfinite(amount) or amount < 0:
        flash("Enter the fare as a number.", "error")
        return redirect(url_for("operator.booking_detail", booking_id=booking.id))

    booking.total_price = amount
    booking.quote_basis = "manual"
    db.session.commit()
    flash(f"Fare for {booking.reference} set to {amount}.", "success")
    return redirect(url_for("operator.booking_detail", booking_id=booking.id))


@bp.route("/cars")
@login_required
def cars():
    return render_template("operator/cars.html", cars=owned(
        Vehicle.query, Vehicle).order_by(Vehicle.created_at.desc()).all())


@bp.route("/cars/new", methods=["GET", "POST"])
@bp.route("/cars/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required
def car_form(vehicle_id=None):
    car = owned(Vehicle.query, Vehicle).filter_by(id=vehicle_id).first_or_404() \
        if vehicle_id is not None else None
    if request.method == "POST":
        # Editing a car must not change the vehicle promised on an open booking.
        if car and (Booking.query.filter(Booking.vehicle_id == car.id,
                Booking.status.in_(("pending", "confirmed", "accepted", "arriving",
                                    "in_progress"))).first() or
                DriverState.query.filter_by(vehicle_id=car.id, available=True).first()):
            flash("Go offline and finish your open bookings before changing this car.", "error")
            return redirect(url_for("operator.cars"))
        values, errors = {}, []
        values["service_mode"] = request.form.get("service_mode", car.service_mode if car else "both")
        if values["service_mode"] not in ("taxi", "rental", "both"):
            errors.append("Choose taxi rides, car rental, or both.")
        for key, label in (("make", "Make"), ("model", "Model")):
            value = request.form.get(key, "").strip()
            if not 1 <= len(value) <= 60:
                errors.append(f"Enter {label.lower()} using 1–60 characters.")
            values[key] = value
        for key, choices in (("category", CATEGORIES), ("transmission", TRANSMISSIONS),
                             ("fuel", FUELS)):
            values[key] = request.form.get(key)
            if values[key] not in choices:
                errors.append(f"Choose a valid {key}.")
        for key, low, high in (("year", 1950, datetime.utcnow().year + 1),
                               ("seats", 1, 20), ("doors", 1, 8), ("luggage", 0, 20)):
            try:
                values[key] = int(request.form.get(key, ""))
                if not low <= values[key] <= high:
                    raise ValueError()
            except ValueError:
                errors.append(f"Enter {key} between {low} and {high}.")
        for key in ("daily_rate", "weekly_rate", "deposit"):
            if values["service_mode"] == "taxi":
                values[key] = None if key == "weekly_rate" else 0
                continue
            raw = request.form.get(key, "").strip().replace(",", "")
            if key == "weekly_rate" and not raw:
                values[key] = None
                continue
            try:
                amount = Decimal(raw)
                if not amount.is_finite() or not 0 <= amount <= Decimal("99999999.99"):
                    raise ValueError()
                if key != "deposit" and amount == 0:
                    raise ValueError()
                values[key] = amount.quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                errors.append(f"Enter a valid {key.replace('_', ' ')} in Dalasi.")
        for key in ("description", "features"):
            values[key] = request.form.get(key, "").strip()
            if len(values[key]) > 5000:
                errors.append(f"Keep {key} under 5,000 characters.")
        upload = request.files.get("photo_file")
        if not errors and upload and upload.filename:
            asset, error = save_upload(upload)
            if error:
                errors.append(error)
            else:
                values["image"] = asset.path
        if not errors:
            if car is None:
                car = Vehicle(operator_id=_current().id)
                db.session.add(car)
            for key, value in values.items():
                setattr(car, key, value)
            # Every changed listing is reviewed; posted owner/publish fields are ignored.
            car.is_active = False
            car.review_pending = True
            if car.id:
                DriverState.query.filter_by(vehicle_id=car.id).update({"available": False})
            db.session.commit()
            flash("Car saved. Staff will check it before it appears to customers.", "success")
            return redirect(url_for("operator.cars"))
        for error in errors:
            flash(error, "error")
    return render_template("operator/car_form.html", car=car,
        form=request.form if request.method == "POST" else None,
        categories=CATEGORIES, transmissions=TRANSMISSIONS, fuels=FUELS)
