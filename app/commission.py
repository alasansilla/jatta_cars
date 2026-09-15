"""What the marketplace earns, and when it is allowed to earn it.

Two rules shape everything here:

* Commission is charged on the **fare**, never on the refundable deposit. The
  deposit is the customer's own money held against damage and handed back, so
  taking a percentage of it would be charging for something nobody earned.
* Commission is recorded **only when a booking is completed**. A pending or
  cancelled booking has earned nothing, and a booking that is un-completed by
  mistake gives its entry back.

The rate is copied onto the entry when it is written. Changing the marketplace
rate later moves future bookings and leaves history alone.
"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .models import CommissionEntry, db
from .settings import current_settings

COMPLETED = "completed"
PENNY = Decimal("0.01")


def _decimal(value, fallback="0"):
    try:
        return Decimal(str(value if value is not None else fallback))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(fallback)


def default_rate(settings=None):
    """The marketplace-wide rate, as a percentage."""
    settings = settings if settings is not None else current_settings()
    rate = _decimal(settings.get("commission_rate"), "0")
    return rate if rate >= 0 else Decimal("0")


def rate_for(operator, settings=None):
    """The rate that applies to one operator.

    An operator with its own negotiated rate keeps it; everyone else follows the
    configured default, so changing that moves them together.
    """
    if operator is not None and operator.commission_rate is not None:
        rate = _decimal(operator.commission_rate)
        return rate if rate >= 0 else Decimal("0")
    return default_rate(settings)


def calculate(base_amount, rate_percent):
    """Commission on `base_amount` at `rate_percent`, to the penny.

    Rounds half up, which is what people expect money to do; Python's default
    banker's rounding would quietly shave alternate half-pennies.
    """
    base = _decimal(base_amount)
    rate = _decimal(rate_percent)
    if base <= 0 or rate <= 0:
        return Decimal("0.00")
    return (base * rate / Decimal("100")).quantize(PENNY, rounding=ROUND_HALF_UP)


def preview(booking, settings=None):
    """What would be recorded if this booking completed. Charges nothing."""
    rate = rate_for(booking.operator, settings)
    base = _decimal(booking.commissionable_amount)
    return {"rate": rate, "base": base, "amount": calculate(base, rate)}


def record_for(booking, settings=None):
    """Write the commission for a completed booking, once.

    Returns the entry, or None if the booking has not completed. Safe to call
    again: the unique key on booking_id means a second call returns what is
    already there rather than charging twice.
    """
    if booking.status != COMPLETED:
        return None
    if booking.commission is not None:
        return booking.commission
    if booking.total_price is None:
        # Nobody has priced this journey yet. Recording zero here would file
        # "earned nothing" as a fact, when the truth is that the fare is still
        # open. The entry waits until someone sets one.
        return None

    figures = preview(booking, settings)
    entry = CommissionEntry(
        booking=booking,
        operator_id=booking.operator_id,
        rate_percent=figures["rate"],
        base_amount=figures["base"],
        amount=figures["amount"],
    )
    db.session.add(entry)
    if booking.completed_at is None:
        booking.completed_at = datetime.utcnow()
    return entry


def reverse_for(booking):
    """Take the commission back when a booking stops being completed."""
    if booking.commission is None:
        return False
    db.session.delete(booking.commission)
    booking.commission = None
    booking.completed_at = None
    return True


def sync_for(booking, settings=None):
    """Bring the ledger in line with the booking's status.

    Called whenever a status changes, so completing and un-completing a booking
    both do the right thing without the caller having to remember which.
    """
    if booking.status in (COMPLETED, 'cancelled'):
        from .models import DriverState
        DriverState.query.filter_by(active_booking_id=booking.id).update(
            {'active_booking_id': None, 'available': False,
             'lat': None, 'lng': None, 'location_at': None})
    if booking.status == COMPLETED:
        return record_for(booking, settings)
    reverse_for(booking)
    return None


def totals(operator=None):
    """Sum of what has been earned, optionally for one operator."""
    query = db.session.query(db.func.coalesce(db.func.sum(CommissionEntry.amount), 0))
    if operator is not None:
        query = query.filter(CommissionEntry.operator_id == operator.id)
    return _decimal(query.scalar())
