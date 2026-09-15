"""Customer-facing pages: fleet, vehicle details, booking and contact."""
import json
import time
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, make_response, redirect,
    render_template, request, session, url_for,
)

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from . import routing
from .forms import (
    parse_date, validate_customer, validate_journey, validate_journey_party,
    validate_rental_dates,
)
from .models import (
    CATEGORIES, MANUAL_QUOTE, RENTAL, RIDE, TRANSFER, TRANSMISSIONS, Booking, Enquiry,
    Operator, OperatorFare, Vehicle, db,
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


def bookable_vehicles():
    """Cars a customer may see and book: listed, and not belonging to a driver
    who is unapproved or suspended. One query, so no page forgets the second half."""
    return (Vehicle.query.outerjoin(Operator, Vehicle.operator_id == Operator.id)
            .filter(Vehicle.is_active.is_(True), Vehicle.service_mode.in_(("rental", "both")),
                    or_(Vehicle.operator_id.is_(None), Operator.status == "approved")))


@bp.route("/")
def index():
    featured = (
        bookable_vehicles()
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
        stats={"vehicles": bookable_vehicles().count()},
    )


@bp.route("/fleet")
def fleet():
    filters = _search_filters()
    query = bookable_vehicles()

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
    vehicle = db.session.get(Vehicle, vehicle_id) or abort(404)
    if not vehicle.is_bookable:
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
        bookable_vehicles().filter(
            Vehicle.category == vehicle.category,
            Vehicle.id != vehicle.id,
        )
        .limit(3)
        .all()
    )

    # A short strip of nearby-priced cars so visitors can flick between options.
    tabs = (
        bookable_vehicles()
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
    vehicle = db.session.get(Vehicle, vehicle_id) or abort(404)
    if not vehicle.is_bookable:
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
        booking_type=RENTAL,
        vehicle=vehicle,
        # The driver who rents the car out. Their commission rate applies, the
        # booking appears in their account, and a review lands on their profile.
        operator_id=vehicle.operator_id,
        customer_name=customer["customer_name"],
        email=customer["email"],
        phone=customer["phone"],
        pickup_location=pickup,
        dropoff_location=dropoff,
        start_date=start,
        end_date=end,
        total_price=vehicle.quote((end - start).days),
        # The deposit as it stands today, kept apart from the fare so commission
        # never touches it even if the car's deposit is changed later.
        deposit_amount=vehicle.deposit or 0,
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
    if booking.is_on_demand:
        return redirect(url_for("dispatch.track", reference=reference))
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
        stats={"vehicles": bookable_vehicles().count()},
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
    return render_template(
        "ride.html",
        fare=fare,
        default_date=tomorrow.isoformat(),
        map_settings=routing.map_settings(),
    )


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

    # Coordinates and route come from the page, which got them from this
    # application's own lookup endpoints. They are treated as a convenience, not
    # as truth: the fare is recomputed here from the stored distance, so editing
    # the form cannot talk the price down.
    pickup_lat = _coordinate(request.form.get("pickup_lat"), 90)
    pickup_lng = _coordinate(request.form.get("pickup_lng"), 180)
    dropoff_lat = _coordinate(request.form.get("dropoff_lat"), 90)
    dropoff_lng = _coordinate(request.form.get("dropoff_lng"), 180)

    distance_m = duration_s = route_provider = None
    if None not in (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) \
            and routing.routing_available():
        try:
            leg = routing.route((pickup_lat, pickup_lng), (dropoff_lat, dropoff_lng))
        except routing.RoutingUnavailable as error:
            current_app.logger.warning("Routing unavailable at submit: %s", error)
            leg = None
        if leg is not None:
            distance_m, duration_s = leg.distance_m, leg.duration_s
            route_provider = leg.provider

    amount, basis = fare.quote(distance_m)

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
        pickup_lat=pickup_lat,
        pickup_lng=pickup_lng,
        dropoff_lat=dropoff_lat,
        dropoff_lng=dropoff_lng,
        route_distance_m=distance_m,
        route_duration_s=duration_s,
        route_provider=route_provider,
        route_meta=json.dumps({
            "measured_at": datetime.utcnow().isoformat(timespec="seconds"),
            "pricing_model": fare.pricing_model,
        }) if distance_m is not None else None,
        # None when nothing could price it. The pages then say the operator
        # will confirm, rather than showing a number nobody stands behind.
        total_price=amount,
        quote_basis=basis,
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


# --- Becoming a driver -------------------------------------------------------

@bp.route("/operators")
def operators():
    """The old "list your service" page. Joining is now by phone number."""
    return redirect(url_for("driver_auth.join"), code=301)


@bp.route("/operators/apply", methods=["POST"])
def operator_apply():
    """The old email application form. Nothing is created from it any more:
    a driver joins by proving their phone number, so an application cannot be
    made in someone else's name."""
    flash("Joining is now done with your phone number.", "success")
    return redirect(url_for("driver_auth.join"))



# --- Map lookups ------------------------------------------------------------
#
# The browser asks this application, and this application asks the provider.
# That keeps any API key on the server, keeps the visitor's IP address away from
# a third party, and means the provider can be swapped without touching a page.

def _within_rate_limit(bucket, limit=40, window=60):
    """A light per-session cap, so this cannot be used as a free open proxy.

    Without it, a public geocoding endpoint is an invitation to burn through
    somebody's quota — or get their provider account suspended.
    """
    now = time.time()
    recent = [stamp for stamp in session.get(bucket, []) if now - stamp < window]
    if len(recent) >= limit:
        return False
    recent.append(now)
    session[bucket] = recent
    return True


@bp.route("/api/geocode")
def api_geocode():
    """Suggest places for typed text. Never fails the page; just returns none."""
    query = (request.args.get("q") or "").strip()[:200]
    available = routing.geocoding_available()

    if not available or len(query) < 3:
        return jsonify({"available": available, "places": []})
    if not _within_rate_limit("_geocode_hits"):
        return jsonify({"available": True, "places": [], "error": "too_many"}), 429

    try:
        places = routing.geocode(query)
    except routing.RoutingUnavailable as error:
        # The address is the customer's own; only the failure is recorded.
        current_app.logger.warning("Geocoding unavailable: %s", error)
        return jsonify({"available": True, "places": [], "error": "lookup_failed"})

    return jsonify({"available": True, "places": [place.as_dict() for place in places]})


@bp.route("/api/reverse-geocode")
def api_reverse_geocode():
    """A readable address for a point the traveller chose. Never fails the page.

    Returns {"available": bool, "place": {...} | null}. The coordinates come from
    the traveller's own tap or location, so they are not logged.
    """
    lat = _coordinate(request.args.get("lat"), 90)
    lng = _coordinate(request.args.get("lng"), 180)
    available = routing.reverse_geocoding_available()
    if lat is None or lng is None:
        return jsonify({"available": available, "place": None, "error": "bad_coordinates"}), 400
    if not available:
        return jsonify({"available": False, "place": None})
    if not _within_rate_limit("_reverse_geocode_hits", limit=30):
        return jsonify({"available": True, "place": None, "error": "too_many"}), 429
    try:
        place = routing.reverse_geocode(lat, lng)
    except routing.RoutingUnavailable as error:
        current_app.logger.warning("Reverse geocoding unavailable: %s", type(error).__name__)
        return jsonify({"available": True, "place": None, "error": "lookup_failed"})
    return jsonify({"available": True, "place": place.as_dict() if place else None})


def _coordinate(raw, limit):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if -limit <= value <= limit else None


@bp.route("/api/route")
def api_route():
    """Distance, duration and the resulting fare for one journey.

    Returns a quote of None when the fare cannot be worked out, which the page
    turns into "the operator will confirm" rather than a number.
    """
    fare = OperatorFare.query.get(request.args.get("fare_id", type=int))
    if fare is None or not fare.is_bookable:
        abort(404)

    origin = (_coordinate(request.args.get("from_lat"), 90),
              _coordinate(request.args.get("from_lng"), 180))
    destination = (_coordinate(request.args.get("to_lat"), 90),
                   _coordinate(request.args.get("to_lng"), 180))

    if None in origin or None in destination:
        return jsonify({"available": routing.routing_available(), "route": None,
                        "quote": None, "basis": MANUAL_QUOTE,
                        "error": "bad_coordinates"}), 400

    if not routing.routing_available():
        amount, basis = fare.quote(None)
        return jsonify({"available": False, "route": None,
                        "quote": float(amount) if amount is not None else None,
                        "basis": basis})

    if not _within_rate_limit("_route_hits"):
        return jsonify({"available": True, "route": None, "quote": None,
                        "basis": MANUAL_QUOTE, "error": "too_many"}), 429

    try:
        leg = routing.route(origin, destination)
    except routing.RoutingUnavailable as error:
        current_app.logger.warning("Routing unavailable: %s", error)
        amount, basis = fare.quote(None)
        return jsonify({"available": True, "route": None,
                        "quote": float(amount) if amount is not None else None,
                        "basis": basis, "error": "route_failed"})

    amount, basis = fare.quote(leg.distance_m)
    return jsonify({
        "available": True,
        "route": leg.as_dict(),
        "quote": float(amount) if amount is not None else None,
        "basis": basis,
    })
