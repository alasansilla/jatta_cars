"""Staff area: manage the fleet, bookings and enquiries.

Access is guarded by a simple session login. That is enough for a single-office
operation; move to Flask-Login and per-user accounts if the team grows.
"""
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

import math

from sqlalchemy.exc import IntegrityError

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for
)

from . import commission, sms
from .media import delete_asset, save_upload
from .storage import media_url
from .models import (
    CATEGORIES, FUELS, OPERATOR_STATUSES, RENTAL, TRANSMISSIONS, AdminUser,
    Booking, CommissionEntry, CommissionSettlement, Enquiry, MediaAsset, Operator,
    OperatorFare, Setting, Vehicle, db
)
from .settings import (
    FIELDS, GROUPS, PLACEHOLDER_MARKER, SCHEMA, current_settings, outstanding_items,
    reset_group, save_settings,
)

bp = Blueprint("admin", __name__)

BOOKING_STATUSES = ["pending", "confirmed", "completed", "cancelled"]

# Extra states an on-demand ride passes through. Staff can filter by them, but
# the ride itself is moved on by its driver in Driving mode; staff may only
# complete or cancel one.
RIDE_ONLY_STATUSES = ["accepted", "arriving", "in_progress", "declined", "expired"]
STAFF_RIDE_STATUSES = ["completed", "cancelled"]


