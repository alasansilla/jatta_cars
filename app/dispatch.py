"""On-demand rides: the customer chooses a driver, and that driver answers.

The rules this module keeps:

* **The customer chooses.** Every trip is requested from one driver the
  customer picked, at that driver's own published price. If the driver says no,
  does not answer in time, or goes silent after accepting, the trip waits for
  the customer to pick someone else. Nobody is ever put in the chosen driver's
  place without the customer choosing them.
* **Each driver's own fare.** The price is worked out on the server from the
  chosen driver's fare and the measured road distance. The page only echoes the
  price it was shown, and a mismatch means "look again", never "charge it".
* **Strict choices.** A missing or malformed selection is refused. Nothing
  falls back to the cheapest driver or to any default.
* **One driver, one trip.** Reserving a driver is a single guarded UPDATE that
  must match exactly one row, so two customers cannot both get the same driver,
  and a driver can never hold two trips.
* **Location only on an accepted trip.** Being online is a heartbeat with no
  position in it. A driver's position is accepted, stored and shown only while
  they hold an accepted trip, to that trip's customer, and is cleared the moment
  the trip ends in any way. A driver whose heartbeat is more than two minutes
  old is offline, and a customer is never shown a position more than 45
  seconds old.

Customers are identified by the booking reference held in their own session.
Drivers must be signed in and approved, and can only act on the trip they hold.
"""
import math
import re
import secrets
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from . import commission, reviews, routing
from .forms import validate_customer
from .models import (
    RIDE_ACTIVE, RIDE_REOPENABLE, Booking, BookingReview, DriverState, Operator,
    OperatorFare, Vehicle, db,
)
from .operator import _current, login_required
from .phone import pretty
from .public import _within_rate_limit, hold_until_published
from .settings import current_settings

bp = Blueprint("dispatch", __name__)

FRESH_FIX = timedelta(minutes=2)      # older than this: not offered, not reservable
SHOW_FIX = timedelta(seconds=45)      # older than this: not shown to the customer
DRIVER_SILENT = timedelta(minutes=3)  # an accepted driver this quiet can be replaced
QUOTE_LIFETIME = timedelta(minutes=3)
MAX_PASSENGERS = 8

# Kept for older imports: the statuses in which a driver is on a trip.
ACTIVE = RIDE_ACTIVE

CANCELLABLE = ("pending", "accepted", "confirmed", "arriving") + RIDE_REOPENABLE
DRIVER_CAN_DECLINE = ("pending", "accepted", "confirmed", "arriving")
NEXT_STAGE = {"accepted": "arriving", "confirmed": "arriving",
              "arriving": "in_progress", "in_progress": "completed"}
STAGE_LABELS = {"arriving": "I'm on my way", "in_progress": "Start the trip",
                "completed": "Finish the trip"}

Choice = namedtuple("Choice", "amount fare state vehicle")


@bp.before_request
def guard():
    # Driving mode is for drivers and has to work before the public site opens.
    if not request.path.startswith("/operator/"):
        held = hold_until_published()
        if held is not None:
            return held
    if request.method == "POST":
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not secrets.compare_digest(str(supplied), str(expected)):
            if request.form and not request.is_json:
                flash("This page had expired. Please try again.", "error")
                return redirect(request.path)
            return jsonify(error="This page has expired. Refresh it and try again."), 400
    return None


@bp.after_request
def private(response):
    response.headers["Cache-Control"] = "no-store"
    return response


# --- reading what the page sent ------------------------------------------------

def payload():
    """The request body as a dict. A JSON list or string is treated as empty."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.is_json:
        return {}
    return request.form


def _whole(value, low=1, high=2_147_483_647):
    """A whole number from JSON or a form, or None. True is not 1; 2.5 is not 2."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\s*[0-9]{1,10}\s*", value):
        number = int(value)
    else:
        return None
    return number if low <= number <= high else None


