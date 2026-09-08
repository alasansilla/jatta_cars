"""Customer-facing pages: fleet, vehicle details, booking and contact."""
from datetime import date, timedelta

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, session, make_response, url_for
)

from sqlalchemy.exc import IntegrityError

from .forms import parse_date, validate_customer, validate_rental_dates
from .models import CATEGORIES, TRANSMISSIONS, Booking, Enquiry, Vehicle, db
from .settings import current_settings

bp = Blueprint("public", __name__)


@bp.before_request
def hold_until_published():
    """Keep the public out until someone says the site is ready.

    Staff signed in still see the real site, so the whole thing can be built and
    checked in place. Everyone else gets a holding page — better than a customer
    finding [TBC] wording or a car priced at zero.
    """
    if current_settings()["site_live"]:
        return None
    if session.get("admin_id"):
        return None
    response = make_response(render_template("holding.html", minimal=True))
    # Nothing here should be cached: it changes the moment the site goes live.
    response.headers["Cache-Control"] = "no-store"
    return response


def _search_filters():
    """Read the fleet filters out of the query string."""
    return {
        "category": request.args.get("category", "").strip(),
        "transmission": request.args.get("transmission", "").strip(),
        "seats": request.args.get("seats", "").strip(),
        "max_price": request.args.get("max_price", "").strip(),
        "pickup": request.args.get("pickup", "").strip(),
        "start": request.args.get("start", "").strip(),
        "end": request.args.get("end", "").strip(),
        "sort": request.args.get("sort", "price_asc").strip(),
    }


@bp.route("/")
def index():
    featured = (
        Vehicle.query.filter_by(is_active=True)
        .order_by(Vehicle.daily_rate.asc())
        .limit(6)
        .all()
    )
    tomorrow = date.today() + timedelta(days=1)
    return render_template(
        "index.html",
        featured=featured,
        categories=CATEGORIES,
        default_start=tomorrow.isoformat(),
        default_end=(tomorrow + timedelta(days=3)).isoformat(),
        stats={"vehicles": Vehicle.query.filter_by(is_active=True).count()},
    )


@bp.route("/fleet")
def fleet():
    filters = _search_filters()
    query = Vehicle.query.filter_by(is_active=True)

    if filters["category"] in CATEGORIES:
        query = query.filter(Vehicle.category == filters["category"])
    if filters["transmission"] in TRANSMISSIONS:
        query = query.filter(Vehicle.transmission == filters["transmission"])
    if filters["seats"].isdigit():
        query = query.filter(Vehicle.seats >= int(filters["seats"]))
    if filters["max_price"].replace(".", "", 1).isdigit():
        query = query.filter(Vehicle.daily_rate <= float(filters["max_price"]))

    sort = filters["sort"]
    if sort == "price_desc":
        query = query.order_by(Vehicle.daily_rate.desc())
    elif sort == "seats_desc":
        query = query.order_by(Vehicle.seats.desc(), Vehicle.daily_rate.asc())
    elif sort == "newest":
        query = query.order_by(Vehicle.year.desc(), Vehicle.daily_rate.asc())
    else:
        query = query.order_by(Vehicle.daily_rate.asc())

    vehicles = query.all()

    # When the visitor gave dates, drop anything already booked for them and
    # work out what each remaining vehicle would cost for that period.
    start, end, date_errors = None, None, []
    days = None
    if filters["start"] or filters["end"]:
        start, end, date_errors = validate_rental_dates(
            filters["start"], filters["end"], current_settings()
        )
        if not date_errors and start and end:
            days = (end - start).days
            vehicles = [v for v in vehicles if v.is_available(start, end)]

    quotes = {v.id: v.quote(days) for v in vehicles} if days else {}

    # Carried onto each vehicle link so the dates survive the click.
    card_query = {
        key: filters[key] for key in ("start", "end", "pickup") if filters[key]
    }

    return render_template(
        "fleet.html",
        vehicles=vehicles,
        filters=filters,
        categories=CATEGORIES,
        transmissions=TRANSMISSIONS,
        date_errors=date_errors,
        days=days,
        quotes=quotes,
        card_query=card_query,
    )