@bp.before_request
def check_csrf():
    """Every state-changing staff request must carry the session's token."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint == "admin.login":
        return None  # nothing privileged to protect yet, and no token issued
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or supplied != expected:
        if request.path.startswith("/admin/api/"):
            return jsonify({"error": "Your session expired. Reload the page and sign in again."}), 400
        flash("That form expired. Please try again.", "error")
        return redirect(request.referrer or url_for("admin.dashboard"))
    return None


@bp.context_processor
def inject_admin_counts():
    """Badge counts for the staff navigation."""
    if not session.get("admin_id"):
        return {}
    return {
        "fake_sms_enabled": sms.is_fake(),
        "nav_car_reviews": Vehicle.query.filter_by(review_pending=True).count(),
        "nav_pending": Booking.query.filter_by(status="pending").count(),
        "nav_unread": Enquiry.query.filter_by(is_read=False).count(),
        "nav_todo": len(outstanding_items()),
        "nav_operators": Operator.query.filter_by(status="pending").count(),
        "nav_commission_owed": commission.outstanding() > 0,
        "admin_username": session.get("admin_username"),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            flash("Please sign in to continue.", "error")
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = AdminUser.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session["admin_id"] = user.id
            session["admin_username"] = user.username
            target = request.args.get("next")
            # Only follow relative paths, so the parameter cannot be used to
            # bounce someone to another site after signing in.
            if target and target.startswith("/") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("admin.dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("admin/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("public.index"))


@bp.route("/")
@login_required
def dashboard():
    today = date.today()
    upcoming = (
        Booking.query.filter(
            Booking.start_date >= today,
            Booking.status.in_(["pending", "confirmed"]),
        )
        .order_by(Booking.start_date.asc())
        .limit(8)
        .all()
    )
    stats = {
        "vehicles": Vehicle.query.count(),
        "active_vehicles": Vehicle.query.filter_by(is_active=True).count(),
        "pending": Booking.query.filter_by(status="pending").count(),
        "confirmed": Booking.query.filter_by(status="confirmed").count(),
        "unread_enquiries": Enquiry.query.filter_by(is_read=False).count(),
        "out_today": Booking.query.filter(
            Booking.status == "confirmed",
            Booking.start_date <= today,
            Booking.end_date > today,
        ).count(),
    }
    revenue_30d = sum(
        float(b.total_price)
        for b in Booking.query.filter(
            Booking.status.in_(["confirmed", "completed"]),
            Booking.start_date >= today - timedelta(days=30),
        ).all()
    )
    recent = Booking.query.order_by(Booking.created_at.desc()).limit(8).all()
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        upcoming=upcoming,
        recent=recent,
        revenue_30d=revenue_30d,
    )


# --- Fleet -----------------------------------------------------------------

@bp.route("/vehicles")
@login_required
def vehicles():
    query = Vehicle.query
    if request.args.get("review") == "pending":
        query = query.filter_by(review_pending=True)
    items = query.order_by(
        Vehicle.review_pending.desc(), Vehicle.is_active.desc(), Vehicle.make.asc(), Vehicle.model.asc()
    ).all()
    return render_template("admin/vehicles.html", vehicles=items)


@bp.route("/vehicles/new", methods=["GET", "POST"])
@bp.route("/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required
def vehicle_form(vehicle_id=None):
    vehicle = Vehicle.query.get_or_404(vehicle_id) if vehicle_id else None

    if request.method == "POST":
        form = request.form
        errors = []

        make = (form.get("make") or "").strip()
        model = (form.get("model") or "").strip()
        if not make or not model:
            errors.append("Make and model are both required.")

        def number(field, label, cast=float, required=True, minimum=None):
            raw = (form.get(field) or "").strip()
            if not raw:
                if required:
                    errors.append(f"{label} is required.")
                return None
            try:
                value = cast(raw)
            except ValueError:
                errors.append(f"{label} must be a number.")
                return None
            if minimum is not None and value < minimum:
                errors.append(f"{label} must be at least {minimum}.")
                return None
            return value

        service_mode = form.get("service_mode", vehicle.service_mode if vehicle else "both")
        if service_mode not in ("taxi", "rental", "both"):
            errors.append("Choose taxi rides, car rental, or both.")
        if vehicle and vehicle.service_mode != service_mode:
            from .models import DriverState
            if Booking.query.filter(Booking.vehicle_id == vehicle.id, Booking.status.in_(("pending", "confirmed", "accepted", "arriving", "in_progress"))).first() or DriverState.query.filter_by(vehicle_id=vehicle.id, available=True).first():
                errors.append("Go offline and finish open bookings before changing this car’s use.")
        year = number("year", "Year", int, minimum=1950)
        daily_rate = 0 if service_mode == "taxi" else number("daily_rate", "Daily rate", float, minimum=0.01)
        weekly_rate = None if service_mode == "taxi" else number("weekly_rate", "Weekly rate", float, required=False, minimum=0.01)
        deposit = 0 if service_mode == "taxi" else number("deposit", "Deposit", float, minimum=0)
        seats = number("seats", "Seats", int, minimum=1)
        doors = number("doors", "Doors", int, minimum=1)
        luggage = number("luggage", "Luggage", int, required=False, minimum=0)

        category = form.get("category")
        if category not in CATEGORIES:
            errors.append("Choose a category.")
        transmission = form.get("transmission")
        if transmission not in TRANSMISSIONS:
            errors.append("Choose a transmission.")
        fuel = form.get("fuel")
        if fuel not in FUELS:
            errors.append("Choose a fuel type.")

        raw_driver = (form.get("operator_id") or "").strip()
        driver_id = None
        if raw_driver:
            driver = db.session.get(Operator, int(raw_driver)) if raw_driver.isdigit() else None
            if driver is None:
                errors.append("Choose a driver from the list.")
            else:
                driver_id = driver.id

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template(
                "admin/vehicle_form.html",
                vehicle=vehicle,
                form=form,
                categories=CATEGORIES,
                transmissions=TRANSMISSIONS,
                fuels=FUELS,
                assets=MediaAsset.query.order_by(MediaAsset.uploaded_at.desc()).all(),
                drivers=_driver_choices(),
            )

        if vehicle is None:
            vehicle = Vehicle()
            db.session.add(vehicle)

        if vehicle.operator_id and vehicle.operator_id != driver_id:
            from .models import DriverState
            if DriverState.query.filter(DriverState.vehicle_id == vehicle.id,
                                        DriverState.active_booking_id.isnot(None)).first():
                flash("That car is on a trip right now. Change its driver after the trip.",
                      "error")
                return redirect(url_for("admin.vehicle_form", vehicle_id=vehicle.id))
            # The previous driver can no longer go online in a car that is not theirs.
            DriverState.query.filter_by(vehicle_id=vehicle.id).update(
                {"vehicle_id": None, "available": False})
        vehicle.operator_id = driver_id
        vehicle.make = make
        vehicle.model = model
        vehicle.year = year
        vehicle.category = category
        vehicle.transmission = transmission
        vehicle.fuel = fuel
        vehicle.seats = seats
        vehicle.doors = doors
        vehicle.luggage = luggage or 0
        vehicle.service_mode = service_mode
        vehicle.daily_rate = daily_rate
        vehicle.weekly_rate = weekly_rate
        vehicle.deposit = deposit
        # An uploaded file wins over the library dropdown.
        upload = request.files.get("photo_file")
        if upload is not None and upload.filename:
            asset, upload_error = save_upload(upload)
            if upload_error:
                flash(upload_error, "error")
                return render_template(
                    "admin/vehicle_form.html", vehicle=vehicle, form=form,
                    categories=CATEGORIES, transmissions=TRANSMISSIONS, fuels=FUELS,
                    assets=MediaAsset.query.order_by(MediaAsset.uploaded_at.desc()).all(),
                    drivers=_driver_choices(),
                )
            vehicle.image = asset.path
        else:
            vehicle.image = (form.get("image") or "").strip() or None
        vehicle.description = (form.get("description") or "").strip() or None
        vehicle.features = (form.get("features") or "").strip() or None
        vehicle.is_active = form.get("is_active") == "on"
        if vehicle.is_active:
            vehicle.review_pending = False

        db.session.commit()
        flash(f"Saved {vehicle.name}.", "success")
        return redirect(url_for("admin.vehicles"))

    return render_template(
        "admin/vehicle_form.html",
        vehicle=vehicle,
        form=None,
        categories=CATEGORIES,
        transmissions=TRANSMISSIONS,
        fuels=FUELS,
        assets=MediaAsset.query.order_by(MediaAsset.uploaded_at.desc()).all(),
        drivers=_driver_choices(),
    )


def _driver_choices():
    return sorted(Operator.query.filter(Operator.status.in_(("approved", "pending"))).all(),
                  key=lambda driver: driver.display_name.casefold())


@bp.route("/vehicles/<int:vehicle_id>/toggle", methods=["POST"])
@login_required
def toggle_vehicle(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    vehicle.is_active = not vehicle.is_active
    if vehicle.is_active:
        vehicle.review_pending = False
    db.session.commit()
    state = "listed" if vehicle.is_active else "hidden"
    flash(f"{vehicle.name} is now {state}.", "success")
    return redirect(url_for("admin.vehicles"))


@bp.route("/vehicles/<int:vehicle_id>/delete", methods=["POST"])
@login_required
def delete_vehicle(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    live = [b for b in vehicle.bookings if b.status in ("pending", "confirmed")]
    if live:
        flash(
            f"{vehicle.name} still has {len(live)} open booking(s). "
            "Cancel or complete those first, or hide the vehicle instead.",
            "error",
        )
        return redirect(url_for("admin.vehicles"))
    name = vehicle.name
    db.session.delete(vehicle)
    db.session.commit()
    flash(f"Deleted {name}.", "success")
    return redirect(url_for("admin.vehicles"))


# --- Bookings --------------------------------------------------------------

@bp.route("/bookings")
@login_required
def bookings():
    status = request.args.get("status", "").strip()
    query = Booking.query
    if status in BOOKING_STATUSES + RIDE_ONLY_STATUSES:
        query = query.filter(Booking.status == status)
    items = query.order_by(Booking.start_date.desc()).all()
    return render_template(
        "admin/bookings.html",
        bookings=items,
        status=status,
        statuses=BOOKING_STATUSES,
        filter_statuses=BOOKING_STATUSES + RIDE_ONLY_STATUSES,
        staff_ride_statuses=STAFF_RIDE_STATUSES,
    )


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@login_required
def update_booking_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get("status")
    if new_status not in BOOKING_STATUSES:
        abort(400)
    if booking.is_on_demand and new_status not in STAFF_RIDE_STATUSES:
        flash(f"{booking.reference} is a ride requested from a driver. Its driver moves "
              f"it on in Driving mode; staff can only complete or cancel it.", "error")
        return redirect(request.referrer or url_for("admin.bookings"))

    # Only a hire holds a car for a range of days. A ride or transfer may not
    # have a car against it at all, so there is nothing here to double-book —
    # and reaching through a null vehicle would just crash.
    holds_a_car = (
        booking.booking_type == RENTAL
        and booking.vehicle is not None
        and new_status in ("pending", "confirmed")
    )
    if holds_a_car and not booking.vehicle.is_available(
        booking.start_date, booking.end_date, ignore_booking_id=booking.id
    ):
        flash(
            f"{booking.vehicle.name} is already committed to another booking "
            "for those dates.",
            "error",
        )
        return redirect(url_for("admin.bookings"))

    if new_status == "completed" and booking.needs_quote:
        # The same rule the operator area enforces: completing is what earns
        # commission, and there is nothing to take a share of until somebody has
        # agreed a fare.
        flash(
            f"Booking {booking.reference} has no fare yet. Set one before "
            f"completing it.",
            "error",
        )
        return redirect(request.referrer or url_for("admin.bookings"))

    booking.status = new_status
    # Commission is earned by completing a booking and given back if that is
    # undone. sync_for does both, so this caller cannot get it half right.
    entry = commission.sync_for(booking)
    try:
        db.session.commit()
    except IntegrityError:
        # Confirming a booking can collide with another confirmed one the same
        # way a customer request can; the database has the final say.
        db.session.rollback()
        subject = booking.vehicle.name if booking.vehicle else "That vehicle"
        flash(
            f"{subject} is already committed to another booking for those dates.",
            "error",
        )
        return redirect(request.referrer or url_for("admin.bookings"))

    if entry is not None:
        flash(
            f"Booking {booking.reference} is now {new_status}. Commission of "
            f"{entry.amount} recorded at {entry.rate_percent}% of the fare.",
            "success",
        )
    else:
        flash(f"Booking {booking.reference} is now {new_status}.", "success")
    return redirect(request.referrer or url_for("admin.bookings"))


# --- Enquiries -------------------------------------------------------------

@bp.route("/enquiries")
@login_required
def enquiries():
    items = Enquiry.query.order_by(Enquiry.created_at.desc()).all()
    return render_template("admin/enquiries.html", enquiries=items)


@bp.route("/enquiries/<int:enquiry_id>/read", methods=["POST"])
@login_required
def toggle_enquiry(enquiry_id):
    enquiry = Enquiry.query.get_or_404(enquiry_id)
    enquiry.is_read = not enquiry.is_read
    db.session.commit()
    return redirect(url_for("admin.enquiries"))


# --- Settings ---------------------------------------------------------------

# Groups whose fields are all edited inline on the live pages; the settings
# screen only carries what has no visible place on the site.
INLINE_ONLY_GROUPS = {"home", "about", "contact", "footer", "rides", "operators"}


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Configuration with no natural home on a public page.

    Page copy is not edited here — staff change that on the pages themselves.
    """
    groups = [group for group in SCHEMA if group.key not in INLINE_ONLY_GROUPS]

    if request.method == "POST":
        group_key = request.form.get("group")
        if group_key not in {group.key for group in groups}:
            abort(400)
        save_settings(request.form, group_key)
        flash(f"{GROUPS[group_key].label} saved.", "success")
        return redirect(url_for("admin.settings"))

    return render_template("admin/settings.html", groups=groups, values=current_settings())


