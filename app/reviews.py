"""Reviews customers leave after a completed booking.

A review hangs off exactly one booking, and only a completed booking can carry
one, so every review on the site comes from a trip or a rental that really
happened. There are no other sources: nothing here lets staff or drivers write
reviews, and nothing is seeded.

Who a review is about:

* a ride or airport transfer is about the **driver** who did it;
* a rental is about the **car**, and through it the driver who rents it out.

A driver's profile shows both, labelled. A car's page shows only that car's
rentals. The driver cards customers choose between on a ride show journey
reviews, since that is the thing being chosen.

Reviews are shown without the customer's name.
"""
from sqlalchemy import func

from .models import JOURNEY_TYPES, RENTAL, Booking, BookingReview

TYPE_LABELS = {"ride": "Ride", "transfer": "Airport transfer", "rental": "Car rental"}


def _completed():
    return (BookingReview.query
            .join(Booking, BookingReview.booking_id == Booking.id)
            .filter(Booking.status == "completed"))


def for_driver(operator_id):
    return _completed().filter(Booking.operator_id == operator_id)


def for_driver_journeys(operator_id):
    return for_driver(operator_id).filter(Booking.booking_type.in_(JOURNEY_TYPES))


def for_vehicle(vehicle_id):
    return _completed().filter(Booking.vehicle_id == vehicle_id,
                               Booking.booking_type == RENTAL)


def summary(query):
    count, average = query.with_entities(
        func.count(BookingReview.id), func.avg(BookingReview.rating)).one()
    count = int(count or 0)
    return {"review_count": count,
            "rating": round(float(average), 1) if count else None}


def newest(query, limit=20):
    return query.order_by(BookingReview.created_at.desc(), BookingReview.id.desc()) \
        .limit(limit).all()


def journey_summary(operator_id):
    return summary(for_driver_journeys(operator_id))


def review_summary(operator_id=None, vehicle_id=None, journeys_only=False):
    """Template helper: a rating and a count for a driver or a car."""
    if vehicle_id is not None:
        return summary(for_vehicle(vehicle_id))
    if operator_id is None:
        return {"review_count": 0, "rating": None}
    if journeys_only:
        return summary(for_driver_journeys(operator_id))
    return summary(for_driver(operator_id))


def reviews_for(operator_id=None, vehicle_id=None):
    """Template helper: the query behind a list of reviews."""
    if vehicle_id is not None:
        return for_vehicle(vehicle_id)
    return for_driver(operator_id)


def recent_reviews(operator_id=None, vehicle_id=None, limit=20):
    return newest(reviews_for(operator_id=operator_id, vehicle_id=vehicle_id), limit)


def stars(rating):
    """★★★★☆ for 4. Decorative only; the number is always shown next to it."""
    whole = max(0, min(5, int(round(rating or 0))))
    return "★" * whole + "☆" * (5 - whole)


def register(app):
    @app.context_processor
    def review_helpers():
        return {
            "review_summary": review_summary,
            "reviews_for": reviews_for,
            "recent_reviews": recent_reviews,
            "review_stars": stars,
            "booking_type_label": lambda value: TYPE_LABELS.get(value, value),
        }