def _amount(value):
    """A price to the cent, or None. Rejects NaN, infinities, exponents and booleans."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        text = repr(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", text):
        return None
    return Decimal(text).quantize(Decimal("0.01"))


def _point(value, limit):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not -limit <= number <= limit:
        return None
    return number


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _money_text(amount):
    return f"{current_settings()['currency']}{Decimal(amount):,.2f}"


# --- which drivers can take a trip -----------------------------------------------

def offers(distance_m, passengers, now=None, exclude_operator_id=None):
    """Every driver who could take this trip right now, priced by their own fare.

    Approved, online (a heartbeat in the last two minutes), holding no trip, driving their own
    active car with enough seats, and publishing an active per-kilometre ride
    fare. Cheapest first; the customer still chooses.
    """
    now = now or datetime.utcnow()
    query = (
        db.session.query(OperatorFare, DriverState, Vehicle)
        .join(DriverState, DriverState.operator_id == OperatorFare.operator_id)
        .join(Operator, Operator.id == OperatorFare.operator_id)
        .join(Vehicle, Vehicle.id == DriverState.vehicle_id)
        .filter(
            Operator.status == "approved",
            DriverState.available.is_(True),
            DriverState.active_booking_id.is_(None),
            DriverState.updated_at >= now - FRESH_FIX,
            Vehicle.is_active.is_(True),
            Vehicle.service_mode.in_(("taxi", "both")),
            Vehicle.operator_id == OperatorFare.operator_id,
            Vehicle.seats >= passengers,
            OperatorFare.kind == "ride",
            OperatorFare.is_active.is_(True),
            OperatorFare.pricing_model == "distance",
            or_(OperatorFare.seats.is_(None), OperatorFare.seats >= passengers),
        )
    )
    if exclude_operator_id is not None:
        query = query.filter(OperatorFare.operator_id != exclude_operator_id)

    choices = []
    for fare, state, vehicle in query.all():
        amount, _basis = fare.quote(distance_m)
        if amount is not None and amount > 0:
            choices.append(Choice(amount, fare, state, vehicle))
    return sorted(choices, key=lambda choice: (choice.amount, choice.fare.id))


def _card(choice, preferred_id=None):
    driver = choice.fare.operator
    car = choice.vehicle
    rated = reviews.journey_summary(driver.id)
    return {
        "fare_id": choice.fare.id,
        "vehicle_id": car.id,
        "driver_id": driver.id,
        "driver": driver.display_name,
        "fare_title": choice.fare.title,
        "vehicle": f"{car.name} · {car.year}",
        "seats": car.seats,
        "price": float(choice.amount),
        "price_text": _money_text(choice.amount),
        "rating": rated["rating"],
        "review_count": rated["review_count"],
        "profile": url_for("dispatch.driver_profile", operator_id=driver.id),
        "preferred": preferred_id is not None and driver.id == preferred_id,
    }


def _cards(choices, preferred_id=None):
    cards = [_card(choice, preferred_id) for choice in choices]
    # Someone the customer asked for by name goes first; otherwise cheapest first.
    cards.sort(key=lambda card: not card["preferred"])
    return cards


def reserve(booking_id, choice, now):
    """Hold the chosen driver for this trip. True only if this call got them."""
    won = db.session.execute(update(DriverState).where(
        DriverState.operator_id == choice.fare.operator_id,
        DriverState.vehicle_id == choice.vehicle.id,
        DriverState.available.is_(True),
        DriverState.active_booking_id.is_(None),
        DriverState.updated_at >= now - FRESH_FIX,
    ).values(active_booking_id=booking_id, available=False))
    return won.rowcount == 1


def release(booking_id):
    """Let go of whichever driver holds this trip, and forget where they were.

    They go back online on their next heartbeat. Their position belonged to this
    trip only, so it goes with it.
    """
    db.session.execute(update(DriverState).where(
        DriverState.active_booking_id == booking_id,
    ).values(active_booking_id=None, available=False, lat=None, lng=None, location_at=None))


def _accept_window():
    return timedelta(seconds=current_app.config.get("DRIVER_ACCEPT_SECONDS", 120))


def settle(booking, now=None):
    """Apply what time alone decides: a request nobody answered expires."""
    if booking.status != "pending" or not booking.is_on_demand or booking.requested_at is None:
        return booking
    now = now or datetime.utcnow()
    if booking.requested_at > now - _accept_window():
        return booking
    expired = db.session.execute(update(Booking).where(
        Booking.id == booking.id,
        Booking.status == "pending",
        Booking.requested_at <= now - _accept_window(),
    ).values(status="expired"))
    if expired.rowcount == 1:
        release(booking.id)
    db.session.commit()
    db.session.refresh(booking)
    return booking


def _silent(booking, state, now):
    """An accepted driver whose Driving mode has stopped sending heartbeats."""
    holding = state is not None and state.active_booking_id == booking.id
    return not (holding and state.updated_at is not None
                and state.updated_at >= now - DRIVER_SILENT)


def _location_state(state, holding, now):
    """"live", "stale", "not_shared" or "offline", for the trip being looked at."""
    if not holding:
        return "not_shared"
    if state.updated_at is None or state.updated_at < now - FRESH_FIX:
        return "offline"
    if state.lat is None or state.lng is None or state.location_at is None:
        return "not_shared"
    if state.location_at < now - SHOW_FIX:
        return "stale"
    return "live"


# Road routes for the live trip map. Asking the routing provider on every poll
# would spend its quota for nothing, so answers are kept briefly per process:
# the pickup-to-destination route for the trip, and the route from the driver
# to the pickup per rounded driver position.
_ROUTE_CACHE = {}
_ROUTE_LOCK = threading.Lock()
TRIP_ROUTE_SECONDS = 600
APPROACH_ROUTE_SECONDS = 60


def _cached_route(key, seconds, origin, destination):
    now = time.monotonic()
    with _ROUTE_LOCK:
        hit = _ROUTE_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        leg = routing.route(origin, destination)
    except routing.RoutingUnavailable:
        leg = None
    value = None
    if leg is not None:
        value = {"geometry": leg.geometry if leg.geometry_known else [],
                 "distance_km": leg.distance_km, "duration_minutes": leg.duration_minutes}
    with _ROUTE_LOCK:
        if len(_ROUTE_CACHE) > 2000:
            _ROUTE_CACHE.clear()
        _ROUTE_CACHE[key] = (now + (seconds if value else 30), value)
    return value


def trip_routes(booking, state, now):
    """The routes the live map draws for an accepted trip. Never raises."""
    empty = {"available": routing.routing_available(), "trip": None, "to_pickup": None}
    if booking.status not in RIDE_ACTIVE or not routing.routing_available():
        return empty
    if None in (booking.pickup_lat, booking.pickup_lng, booking.dropoff_lat, booking.dropoff_lng):
        return empty
    pickup = (float(booking.pickup_lat), float(booking.pickup_lng))
    dropoff = (float(booking.dropoff_lat), float(booking.dropoff_lng))
    result = dict(empty)
    result["trip"] = _cached_route(("trip", booking.id, pickup, dropoff), TRIP_ROUTE_SECONDS,
                                   pickup, dropoff)
    holding = state is not None and state.active_booking_id == booking.id
    if booking.status in ("accepted", "confirmed", "arriving") \
            and _location_state(state, holding, now) == "live":
        here = (round(state.lat, 3), round(state.lng, 3))
        result["to_pickup"] = _cached_route(("approach", booking.id, here, pickup),
                                            APPROACH_ROUTE_SECONDS, here, pickup)
    return result


def can_choose_again(booking, now=None):
    now = now or datetime.utcnow()
    if booking.status in RIDE_REOPENABLE:
        return True
    if booking.status in ("accepted", "confirmed", "arriving"):
        state = db.session.get(DriverState, booking.operator_id) if booking.operator_id else None
        return _silent(booking, state, now)
    return False


def _excluded_driver(booking):
    """A driver who said no, or went silent, is not offered for the same trip again."""
    if booking.status == "expired":
        return None
    return booking.operator_id


# --- the customer's side ----------------------------------------------------------

@bp.get("/ride")
def ride():
    preferred = _whole(request.args.get("driver"))
    preferred_driver = None
    if preferred is not None:
        preferred_driver = Operator.query.filter_by(id=preferred, status="approved").first()
    return render_template("dispatch/ride.html", map_settings=routing.map_settings(),
                           preferred_driver=preferred_driver)


@bp.post("/ride/estimate")
def estimate():
    if not _within_rate_limit("_dispatch_quote", 15):
        return jsonify(error="Please wait a moment before checking again."), 429
    data = payload()
    passengers = _whole(data.get("passengers", 1), 1, MAX_PASSENGERS)
    if passengers is None:
        return jsonify(error=f"Choose between 1 and {MAX_PASSENGERS} passengers."), 400

    points = [_point(data.get("pickup_lat"), 90), _point(data.get("pickup_lng"), 180),
              _point(data.get("dropoff_lat"), 90), _point(data.get("dropoff_lng"), 180)]
    if None in points:
        return jsonify(error="Choose your pickup and destination from the search results."), 400

    session.pop("ride_quote", None)
    if not routing.routing_available():
        return jsonify(route=None, choices=[], quote=None,
                       error="Fares can't be worked out right now because route "
                             "measuring is switched off. You can book a scheduled "
                             "journey instead.")
    try:
        leg = routing.route(points[:2], points[2:])
    except routing.RoutingUnavailable:
        leg = None
    if leg is None:
        return jsonify(route=None, choices=[], quote=None,
                       error="We couldn't measure that route. Please try again.")

    preferred_id = _whole(data.get("driver"))
    choices = offers(leg.distance_m, passengers)
    note = None
    if preferred_id is not None and not any(c.fare.operator_id == preferred_id for c in choices):
        wanted = Operator.query.filter_by(id=preferred_id, status="approved").first()
        if wanted is not None:
            note = (f"{wanted.display_name} can't take a ride right now. "
                    f"You can choose another driver below.")

    if not choices:
        return jsonify(route=leg.as_dict(), choices=[], quote=None, note=note,
                       error="No driver is free for this trip right now. Please try "
                             "again in a few minutes, or book a scheduled journey.")

    token = secrets.token_urlsafe(24)
    session["ride_quote"] = {
        "token": token, "points": points, "distance": leg.distance_m,
        "duration": leg.duration_s, "provider": leg.provider, "passengers": passengers,
        "expires": (datetime.utcnow() + QUOTE_LIFETIME).timestamp(),
    }
    cards = _cards(choices, preferred_id)
    return jsonify(route=leg.as_dict(), choices=cards, token=token, note=note,
                   quote=min(card["price"] for card in cards),
                   currency=current_settings()["currency"],
                   message="Choose your driver. We check they are still free when you send the request.")


def _choose_again_response(message, quote, choices, status=409):
    """Refused: show the drivers who are free now, and keep the estimate alive."""
    quote["expires"] = (datetime.utcnow() + QUOTE_LIFETIME).timestamp()
    session["ride_quote"] = quote
    return jsonify(error=message, choices=_cards(choices), token=quote["token"],
                   quote=min((float(c.amount) for c in choices), default=None)), status


@bp.post("/ride/request")
def request_ride():
    data = payload()
    quote = session.get("ride_quote") or {}
    token = data.get("token")
    if not isinstance(token, str) or not quote.get("token") \
            or not secrets.compare_digest(token, str(quote["token"])) \
            or quote.get("expires", 0) < datetime.utcnow().timestamp():
        return jsonify(error="Your estimate has run out. Please check the fare again."), 400

    fare_id = _whole(data.get("fare_id"))
    vehicle_id = _whole(data.get("vehicle_id"))
    expected = _amount(data.get("expected_price"))
    if fare_id is None or vehicle_id is None or expected is None:
        return jsonify(error="Choose a driver first."), 400

    customer, errors = validate_customer({
        "customer_name": _text(data.get("customer_name"), 120),
        "email": _text(data.get("email"), 160),
        "phone": _text(data.get("phone"), 40),
    })
    pickup = _text(data.get("pickup_address"), 240)
    dropoff = _text(data.get("dropoff_address"), 240)
    if len(pickup) < 3 or len(dropoff) < 3:
        errors.append("Enter your pickup and destination.")
    if errors:
        return jsonify(error=" ".join(errors)), 400

    # One open ride per browser. A second request would reserve a second driver
    # for a customer who can only ride with one.
    current = session.get("booking_reference")
    if current:
        open_trip = Booking.query.filter_by(reference=current).first()
        if open_trip is not None and open_trip.is_on_demand and \
                open_trip.status in ("pending",) + RIDE_ACTIVE + RIDE_REOPENABLE:
            return jsonify(error="You already have a ride request open.",
                           url=url_for("dispatch.track", reference=open_trip.reference)), 409

    choices = offers(quote["distance"], quote["passengers"])
    chosen = next((c for c in choices if c.fare.id == fare_id and c.vehicle.id == vehicle_id), None)
    if chosen is None:
        return _choose_again_response(
            "That driver is no longer free. Please choose another driver.", quote, choices)
    if chosen.amount != expected:
        return _choose_again_response(
            f"{chosen.fare.operator.display_name}'s price for this trip is now "
            f"{_money_text(chosen.amount)}. Please check it and choose again.", quote, choices)

    now = datetime.utcnow()
    points = quote["points"]
    trip = Booking(
        reference=Booking.new_reference(), booking_type="ride",
        request_token=quote["token"], requested_at=now,
        operator_id=chosen.fare.operator_id, fare_id=chosen.fare.id, vehicle_id=chosen.vehicle.id,
        customer_name=customer["customer_name"], email=customer["email"],
        phone=customer["phone"] or None,
        pickup_location=pickup[:120], dropoff_location=dropoff[:120],
        pickup_address=pickup, dropoff_address=dropoff,
        pickup_at=now, start_date=now.date(), end_date=now.date(),
        passengers=quote["passengers"],
        pickup_lat=points[0], pickup_lng=points[1], dropoff_lat=points[2], dropoff_lng=points[3],
        route_distance_m=quote["distance"], route_duration_s=quote["duration"],
        route_provider=quote["provider"],
        total_price=chosen.amount, quote_basis="distance", deposit_amount=0, status="pending",
    )
    db.session.add(trip)
    try:
        db.session.flush()
    except IntegrityError:
        # The same estimate already became a trip: a double tap, or two tabs.
        db.session.rollback()
        same = Booking.query.filter_by(request_token=quote["token"]).first()
        if same is None:
            raise
        session.pop("ride_quote", None)
        session["booking_reference"] = same.reference
        return jsonify(url=url_for("dispatch.track", reference=same.reference)), 200

    if not reserve(trip.id, chosen, now):
        db.session.rollback()
        return _choose_again_response(
            "That driver just became busy. Please choose another driver.",
            quote, offers(quote["distance"], quote["passengers"]))
    db.session.commit()

    session.pop("ride_quote", None)
    session["booking_reference"] = trip.reference
    return jsonify(url=url_for("dispatch.track", reference=trip.reference)), 201


def customer_booking(reference):
    reference = str(reference or "").upper()[:12]
    if session.get("booking_reference") != reference:
        abort(404)
    booking = Booking.query.filter_by(reference=reference, booking_type="ride").first()
    if booking is None or not booking.is_on_demand:
        abort(404)
    return booking


@bp.get("/ride/track/<reference>")
def track(reference):
    booking = settle(customer_booking(reference))
    return render_template("dispatch/track.html", booking=booking,
                           map_settings=routing.map_settings())


@bp.get("/ride/status/<reference>")
def status(reference):
    booking = settle(customer_booking(reference))
    now = datetime.utcnow()
    driver_account = booking.operator
    state = db.session.get(DriverState, booking.operator_id) if booking.operator_id else None

    chosen = None
    if driver_account is not None and booking.status != "cancelled":
        chosen = {"name": driver_account.display_name,
                  "vehicle": booking.vehicle.name if booking.vehicle else None,
                  "profile": url_for("dispatch.driver_profile", operator_id=driver_account.id)}

    # Contact details and position only once the driver has accepted, and only
    # while this driver is actually holding this trip.
    driver = None
    if booking.status in RIDE_ACTIVE and driver_account is not None:
        driver = {"name": driver_account.display_name,
                  "phone": pretty(driver_account.phone_e164) or driver_account.phone,
                  "vehicle": booking.vehicle.name if booking.vehicle else None}
        holding = state is not None and state.active_booking_id == booking.id
        driver["online"] = bool(holding and state.updated_at is not None
                                and state.updated_at >= now - FRESH_FIX)
        driver["location"] = _location_state(state, holding, now)
        if driver["location"] == "live":
            driver.update(lat=state.lat, lng=state.lng,
                          updated_at=state.location_at.isoformat() + "Z")

    expires_in = None
    if booking.status == "pending" and booking.requested_at is not None:
        left = booking.requested_at + _accept_window() - now
        expires_in = max(0, int(left.total_seconds()))

    reviewed = BookingReview.query.filter_by(booking_id=booking.id).first() is not None
    return jsonify(
        status=booking.status,
        chosen=chosen,
        driver=driver,
        fare=float(booking.total_price) if booking.total_price is not None else None,
        fare_text=_money_text(booking.total_price) if booking.total_price is not None else None,
        expires_in=expires_in,
        can_cancel=booking.status in CANCELLABLE,
        can_choose_again=can_choose_again(booking, now),
        driver_silent=booking.status in ("accepted", "confirmed", "arriving")
        and _silent(booking, state, now),
        review_url=url_for("dispatch.review", reference=booking.reference)
        if booking.status == "completed" and not reviewed else None,
    )


@bp.get("/ride/route/<reference>")
def customer_route(reference):
    """Road routes for this customer's accepted trip. Nothing before acceptance."""
    booking = settle(customer_booking(reference))
    if not _within_rate_limit("_trip_route", 30):
        return jsonify(error="Please wait a moment."), 429
    state = db.session.get(DriverState, booking.operator_id) if booking.operator_id else None
    return jsonify(trip_routes(booking, state, datetime.utcnow()))