@bp.route("/settings/<group_key>/reset", methods=["POST"])
@login_required
def settings_reset(group_key):
    if group_key not in GROUPS:
        abort(404)
    reset_group(group_key)
    flash(f"{GROUPS[group_key].label} reset to the original wording.", "success")
    return redirect(request.referrer or url_for("admin.settings"))


@bp.route("/checklist")
@login_required
def checklist():
    """What still has to be filled in before the site is shown to customers."""
    settings = current_settings()
    items = outstanding_items(settings)

    # Group them so the page reads as a to-do list per area of the site.
    by_group = {}
    for item in items:
        # Not "items": Jinja would resolve entry.items to the dict method.
        by_group.setdefault(item["group"].key, {"group": item["group"], "rows": []})
        by_group[item["group"].key]["rows"].append(item)

    fleet_size = Vehicle.query.count()
    listed = Vehicle.query.filter_by(is_active=True).count()

    # Things that are not settings but still block going live.
    blockers = []
    if fleet_size == 0:
        blockers.append("No cars have been added yet.")
    elif listed == 0:
        blockers.append("Cars exist but none are listed, so the fleet page is empty.")
    priced = Vehicle.query.filter(Vehicle.daily_rate > 0).count()
    if fleet_size and priced < fleet_size:
        blockers.append(f"{fleet_size - priced} car(s) have no daily rate set.")
    if not settings.get("default_excess"):
        blockers.append("The insurance excess is still 0, so it is not quoted anywhere.")

    # Two things that look finished but are not, and would otherwise be found
    # out by a customer rather than by whoever runs this.
    if not current_app.config.get("ROUTER_URL"):
        blockers.append(
            "No routing provider is configured, so ride distances are not measured "
            "and every journey falls back to a manual quote.")
    sms_problems = sms.configuration_problems(current_app)
    if sms.is_fake():
        blockers.append(
            "Driver sign-in uses the local test text-message transport. No real text "
            "is sent, so drivers outside this computer cannot sign in.")
    elif sms_problems:
        blockers.append(
            "No SMS provider is configured, so drivers cannot join or sign in with "
            "their phone number. " + " ".join(sms_problems))
    owed = commission.outstanding()
    if owed > 0:
        blockers.append(
            f"{owed} of commission is owed by drivers. Nothing is collected online: "
            f"settle up with them directly and record each payment in the commission "
            f"ledger.")

    return render_template(
        "admin/checklist.html",
        live=settings["site_live"],
        groups=list(by_group.values()),
        total=len(items),
        blockers=blockers,
        marker=PLACEHOLDER_MARKER,
        fleet_size=fleet_size,
        listed=listed,
    )


