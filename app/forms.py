"""Small validation helpers.

The project deliberately avoids WTForms: the forms here are simple enough that
hand-rolled checks stay readable and keep the dependency list short.
"""
import re
from datetime import date, datetime

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def parse_date(value):
    """Turn a YYYY-MM-DD string from a date input into a date, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_rental_dates(start_raw, end_raw, config):
    """Check a requested hire period.

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

    days = (end - start).days
    if days < config["MIN_RENTAL_DAYS"]:
        errors.append(f"The minimum hire is {config['MIN_RENTAL_DAYS']} day(s).")
    if days > config["MAX_RENTAL_DAYS"]:
        errors.append(f"The maximum hire is {config['MAX_RENTAL_DAYS']} days.")
    if (start - today).days > config["MAX_ADVANCE_DAYS"]:
        errors.append(f"Bookings open {config['MAX_ADVANCE_DAYS']} days ahead at most.")

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