@bp.post("/ride/cancel/<reference>")
def cancel(reference):
    booking = settle(customer_booking(reference))
    changed = db.session.execute(update(Booking).where(
        Booking.id == booking.id, Booking.status.in_(CANCELLABLE),
    ).values(status="cancelled"))
    if changed.rowcount != 1:
        db.session.rollback()
        return jsonify(error="This trip can't be cancelled now."), 409
    release(booking.id)
    db.session.commit()
    return jsonify(status="cancelled")


@bp.post("/ride/choices/<reference>")
def choices_again(reference):
    """Drivers the customer can pick instead, when their driver can't take the trip."""
    booking = settle(customer_booking(reference))
    if not can_choose_again(booking):
        return jsonify(error="Your driver is still on this trip."), 409
    if not _within_rate_limit("_dispatch_quote", 15):
        return jsonify(error="Please wait a moment before checking again."), 429
    if booking.route_distance_m is None or not booking.passengers:
        return jsonify(error="This trip can't be re-priced. Please request a new ride."), 409

    choices = offers(booking.route_distance_m, booking.passengers,
                     exclude_operator_id=_excluded_driver(booking))
    token = secrets.token_urlsafe(24)
    session["rechoice"] = {"reference": booking.reference, "token": token,
                           "expires": (datetime.utcnow() + QUOTE_LIFETIME).timestamp()}
    return jsonify(
        choices=_cards(choices), token=token,
        route={"distance_km": booking.route_distance_km,
               "duration_minutes": round((booking.route_duration_s or 0) / 60)},
        error=None if choices else "No other driver is free right now. Please try again "
                                   "in a few minutes.")