@bp.route("/checklist/publish", methods=["POST"])
@login_required
def checklist_publish():
    """Put the site in front of the public, or take it back down."""
    wanted = request.form.get("live") == "1"
    save_settings({"site_live": wanted})
    if wanted:
        remaining = len(outstanding_items())
        if remaining:
            flash(
                f"The site is live. {remaining} piece(s) of wording still say "
                f"{PLACEHOLDER_MARKER} and customers can now see them.",
                "error",
            )
        else:
            flash("The site is live.", "success")
    else:
        flash("The site is a draft again. Only signed-in staff can see it.", "success")
    return redirect(url_for("admin.checklist"))


# --- Media ------------------------------------------------------------------

@bp.route("/media", methods=["GET", "POST"])
@login_required
def media():
    if request.method == "POST":
        asset, error = save_upload(request.files.get("file"), request.form.get("alt_text"))
        if error:
            flash(error, "error")
        else:
            flash(f"Uploaded {asset.original_name}.", "success")
        return redirect(url_for("admin.media"))

    assets = MediaAsset.query.order_by(MediaAsset.uploaded_at.desc()).all()
    return render_template("admin/media.html", assets=assets)


@bp.route("/media/<int:asset_id>/delete", methods=["POST"])
@login_required
def media_delete(asset_id):
    asset = MediaAsset.query.get_or_404(asset_id)
    error = delete_asset(asset)
    flash(error or f"Deleted {asset.original_name}.", "error" if error else "success")
    return redirect(url_for("admin.media"))


