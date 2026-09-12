"""Customer-facing pages: fleet, vehicle details, booking and contact."""
from datetime import date, timedelta

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, session, make_response, url_for
)

from sqlalchemy.exc import IntegrityError

from .forms import (
    parse_date, validate_customer, validate_journey, validate_journey_party,
    validate_rental_dates,
)
from .models import (
    CATEGORIES, RIDE, TRANSFER, TRANSMISSIONS, Booking, Enquiry, Operator,
    OperatorFare, Vehicle, db,
)
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



# --- Scheduled rides and airport transfers ----------------------------------

def _published_fares():
    """Fares customers may actually book.

    Only from approved operators, and only ones the operator has listed. An
    operator who is suspended disappears from here without anyone editing a
    fare.
    """
    return (
        OperatorFare.query.join(Operator)
        .filter(OperatorFare.is_active.is_(True), Operator.status == "approved")
        .order_by(OperatorFare.price.asc())
        .all()
    )


@bp.route("/rides")
def rides():
    fares = _published_fares()
    return render_template(
        "rides.html",
        rides=[fare for fare in fares if fare.kind == RIDE],
        transfers=[fare for fare in fares if fare.kind == TRANSFER],
        total=len(fares),
    )


@bp.route("/rides/<int:fare_id>")
def ride_detail(fare_id):
    fare = OperatorFare.query.get_or_404(fare_id)
    if not fare.is_bookable:
        abort(404)
    tomorrow = date.today() + timedelta(days=1)
    return render_template("ride.html", fare=fare, default_date=tomorrow.isoformat())


@bp.route("/rides/<int:fare_id>/request", methods=["POST"])
def request_ride(fare_id):
    fare = OperatorFare.query.get_or_404(fare_id)
    if not fare.is_bookable:
        abort(404)

    settings = current_settings()
    pickup_at, errors = validate_journey(request.form, settings)
    customer, customer_errors = validate_customer(request.form)
    party, party_errors = validate_journey_party(request.form, fare)
    errors = errors + customer_errors + party_errors

    pickup_address = (request.form.get("pickup_address") or "").strip()
    dropoff_address = (request.form.get("dropoff_address") or "").strip()
    if len(pickup_address) < 3:
        errors.append("Tell the driver where to collect you.")
    if len(dropoff_address) < 3:
        errors.append("Tell the driver where you are going.")

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("public.ride_detail", fare_id=fare.id))

    day = pickup_at.date()
    booking = Booking(
        reference=Booking.new_reference(),
        booking_type=fare.kind,
        operator_id=fare.operator_id,
        fare_id=fare.id,
        customer_name=customer["customer_name"],
        email=customer["email"],
        phone=customer["phone"],
        pickup_location=fare.from_location,
        dropoff_location=fare.to_location,
        pickup_address=pickup_address,
        dropoff_address=dropoff_address,
        pickup_at=pickup_at,
        passengers=party["passengers"],
        luggage_count=party["luggage_count"],
        flight_number=(request.form.get("flight_number") or "").strip() or None,
        # A journey happens on one day. Both dates are set so the column stays
        # non-null; the overlap constraint only applies to hires, so this can
        # never block another ride on the same car.
        start_date=day,
        end_date=day,
        total_price=fare.price,
        # The marketplace does not set a deposit on a journey, and will not
        # invent one. If an operator takes one it is their arrangement.
        deposit_amount=0,
        status="pending",
        notes=(request.form.get("notes") or "").strip() or None,
    )
    db.session.add(booking)
    db.session.commit()

    session["booking_reference"] = booking.reference
    return redirect(url_for("public.booking_detail", reference=booking.reference))


# --- Operators --------------------------------------------------------------

@bp.route("/operators")
def operators():
    return render_template(
        "operators.html",
        approved=Operator.query.filter_by(status="approved")
        .order_by(Operator.name).all(),
        commission_rate=current_settings()["commission_rate"],
    )


@bp.route("/operators/apply", methods=["POST"])
def operator_apply():
    """Take an application. It lists nobody until a person approves it."""
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    contact_name = (request.form.get("contact_name") or "").strip()

    errors = []
    if len(name) < 2:
        errors.append("Enter your business name.")
    contact, contact_errors = validate_customer({
        "customer_name": contact_name or name, "email": email, "phone": phone,
    })
    errors += contact_errors

    if not errors and Operator.query.filter(
            db.func.lower(Operator.email) == email).first():
        errors.append("We already have an application from that email address. "
                      "Get in touch if you have not heard back.")

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("public.operators") + "#apply")

    db.session.add(Operator(
        name=name,
        slug=Operator.make_slug(name),
        contact_name=contact_name or None,
        email=contact["email"],
        phone=contact["phone"] or None,
        service_area=(request.form.get("service_area") or "").strip() or None,
        notes=(request.form.get("about") or "").strip() or None,
        status="pending",
    ))
    db.session.commit()

    flash("Thanks — your application is with us. We review every one before "
          "listing anybody, and will be in touch.", "success")
    return redirect(url_for("public.operators"))