@bp.post("/ride/choose-again/<reference>")
def choose_again(reference):
    """Send the same trip to a driver the customer has just picked."""
    booking = settle(customer_booking(reference))
    data = payload()
    now = datetime.utcnow()

    pending = session.get("rechoice") or {}
    token = data.get("token")
    if pending.get("reference") != booking.reference or not isinstance(token, str) \
            or not pending.get("token") or not secrets.compare_digest(token, str(pending["token"])) \
            or pending.get("expires", 0) < now.timestamp():
        return jsonify(error="Please look at the available drivers again."), 400

    fare_id = _whole(data.get("fare_id"))
    vehicle_id = _whole(data.get("vehicle_id"))
    expected = _amount(data.get("expected_price"))
    if fare_id is None or vehicle_id is None or expected is None:
        return jsonify(error="Choose a driver first."), 400

    if not can_choose_again(booking, now):
        return jsonify(error="Your driver is still on this trip."), 409

    choices = offers(booking.route_distance_m, booking.passengers,
                     exclude_operator_id=_excluded_driver(booking))
    chosen = next((c for c in choices if c.fare.id == fare_id and c.vehicle.id == vehicle_id), None)

    def refused(message):
        pending["expires"] = (datetime.utcnow() + QUOTE_LIFETIME).timestamp()
        session["rechoice"] = pending
        return jsonify(error=message, choices=_cards(choices), token=pending["token"]), 409

    if chosen is None:
        return refused("That driver is no longer free. Please choose another driver.")
    if chosen.amount != expected:
        return refused(f"{chosen.fare.operator.display_name}'s price for this trip is now "
                       f"{_money_text(chosen.amount)}. Please check it and choose again.")

    moved = db.session.execute(update(Booking).where(
        Booking.id == booking.id,
        Booking.status == booking.status,
    ).values(operator_id=chosen.fare.operator_id, vehicle_id=chosen.vehicle.id,
             fare_id=chosen.fare.id, total_price=chosen.amount, quote_basis="distance",
             status="pending", requested_at=now))
    if moved.rowcount != 1:
        db.session.rollback()
        return jsonify(error="Your trip has just changed. Please look again."), 409

    # The previous driver lets go first, so the release cannot undo the new hold.
    release(booking.id)
    if not reserve(booking.id, chosen, now):
        db.session.rollback()
        return refused("That driver just became busy. Please choose another driver.")
    db.session.commit()
    session.pop("rechoice", None)
    return jsonify(status="pending")