# --- Inline editor API ------------------------------------------------------

# Vehicle columns the inline editor may write, with how each is coerced.
# Anything not listed here cannot be reached from the editor API.
EDITABLE_VEHICLE_FIELDS = {
    "make": str,
    "model": str,
    "description": str,
    "features": str,
    "image": str,
    "daily_rate": float,
    "weekly_rate": float,
    "deposit": float,
    "year": int,
    "seats": int,
    "doors": int,
    "luggage": int,
    "category": "choice",
    "transmission": "choice",
    "fuel": "choice",
}

# Valid values for the fields edited through the inline picker.
VEHICLE_CHOICES = {
    "category": CATEGORIES,
    "transmission": TRANSMISSIONS,
    "fuel": FUELS,
}

# Sensible starting points for a car added from the front end. Everything here
# is obviously provisional, so a half-finished row cannot read as a real offer.
NEW_VEHICLE_DEFAULTS = {
    "make": "New",
    "model": "car",
    "year": date.today().year,
    "category": "Economy",
    "transmission": "Manual",
    "fuel": "Petrol",
    "seats": 5,
    "doors": 5,
    "luggage": 2,
    # The confirmed standard terms, so a new car is right by default and only
    # needs changing when it is not.
    "daily_rate": 10000,
    "deposit": 5000,
    "is_active": False,
}


