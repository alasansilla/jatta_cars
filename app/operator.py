"""The signed-in area for an approved transport operator.

Every query here is filtered to the operator holding the session. An operator
must never be able to see another operator's vehicles, fares, bookings or
earnings, so the filter is applied in one place — `_current()` plus `owned()` —
rather than remembered at each call site.

Customer contact details are shown only once a booking is confirmed. Before
that the operator sees the journey and the fare, which is what they need to
decide, but not the person's email and phone.
"""
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, session,
    url_for,
)

from . import commission
from .models import (
    BOOKING_TYPES, JOURNEY_TYPES, RIDE, TRANSFER, Booking, Operator, OperatorFare,
    Vehicle, db,
)
from .settings import current_settings

bp = Blueprint("operator", __name__)

FARE_KINDS = (RIDE, TRANSFER)

# Statuses at which an operator may see who the customer is. Before a booking is
# confirmed there is no reason for them to hold a stranger's contact details.
CONTACT_VISIBLE_AT = ("confirmed", "completed")


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


def _current():
    """The operator this session belongs to, or None.

    A suspended or rejected operator loses access immediately, without anyone
    having to clear their session.
    """
    operator_id = session.get("operator_id")
    if not operator_id:
        return None
    operator = db.session.get(Operator, operator_id)
    if operator is None or not operator.is_approved:
        return None
    return operator


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _current() is None:
            session.pop("operator_id", None)
            flash("Please sign in to continue.", "error")
            return redirect(url_for("operator.login", next=request.path))
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
            session["operator_id"] = operator.id
            session["operator_name"] = operator.name
            target = request.args.get("next")
            if target and target.startswith("/operator") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("operator.dashboard"))

        # Deliberately one message: telling an unapproved applicant that their
        # password was right would confirm the account exists.
        flash("Those details do not match an approved operator account.", "error")
    return render_template("operator/login.html")


@bp.route("/logout")
def logout():
    session.pop("operator_id", None)
    session.pop("operator_name", None)
    flash("Signed out.", "success")
    return redirect(url_for("public.index"))


@bp.route("/")
@login_required
def dashboard():
    operator = _current()
    upcoming = owned(Booking.query).filter(
        Booking.status.in_(("pending", "confirmed"))
    ).order_by(Booking.start_date.asc()).limit(10).all()

    counts = {
        "pending": owned(Booking.query).filter_by(status="pending").count(),
        "confirmed": owned(Booking.query).filter_by(status="confirmed").count(),
        "completed": owned(Booking.query).filter_by(status="completed").count(),
        "fares": OperatorFare.query.filter_by(operator_id=operator.id).count(),
        "vehicles": Vehicle.query.filter_by(operator_id=operator.id).count(),
    }
    return render_template(
        "operator/dashboard.html",
        upcoming=upcoming,
        counts=counts,
        earned=commission.totals(operator),
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
        raw_price = (request.form.get("price") or "").strip()

        if len(title) < 3:
            errors.append("Give the route a name.")
        if not from_location or not to_location:
            errors.append("Enter where the journey starts and ends.")
        if kind not in FARE_KINDS:
            errors.append("Choose whether this is a ride or an airport transfer.")
        try:
            price = float(raw_price.replace(",", ""))
        except ValueError:
            price = -1
        if price <= 0:
            errors.append("Enter the fare you charge for this journey.")

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
                price=price,
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