# --- choosing a driver by name, and reviews -------------------------------------

def _driver_facts(driver, now):
    state = db.session.get(DriverState, driver.id)
    ride_fares = [fare for fare in driver.fares
                  if fare.is_active and fare.kind == "ride" and fare.pricing_model == "distance"]
    cars = [car for car in driver.vehicles if car.is_active]
    return {
        "driver": driver,
        "ride_fares": ride_fares,
        "scheduled_fares": [fare for fare in driver.fares
                            if fare.is_active and not (fare.kind == "ride"
                                                       and fare.pricing_model == "distance")],
        "cars": [car for car in cars if car.service_mode in ("rental", "both")],
        "offers_rides": bool(ride_fares and any(car.service_mode in ("taxi", "both") for car in cars)),
        "available_now": bool(state and state.available and state.active_booking_id is None
                              and state.updated_at and state.updated_at >= now - FRESH_FIX),
        "rating": reviews.summary(reviews.for_driver(driver.id)),
    }


@bp.get("/drivers")
def drivers():
    query = _text(request.args.get("q"), 120)
    approved = Operator.query.filter_by(status="approved").all()
    if query:
        needle = query.casefold()
        approved = [d for d in approved
                    if needle in " ".join(filter(None, [d.display_name, d.name, d.service_area]))
                    .casefold()]
    approved.sort(key=lambda d: d.display_name.casefold())
    now = datetime.utcnow()
    return render_template("dispatch/drivers.html",
                           listing=[_driver_facts(d, now) for d in approved], query=query)