@bp.route("/api/save", methods=["POST"])
@login_required
def api_save():
    """Apply a batch of inline edits: site settings and per-vehicle fields.

    Everything is validated before anything is written, so a single bad value
    cannot leave half the page saved.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Send an object containing settings and records."}), 400
    setting_changes = payload.get("settings") or {}
    record_changes = payload.get("records") or {}

    if not isinstance(setting_changes, dict) or not isinstance(record_changes, dict):
        return jsonify({"error": "Settings and records must be objects."}), 400

    unknown = [key for key in setting_changes if key not in FIELDS]
    if unknown:
        return jsonify({"error": f"Unknown setting: {unknown[0]}"}), 400

    planned = []
    for reference, raw in record_changes.items():
        parts = reference.split(":")
        if len(parts) != 3 or parts[0] != "vehicle":
            return jsonify({"error": f"Cannot edit \u201c{reference}\u201d here."}), 400

        _, raw_id, column = parts
        caster = EDITABLE_VEHICLE_FIELDS.get(column)
        if caster is None or not raw_id.isdigit():
            return jsonify({"error": f"Cannot edit \u201c{reference}\u201d here."}), 400

        vehicle = db.session.get(Vehicle, int(raw_id))
        if vehicle is None:
            return jsonify({"error": "That vehicle no longer exists."}), 404

        if not isinstance(raw, str):
            return jsonify({"error": "Field values must be text."}), 400
        value = raw.strip()

        if caster is float:
            try:
                value = float(value.replace(",", "").replace("\u2009", ""))
            except ValueError:
                return jsonify({"error": f"\u201c{raw}\u201d is not a number."}), 400
            if not math.isfinite(value) or value < 0:
                return jsonify({"error": "Enter a finite, non-negative price."}), 400
        elif caster is int:
            try:
                value = int(float(value.replace(",", "")))
            except ValueError:
                return jsonify({"error": f"\u201c{raw}\u201d is not a whole number."}), 400
            limits = {"year": (1950, 2100), "seats": (1, 25), "doors": (1, 8),
                      "luggage": (0, 30)}
            low, high = limits[column]
            if not low <= value <= high:
                return jsonify(
                    {"error": f"{column.title()} must be between {low} and {high}."}
                ), 400
        elif caster == "choice":
            allowed = VEHICLE_CHOICES[column]
            match = next((o for o in allowed if o.lower() == value.lower()), None)
            if match is None:
                return jsonify({
                    "error": f"{column.title()} must be one of: {', '.join(allowed)}."
                }), 400
            value = match
        elif column in ("make", "model") and not value:
            return jsonify({"error": "Make and model cannot be empty."}), 400
        else:
            value = value or None

        planned.append((vehicle, column, value))

    try:
        if setting_changes:
            save_settings(setting_changes)
        for vehicle, column, value in planned:
            setattr(vehicle, column, value)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Inline save failed")
        return jsonify({"error": "Could not save those changes."}), 500

    return jsonify({"saved": len(setting_changes) + len(planned)})


@bp.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    asset, error = save_upload(request.files.get("file"))
    if error:
        return jsonify({"error": error}), 400
    return jsonify({
        "path": asset.path,
        "url": media_url(asset.path),
        "id": asset.id,
    })


@bp.route("/api/icons")
@login_required
def api_icons():
    """The icon set offered by the inline icon picker."""
    from flask import render_template_string

    from .settings import ICON_CHOICES

    template = (
        "{% from 'partials/icons.html' import icon_by_name %}{{ icon_by_name(name, 26) }}"
    )
    return jsonify({
        name: render_template_string(template, name=name).strip()
        for name, _label in ICON_CHOICES
    })


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Change the signed-in staff password."""
    user = db.session.get(AdminUser, session["admin_id"])
    if user is None:
        session.clear()
        return redirect(url_for("admin.login"))

    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        errors = []
        if not user.check_password(current):
            errors.append("Your current password is not right.")
        if len(new) < 10:
            errors.append("Choose a new password of at least 10 characters.")
        if new != confirm:
            errors.append("The two new passwords do not match.")
        if new and new == current:
            errors.append("The new password must be different from the old one.")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            user.set_password(new)
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("admin.dashboard"))

    return render_template("admin/account.html", user=user)


@bp.route("/api/vehicle/new", methods=["POST"])
@login_required
def api_vehicle_new():
    """Add a car from the front end, so the whole fleet can be built in place.

    It starts hidden from the public site: a car with no rate and no
    description should not be on the fleet page while it is being filled in.
    """
    vehicle = Vehicle(**NEW_VEHICLE_DEFAULTS)
    db.session.add(vehicle)
    db.session.commit()
    return jsonify({
        "id": vehicle.id,
        "url": url_for("public.vehicle_detail", vehicle_id=vehicle.id),
    })


