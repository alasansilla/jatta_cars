"""Staff area: manage the fleet, bookings and enquiries.

Access is guarded by a simple session login. That is enough for a single-office
operation; move to Flask-Login and per-user accounts if the team grows.
"""
from datetime import date, timedelta
from functools import wraps

import math

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for
)

from .media import delete_asset, save_upload
from .models import (
    CATEGORIES, FUELS, TRANSMISSIONS, AdminUser, Booking, Enquiry, MediaAsset,
    Setting, Vehicle, db
)
from .settings import (
    FIELDS, GROUPS, PLACEHOLDER_MARKER, SCHEMA, current_settings, outstanding_items,
    reset_group, save_settings,
)

bp = Blueprint("admin", __name__)

BOOKING_STATUSES = ["pending", "confirmed", "completed", "cancelled"]


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
        "nav_pending": Booking.query.filter_by(status="pending").count(),
        "nav_unread": Enquiry.query.filter_by(is_read=False).count(),
        "nav_todo": len(outstanding_items()),
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
    items = Vehicle.query.order_by(
        Vehicle.is_active.desc(), Vehicle.make.asc(), Vehicle.model.asc()
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

        year = number("year", "Year", int, minimum=1950)
        daily_rate = number("daily_rate", "Daily rate", float, minimum=0)
        weekly_rate = number("weekly_rate", "Weekly rate", float, required=False, minimum=0)
        deposit = number("deposit", "Deposit", float, minimum=0)
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
            )

        if vehicle is None:
            vehicle = Vehicle()
            db.session.add(vehicle)

        vehicle.make = make
        vehicle.model = model
        vehicle.year = year
        vehicle.category = category
        vehicle.transmission = transmission
        vehicle.fuel = fuel
        vehicle.seats = seats
        vehicle.doors = doors
        vehicle.luggage = luggage or 0
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
                )
            vehicle.image = asset.path
        else:
            vehicle.image = (form.get("image") or "").strip() or None
        vehicle.description = (form.get("description") or "").strip() or None
        vehicle.features = (form.get("features") or "").strip() or None
        vehicle.is_active = form.get("is_active") == "on"

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
    )


@bp.route("/vehicles/<int:vehicle_id>/toggle", methods=["POST"])
@login_required
def toggle_vehicle(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    vehicle.is_active = not vehicle.is_active
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
    if status in BOOKING_STATUSES:
        query = query.filter(Booking.status == status)
    items = query.order_by(Booking.start_date.desc()).all()
    return render_template(
        "admin/bookings.html",
        bookings=items,
        status=status,
        statuses=BOOKING_STATUSES,
    )


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@login_required
def update_booking_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get("status")
    if new_status not in BOOKING_STATUSES:
        abort(400)

    # Confirming must not double-book a vehicle that was freed up and re-let.
    if new_status in ("pending", "confirmed") and not booking.vehicle.is_available(
        booking.start_date, booking.end_date, ignore_booking_id=booking.id
    ):
        flash(
            f"{booking.vehicle.name} is already committed to another booking "
            "for those dates.",
            "error",
        )
        return redirect(url_for("admin.bookings"))

    booking.status = new_status
    db.session.commit()
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
INLINE_ONLY_GROUPS = {"home", "about", "contact", "footer"}


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
    "daily_rate": 0,
    "deposit": 0,
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
        "url": url_for("static", filename=asset.path),
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
    db.session.commit()
    return jsonify({"listed": vehicle.is_active})


@bp.route("/api/choices")
@login_required
def api_choices():
    """Valid values for the fields the inline editor offers as a picker."""
    return jsonify(VEHICLE_CHOICES)