@bp.get("/drivers/<int:operator_id>")
def driver_profile(operator_id):
    driver = Operator.query.filter_by(id=operator_id, status="approved").first_or_404()
    facts = _driver_facts(driver, datetime.utcnow())
    return render_template(
        "dispatch/profile.html", profile=driver, facts=facts, cars=facts["cars"],
        journeys=reviews.summary(reviews.for_driver_journeys(driver.id)),
        review_list=reviews.newest(reviews.for_driver(driver.id), 30),
    )


@bp.route("/booking/<reference>/review", methods=["GET", "POST"])
def review(reference):
    reference = str(reference or "").upper()[:12]
    if session.get("booking_reference") != reference:
        abort(404)
    booking = Booking.query.filter_by(reference=reference).first_or_404()
    if booking.operator_id is None and booking.vehicle_id is None:
        abort(404)
    if booking.status != "completed":
        return render_template("dispatch/review.html", booking=booking, existing=None,
                               error=None, not_ready=True), 403

    existing = BookingReview.query.filter_by(booking_id=booking.id).first()
    error = None
    if request.method == "POST" and existing is None:
        raw_rating = request.form.get("rating", "")
        rating = int(raw_rating) if raw_rating in ("1", "2", "3", "4", "5") else None
        comment = str(request.form.get("comment") or "").strip()
        if rating is None:
            error = "Choose a rating from 1 to 5 stars."
        elif not 3 <= len(comment) <= 2000:
            error = "Please write a few words about it (up to 2,000 characters)."
        else:
            db.session.add(BookingReview(booking_id=booking.id, rating=rating, comment=comment))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
            return redirect(url_for("dispatch.review", reference=reference))
    return render_template("dispatch/review.html", booking=booking, existing=existing,
                           error=error, not_ready=False), (400 if error else 200)


# --- the driver's side ------------------------------------------------------------

def _held_booking(driver, state, now):
    """The trip this driver holds, after letting time expire an unanswered request."""
    if state is None or state.active_booking_id is None:
        return None
    booking = db.session.get(Booking, state.active_booking_id)
    if booking is None or booking.operator_id != driver.id:
        return None
    booking = settle(booking, now)
    db.session.refresh(state)
    if state.active_booking_id != booking.id or \
            booking.status not in ("pending",) + RIDE_ACTIVE:
        return None
    return booking


def _job(booking, now):
    job = {
        "id": booking.id,
        "reference": booking.reference,
        "status": booking.status,
        "pickup": booking.pickup_address or booking.pickup_location,
        "dropoff": booking.dropoff_address or booking.dropoff_location,
        "pickup_point": [float(booking.pickup_lat), float(booking.pickup_lng)]
        if booking.pickup_lat is not None and booking.pickup_lng is not None else None,
        "dropoff_point": [float(booking.dropoff_lat), float(booking.dropoff_lng)]
        if booking.dropoff_lat is not None and booking.dropoff_lng is not None else None,
        "distance_km": booking.route_distance_km,
        "duration": booking.route_duration_label,
        "passengers": booking.passengers,
        "fare_text": _money_text(booking.total_price) if booking.total_price is not None else None,
        "expires_in": None,
        "customer": None,
        "next": None,
        "can_decline": booking.status in DRIVER_CAN_DECLINE,
    }
    if booking.status == "pending":
        left = booking.requested_at + _accept_window() - now if booking.requested_at else timedelta(0)
        job["expires_in"] = max(0, int(left.total_seconds()))
    else:
        # The customer's name and number only once the driver has accepted.
        job["customer"] = {"name": booking.customer_name, "phone": booking.phone}
        stage = NEXT_STAGE.get(booking.status)
        if stage:
            job["next"] = {"status": stage, "label": STAGE_LABELS[stage]}
    return job