@bp.route("/api/vehicle/<int:vehicle_id>/delete", methods=["POST"])
@login_required
def api_vehicle_delete(vehicle_id):
    vehicle = db.session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return jsonify({"error": "That vehicle no longer exists."}), 404

    live = [b for b in vehicle.bookings if b.status in ("pending", "confirmed")]
    if live:
        return jsonify({
            "error": f"{vehicle.name} has {len(live)} open booking(s). "
                     "Cancel or complete those first, or just hide the car."
        }), 400

    name = vehicle.name
    db.session.delete(vehicle)
    db.session.commit()
    return jsonify({"deleted": name, "url": url_for("public.fleet")})


@bp.route("/api/vehicle/<int:vehicle_id>/listed", methods=["POST"])
@login_required
def api_vehicle_listed(vehicle_id):
    """Show or hide a car without leaving the page."""
    vehicle = db.session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return jsonify({"error": "That vehicle no longer exists."}), 404
    vehicle.is_active = not vehicle.is_active
    if vehicle.is_active:
        vehicle.review_pending = False
    db.session.commit()
    return jsonify({"listed": vehicle.is_active})


@bp.route("/api/choices")
@login_required
def api_choices():
    """Valid values for the fields the inline editor offers as a picker."""
    return jsonify(VEHICLE_CHOICES)



# --- Commission ledger -------------------------------------------------------

@bp.route("/commission")
@login_required
def commission_ledger():
    """What each driver owes us, and what they have paid.

    Nothing is collected online, so this is the record of money that actually
    changed hands. Rows are never edited or deleted; a mistake is corrected by
    recording the opposite amount with a note.
    """
    drivers = Operator.query.order_by(Operator.name).all()
    statements = [commission.statement(driver) for driver in drivers]
    statements = [row for row in statements
                  if row["earned"] or row["settled"] or row["operator"].is_approved]
    recent = (CommissionSettlement.query
              .order_by(CommissionSettlement.recorded_at.desc(), CommissionSettlement.id.desc())
              .limit(50).all())
    return render_template(
        "admin/commission.html",
        statements=statements,
        recent=recent,
        methods=commission.METHODS,
        earned=commission.totals(),
        settled=commission.settled(),
        outstanding=commission.outstanding(),
    )


@bp.route("/commission/<int:operator_id>/settle", methods=["POST"])
@login_required
def commission_settle(operator_id):
    """Write down a payment a driver has handed over."""
    operator = db.session.get(Operator, operator_id) or abort(404)
    try:
        settlement = commission.record_settlement(
            operator,
            request.form.get("amount"),
            request.form.get("method"),
            reference=request.form.get("reference"),
            note=request.form.get("note"),
            recorded_by=db.session.get(AdminUser, session.get("admin_id")),
        )
    except commission.SettlementError as error:
        flash(str(error), "error")
        return redirect(url_for("admin.commission_ledger"))

    db.session.commit()
    kind = "correction" if settlement.is_correction else "payment"
    flash(f"Recorded a {kind} of {settlement.amount} from {operator.name}. "
          f"They now owe {commission.outstanding(operator)}.", "success")
    return redirect(url_for("admin.commission_ledger"))


# --- Operators --------------------------------------------------------------

@bp.route("/operators")
@login_required
def operators():
    status = (request.args.get("status") or "").strip()
    query = Operator.query
    if status in OPERATOR_STATUSES:
        query = query.filter(Operator.status == status)

    listed = query.order_by(Operator.status, Operator.name).all()
    earned = {
        row[0]: row[1] for row in
        db.session.query(CommissionEntry.operator_id, db.func.sum(CommissionEntry.amount))
        .group_by(CommissionEntry.operator_id).all()
    }
    return render_template(
        "admin/operators.html",
        operators=listed,
        status=status,
        statuses=OPERATOR_STATUSES,
        earned=earned,
        default_rate=commission.default_rate(),
        counts={name: Operator.query.filter_by(status=name).count()
                for name in OPERATOR_STATUSES},
    )


