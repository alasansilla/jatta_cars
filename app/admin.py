"""Staff area: manage the fleet, bookings and enquiries.

Access is guarded by a simple session login. That is enough for a single-office
operation; move to Flask-Login and per-user accounts if the team grows.
"""
from datetime import date, timedelta
from functools import wraps

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for
)

from .models import (
    CATEGORIES, FUELS, TRANSMISSIONS, AdminUser, Booking, Enquiry, Vehicle, db
)

bp = Blueprint("admin", __name__)

BOOKING_STATUSES = ["pending", "confirmed", "completed", "cancelled"]


@bp.context_processor
def inject_admin_counts():
    """Badge counts for the staff navigation."""
    if not session.get("admin_id"):
        return {}
    return {
        "nav_pending": Booking.query.filter_by(status="pending").count(),
        "nav_unread": Enquiry.query.filter_by(is_read=False).count(),
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