@bp.get("/operator/drive")
@login_required
def drive():
    driver = _current()
    return render_template(
        "dispatch/drive.html", map_settings=routing.map_settings(),
        vehicles=Vehicle.query.filter_by(operator_id=driver.id, is_active=True).filter(Vehicle.service_mode.in_(("taxi", "both")))
        .order_by(Vehicle.make, Vehicle.model).all(),
        state=db.session.get(DriverState, driver.id),
        has_ride_fare=any(fare.is_active and fare.kind == "ride" and fare.pricing_model == "distance"
                          for fare in driver.fares),
        accept_seconds=int(_accept_window().total_seconds()),
    )


@bp.get("/operator/drive/state")
@login_required
def drive_state():
    driver = _current()
    now = datetime.utcnow()
    state = db.session.get(DriverState, driver.id)
    booking = _held_booking(driver, state, now)
    fix_age = int((now - state.updated_at).total_seconds()) \
        if state is not None and state.updated_at is not None else None
    location_age = int((now - state.location_at).total_seconds()) \
        if state is not None and state.location_at is not None else None

    waiting = (Booking.query
               .filter(Booking.operator_id == driver.id, Booking.status == "pending",
                       Booking.request_token.is_(None))
               .order_by(Booking.start_date.asc()).limit(10).all())
    return jsonify(
        online=bool(state is not None and state.available and fix_age is not None
                    and fix_age <= FRESH_FIX.total_seconds()),
        vehicle_id=state.vehicle_id if state is not None else None,
        heartbeat_age_s=fix_age,
        location_age_s=location_age,
        # True only while this driver holds an accepted trip: the one time
        # Driving mode sends the phone's position.
        share_location=booking is not None and booking.status in RIDE_ACTIVE,
        job=_job(booking, now) if booking is not None else None,
        scheduled=[{
            "reference": item.reference,
            "what": (f"{item.pickup_address or item.pickup_location} → "
                     f"{item.dropoff_address or item.dropoff_location}")
            if item.is_journey else (item.vehicle.name if item.vehicle else "Car rental"),
            "when": item.pickup_at.strftime("%a %d %b, %H:%M") if item.pickup_at
            else item.start_date.strftime("%a %d %b"),
            "url": url_for("operator.booking_detail", booking_id=item.id),
        } for item in waiting],
    )


@bp.post("/operator/drive/heartbeat")
@login_required
def heartbeat():
    """Driving mode is open: keep this driver online. Carries no location."""
    driver = _current()
    data = payload()
    state = db.session.get(DriverState, driver.id)
    if state is None:
        state = DriverState(operator_id=driver.id, available=False)
        db.session.add(state)
    if not state.active_booking_id:
        vehicle_id = _whole(data.get("vehicle_id"))
        vehicle = Vehicle.query.filter_by(id=vehicle_id, operator_id=driver.id,
                                          is_active=True).filter(Vehicle.service_mode.in_(("taxi", "both"))).first() if vehicle_id else None
        if vehicle is None:
            db.session.rollback()
            return jsonify(error="Choose your car first."), 400
        state.vehicle_id = vehicle.id
        state.available = data.get("available") is True
    state.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(active_booking_id=state.active_booking_id, available=state.available)


@bp.post("/operator/drive/location")
@login_required
def location():
    """The driver's position, for the customer of the accepted trip they hold.

    Refused and not stored at any other time: before acceptance, after the trip
    ends, or for a trip that is not this driver's.
    """
    driver = _current()
    data = payload()
    now = datetime.utcnow()
    lat, lng = _point(data.get("lat"), 90), _point(data.get("lng"), 180)
    if lat is None or lng is None:
        return jsonify(error="Your phone didn't send a usable location."), 400
    booking_id = _whole(data.get("booking_id"))
    state = db.session.get(DriverState, driver.id)
    booking = db.session.get(Booking, booking_id) if booking_id else None
    if (state is None or booking is None or booking.operator_id != driver.id
            or state.active_booking_id != booking.id or booking.status not in RIDE_ACTIVE):
        return jsonify(error="Your location is only shared during a trip you have accepted.",
                       share_location=False), 409
    changed = db.session.execute(update(DriverState).where(
        DriverState.operator_id == driver.id,
        DriverState.active_booking_id == booking.id,
    ).values(lat=lat, lng=lng, location_at=now, updated_at=now))
    if changed.rowcount != 1:
        db.session.rollback()
        return jsonify(error="This trip has just changed.", share_location=False), 409
    db.session.commit()
    return jsonify(share_location=True, active_booking_id=booking.id)


