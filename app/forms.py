"""Small validation helpers.

The project deliberately avoids WTForms: the forms here are simple enough that
hand-rolled checks stay readable and keep the dependency list short.
"""
import re
from datetime import date, datetime, time

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def parse_date(value):
    """Turn a YYYY-MM-DD string from a date input into a date, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_rental_dates(start_raw, end_raw, settings):
    """Check a requested hire period against the limits set in the staff area.

    Returns (start, end, errors). Dates are inclusive of the pickup day and
    exclusive of the return day, so a Friday-to-Sunday hire is two days.
    """
    errors = []
    start = parse_date(start_raw)
    end = parse_date(end_raw)

    if start is None:
        errors.append("Enter a valid pick-up date.")
    if end is None:
        errors.append("Enter a valid return date.")
    if start is None or end is None:
        return None, None, errors

    today = date.today()
    if start < today:
        errors.append("The pick-up date cannot be in the past.")
    if end <= start:
        errors.append("The return date must be after the pick-up date.")
        return start, end, errors

    minimum = settings["min_rental_days"]
    maximum = settings["max_rental_days"]
    advance = settings["max_advance_days"]

    days = (end - start).days
    if days < minimum:
        errors.append(f"The minimum hire is {minimum} day(s).")
    if days > maximum:
        errors.append(f"The maximum hire is {maximum} days.")
    if (start - today).days > advance:
        errors.append(f"Bookings open {advance} days ahead at most.")

    return start, end, errors


def validate_customer(form):
    """Check the customer's contact details. Returns (values, errors)."""
    errors = []
    name = (form.get("customer_name") or "").strip()
    email = (form.get("email") or "").strip()
    phone = (form.get("phone") or "").strip()

    if len(name) < 2:
        errors.append("Enter your full name.")
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if phone and len(phone) < 6:
        errors.append("Enter a valid phone number, or leave it blank.")

    return {"customer_name": name, "email": email, "phone": phone}, errors


def validate_journey(form, settings):
    """Check a requested ride or airport transfer.

    Returns (pickup_at, errors). A journey happens at a moment rather than over
    a range of days, so this checks a date *and* a time, and refuses anything in
    the past — a driver cannot be sent to a pick-up that has already happened.
    """
    errors = []
    day = parse_date(form.get("pickup_date"))
    raw_time = (form.get("pickup_time") or "").strip()

    moment = None
    if raw_time:
        hour, _, minute = raw_time.partition(":")
        try:
            moment = time(int(hour), int(minute))
        except (ValueError, TypeError):
            moment = None

    if day is None:
        errors.append("Choose the date you want to travel.")
    if moment is None:
        errors.append("Enter a pick-up time, for example 14:30.")
    if errors:
        return None, errors

    pickup_at = datetime.combine(day, moment)
    if pickup_at <= datetime.now():
        errors.append("The pick-up time has to be in the future.")

    advance = settings["max_advance_days"]
    if (day - date.today()).days > advance:
        errors.append(f"Journeys can be booked {advance} days ahead at most.")

    return pickup_at, errors


def validate_journey_party(form, fare):
    """Passenger and luggage counts, checked against what the fare seats."""
    errors = []
    raw = (form.get("passengers") or "").strip()
    passengers = int(raw) if raw.isdigit() else 0
    if passengers < 1:
        errors.append("Say how many people are travelling.")
    elif fare is not None and fare.seats and passengers > fare.seats:
        errors.append(f"That vehicle seats {fare.seats}. Ask the operator about a "
                      f"larger one, or split the journey.")

    raw_luggage = (form.get("luggage_count") or "").strip()
    luggage = int(raw_luggage) if raw_luggage.isdigit() else 0

    return {"passengers": passengers or None, "luggage_count": luggage or None}, errors