@bp.route("/operators/<int:operator_id>/decision", methods=["POST"])
@login_required
def operator_decision(operator_id):
    """Approve, reject, suspend or reopen an application.

    Approving is the only thing that puts an operator's vehicles and fares in
    front of customers, so nothing they have entered is public until a person
    has looked at it.
    """
    operator = db.session.get(Operator, operator_id) or abort(404)
    decision = request.form.get("status")
    if decision not in OPERATOR_STATUSES:
        abort(400)

    operator.status = decision
    if decision == "approved" and operator.approved_at is None:
        operator.approved_at = datetime.utcnow()
    if decision != "approved":
        # Suspending must actually take effect: the session check reads status,
        # so their next request signs them out.
        operator.approved_at = operator.approved_at if decision == "suspended" else None

    db.session.commit()

    if decision == "approved" and operator.signs_in_by_phone:
        # The usual case: they joined by confirming their number, so approval is
        # all they were waiting for.
        flash(f"{operator.name} is approved and can sign in with their phone number.",
              "success")
    elif decision == "approved" and not operator.password_hash:
        flash(
            f"{operator.name} is approved, but cannot sign in yet: no confirmed phone "
            f"number and no email password. Ask them to join with their phone number, "
            f"or issue a password below.",
            "success",
        )
    else:
        flash(f"{operator.name} is now {decision}.", "success")
    return redirect(request.referrer or url_for("admin.operators"))


@bp.route("/operators/<int:operator_id>/rate", methods=["POST"])
@login_required
def operator_rate(operator_id):
    """Give one operator their own commission rate, or put them back on the default."""
    operator = db.session.get(Operator, operator_id) or abort(404)
    raw = (request.form.get("commission_rate") or "").strip()

    if not raw:
        operator.commission_rate = None
        db.session.commit()
        flash(f"{operator.name} now uses the default rate.", "success")
        return redirect(request.referrer or url_for("admin.operators"))

    try:
        rate = float(raw.replace(",", "").rstrip("%"))
    except ValueError:
        flash("Enter the rate as a number, for example 5.", "error")
        return redirect(request.referrer or url_for("admin.operators"))

    if not 0 <= rate <= 100:
        flash("A commission rate has to be between 0 and 100.", "error")
        return redirect(request.referrer or url_for("admin.operators"))

    operator.commission_rate = rate
    db.session.commit()
    flash(f"{operator.name} is now on {rate}%. Commission already recorded is "
          f"unchanged.", "success")
    return redirect(request.referrer or url_for("admin.operators"))


@bp.route("/operators/<int:operator_id>/access", methods=["POST"])
@login_required
def operator_access(operator_id):
    """Issue a one-off sign-in password for an approved operator.

    Generated here and shown once rather than chosen by staff, so a weak shared
    password never gets set. Pass it to the operator yourself; nothing is
    emailed, because the site sends no email.
    """
    operator = db.session.get(Operator, operator_id) or abort(404)
    if not operator.is_approved:
        flash("Approve the driver before giving them a sign-in.", "error")
        return redirect(request.referrer or url_for("admin.operators"))
    if not operator.email:
        flash(f"{operator.name} has no email address. They sign in with their phone "
              f"number, so they don't need a password.", "error")
        return redirect(request.referrer or url_for("admin.operators"))

    password = secrets.token_urlsafe(12)
    operator.set_password(password)
    db.session.commit()
    flash(
        f"Sign-in for {operator.name} — email {operator.email}, password "
        f"{password} — shown once. Give it to them directly.",
        "success",
    )
    return redirect(request.referrer or url_for("admin.operators"))


@bp.route("/operators/<int:operator_id>/phone/remove", methods=["POST"])
@login_required
def operator_phone_remove(operator_id):
    """Take a phone number off a driver account. Never puts one on.

    For a lost SIM, a number recycled to someone else, or an old unverified
    contact number that blocks the real owner from joining. Staff can only
    remove: a number gets onto an account only by its holder typing back a
    texted code. Nothing is merged and no other account is touched.
    """
    operator = db.session.get(Operator, operator_id) or abort(404)
    which = request.form.get("which")
    if which == "verified" and operator.phone_e164:
        operator.phone_e164 = None
        operator.phone_verified_at = None
        message = (f"Removed the sign-in number from {operator.name}. They can no "
                   f"longer sign in with it until they verify a number again.")
    elif which == "contact" and operator.phone:
        operator.phone = None
        message = f"Removed the unverified contact number from {operator.name}."
    else:
        abort(400)
    db.session.commit()
    current_app.logger.info("Staff removed a %s phone number from driver %s",
                            which, operator.id)
    flash(message, "success")
    return redirect(request.referrer or url_for("admin.operators"))


@bp.route("/dev/text-messages")
@login_required
def dev_text_messages():
    """Codes the local fake transport would have texted. Development only."""
    if not sms.is_fake():
        abort(404)
    return render_template("admin/dev_text_messages.html",
                           messages=sms.read_fake_outbox_file(limit=20))