@bp.get("/operator/drive/route")
@login_required
def driver_route():
    """Road routes for the accepted trip this driver holds, and no other."""
    driver = _current()
    now = datetime.utcnow()
    booking_id = _whole(request.args.get("booking_id"))
    state = db.session.get(DriverState, driver.id)
    booking = db.session.get(Booking, booking_id) if booking_id else None
    if booking is None or booking.operator_id != driver.id or state is None \
            or state.active_booking_id != booking.id:
        return jsonify(error="That trip isn't yours."), 404
    if not _within_rate_limit("_trip_route", 30):
        return jsonify(error="Please wait a moment."), 429
    return jsonify(trip_routes(booking, state, now))


@bp.post("/operator/drive/location/stop")
@login_required
def stop_location():
    """The driver turned location sharing off. The trip itself carries on."""
    driver = _current()
    state = db.session.get(DriverState, driver.id)
    if state is not None:
        state.forget_location()
        db.session.commit()
    return jsonify(share_location=False)


@bp.post("/operator/drive/offline")
@login_required
def offline():
    driver = _current()
    state = db.session.get(DriverState, driver.id)
    if state is not None:
        state.available = False
        state.forget_location()
        if state.active_booking_id:
            # Going offline with a request still waiting says no to it, so the
            # customer can choose someone else straight away.
            declined = db.session.execute(update(Booking).where(
                Booking.id == state.active_booking_id,
                Booking.operator_id == driver.id,
                Booking.status == "pending",
            ).values(status="declined"))
            if declined.rowcount == 1:
                release(state.active_booking_id)
        db.session.commit()
    return jsonify(available=False)


def _owned_held(driver, data, now):
    booking_id = _whole(data.get("booking_id"))
    if booking_id is None:
        return None, (jsonify(error="Which trip?"), 400)
    booking = Booking.query.filter_by(id=booking_id, operator_id=driver.id).first()
    if booking is None or not booking.is_on_demand:
        return None, (jsonify(error="That trip isn't yours."), 404)
    booking = settle(booking, now)
    state = db.session.get(DriverState, driver.id)
    if state is None or state.active_booking_id != booking.id:
        messages = {"expired": "This request timed out. The customer is choosing another driver.",
                    "cancelled": "The customer cancelled this trip.",
                    "declined": "This trip is no longer yours."}
        return None, (jsonify(error=messages.get(booking.status, "This trip is no longer yours."),
                              status=booking.status), 409)
    return booking, None


@bp.post("/operator/drive/accept")
@login_required
def accept():
    driver = _current()
    now = datetime.utcnow()
    booking, refused = _owned_held(driver, payload(), now)
    if refused:
        return refused
    if booking.status != "pending":
        return jsonify(error="This request is no longer waiting for you.",
                       status=booking.status), 409

    state = db.session.get(DriverState, driver.id)
    if state.updated_at is None or state.updated_at < now - FRESH_FIX:
        return jsonify(error="You seem to be offline. Keep Driving mode open, then accept."), 409

    accepted = db.session.execute(update(Booking).where(
        Booking.id == booking.id,
        Booking.operator_id == driver.id,
        Booking.status == "pending",
        Booking.requested_at > now - _accept_window(),
    ).values(status="accepted"))
    if accepted.rowcount != 1:
        db.session.rollback()
        settle(booking)
        return jsonify(error="This request is no longer waiting for you.",
                       status=booking.status), 409
    db.session.commit()
    return jsonify(status="accepted")


@bp.post("/operator/drive/decline")
@login_required
def decline():
    driver = _current()
    booking, refused = _owned_held(driver, payload(), datetime.utcnow())
    if refused:
        return refused
    if booking.status not in DRIVER_CAN_DECLINE:
        return jsonify(error="You can't hand back a trip that has started."), 409
    declined = db.session.execute(update(Booking).where(
        Booking.id == booking.id, Booking.operator_id == driver.id,
        Booking.status == booking.status,
    ).values(status="declined"))
    if declined.rowcount != 1:
        db.session.rollback()
        return jsonify(error="This trip has just changed. Please look again."), 409
    release(booking.id)
    db.session.commit()
    return jsonify(status="declined")


@bp.post("/operator/drive/status")
@login_required
def progress():
    driver = _current()
    data = payload()
    booking, refused = _owned_held(driver, data, datetime.utcnow())
    if refused:
        return refused
    target = data.get("status")
    if not isinstance(target, str) or NEXT_STAGE.get(booking.status) != target:
        return jsonify(error="That step isn't possible now.", status=booking.status), 409
    if target == "completed" and booking.needs_quote:
        return jsonify(error="This trip has no fare yet."), 400

    moved = db.session.execute(update(Booking).where(
        Booking.id == booking.id, Booking.operator_id == driver.id,
        Booking.status == booking.status,
    ).values(status=target))
    if moved.rowcount != 1:
        db.session.rollback()
        return jsonify(error="This trip has just changed. Please look again."), 409
    db.session.refresh(booking)
    commission.sync_for(booking)
    db.session.commit()
    return jsonify(status=target)