@bp.route("/fleet/<int:vehicle_id>")
def vehicle_detail(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    if not vehicle.is_active:
        abort(404)

    start = request.args.get("start", "")
    end = request.args.get("end", "")
    quote = None
    days = None
    parsed_start, parsed_end = parse_date(start), parse_date(end)
    if parsed_start and parsed_end and parsed_end > parsed_start:
        days = (parsed_end - parsed_start).days
        quote = vehicle.quote(days)

    tomorrow = date.today() + timedelta(days=1)
    similar = (
        Vehicle.query.filter(
            Vehicle.category == vehicle.category,
            Vehicle.id != vehicle.id,
            Vehicle.is_active.is_(True),
        )
        .limit(3)
        .all()
    )

    # A short strip of nearby-priced cars so visitors can flick between options.
    tabs = (
        Vehicle.query.filter(Vehicle.is_active.is_(True))
        .order_by(Vehicle.daily_rate.asc())
        .limit(4)
        .all()
    )
    if vehicle.id not in [item.id for item in tabs]:
        tabs = tabs[:3] + [vehicle]

    pickup = request.args.get("pickup", "").strip()
    locations = current_settings()["locations"]
    if pickup not in locations:
        pickup = locations[0] if locations else ""

    return render_template(
        "vehicle.html",
        vehicle=vehicle,
        start=start or tomorrow.isoformat(),
        end=end or (tomorrow + timedelta(days=3)).isoformat(),
        quote=quote,
        days=days,
        similar=similar,
        tabs=tabs,
        pickup=pickup,
        card_query={"start": start, "end": end, "pickup": pickup},
    )


@bp.route("/fleet/<int:vehicle_id>/book", methods=["POST"])
def book(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    if not vehicle.is_active:
        abort(404)

    settings = current_settings()
    start, end, errors = validate_rental_dates(
        request.form.get("start"), request.form.get("end"), settings
    )
    customer, customer_errors = validate_customer(request.form)
    errors += customer_errors

    pickup = (request.form.get("pickup_location") or "").strip()
    dropoff = (request.form.get("dropoff_location") or "").strip() or pickup
    valid_locations = settings["locations"]
    if pickup not in valid_locations:
        errors.append("Choose a pick-up location.")
    if dropoff not in valid_locations:
        errors.append("Choose a return location.")

    if not errors and not vehicle.is_available(start, end):
        errors.append(
            "Sorry, that vehicle is already booked for those dates. "
            "Try different dates or another vehicle."
        )

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(
            url_for(
                "public.vehicle_detail",
                vehicle_id=vehicle.id,
                start=request.form.get("start", ""),
                end=request.form.get("end", ""),
            )
        )

    booking = Booking(
        reference=Booking.new_reference(),
        vehicle=vehicle,
        customer_name=customer["customer_name"],
        email=customer["email"],
        phone=customer["phone"],
        pickup_location=pickup,
        dropoff_location=dropoff,
        start_date=start,
        end_date=end,
        total_price=vehicle.quote((end - start).days),
        status="pending",
        notes=(request.form.get("notes") or "").strip() or None,
    )
    db.session.add(booking)
    try:
        db.session.commit()
    except IntegrityError:
        # The availability check above is a read, and two requests can pass it
        # at the same moment. On Postgres an exclusion constraint refuses the
        # second write, which lands here rather than double-booking the car.
        db.session.rollback()
        current_app.logger.info(
            "Booking race refused for vehicle %s, %s to %s", vehicle.id, start, end
        )
        flash(
            "Sorry, someone booked that car for those dates a moment before you. "
            "Try different dates or another vehicle.",
            "error",
        )
        return redirect(
            url_for(
                "public.vehicle_detail",
                vehicle_id=vehicle.id,
                start=request.form.get("start", ""),
                end=request.form.get("end", ""),
            )
        )

    session["booking_reference"] = booking.reference
    return redirect(url_for("public.booking_detail", reference=booking.reference))


@bp.route("/booking/<reference>")
def booking_detail(reference):
    reference = reference.upper()
    if session.get("booking_reference") != reference:
        return redirect(url_for("public.booking_lookup"))
    booking = Booking.query.filter_by(reference=reference).first_or_404()
    response = make_response(render_template("booking.html", booking=booking))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@bp.route("/booking", methods=["GET", "POST"])
def booking_lookup():
    """Let a customer find their booking again with reference plus email."""
    if request.method == "POST":
        reference = (request.form.get("reference") or "").strip().upper()
        email = (request.form.get("email") or "").strip().lower()
        booking = Booking.query.filter_by(reference=reference).first()
        if booking and booking.email.lower() == email:
            session["booking_reference"] = booking.reference
            return redirect(url_for("public.booking_detail", reference=booking.reference))
        flash("No booking matches that reference and email address.", "error")
    return render_template("booking_lookup.html")


@bp.route("/about")
def about():
    return render_template(
        "about.html",
        stats={"vehicles": Vehicle.query.filter_by(is_active=True).count()},
    )


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        customer, errors = validate_customer(
            {
                "customer_name": request.form.get("name"),
                "email": request.form.get("email"),
                "phone": request.form.get("phone"),
            }
        )
        message = (request.form.get("message") or "").strip()
        if len(message) < 10:
            errors.append("Please write a slightly longer message.")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            db.session.add(
                Enquiry(
                    name=customer["customer_name"],
                    email=customer["email"],
                    phone=customer["phone"] or None,
                    subject=(request.form.get("subject") or "").strip() or None,
                    message=message,
                )
            )
            db.session.commit()
            flash(current_settings()["contact_success"], "success")
            return redirect(url_for("public.contact"))

    return render_template("contact.html")
