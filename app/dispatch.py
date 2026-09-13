"""Customer ride requests and authenticated single-vehicle operator dispatch."""
from datetime import datetime, timedelta
from decimal import Decimal
import secrets
from flask import Blueprint, abort, jsonify, render_template, request, session, redirect, url_for
from sqlalchemy import update
from .models import db, Booking, DriverState, OperatorFare, Operator, Vehicle, BookingReview
from sqlalchemy.exc import IntegrityError
from .operator import _current, login_required
from .public import hold_until_published, _coordinate, _within_rate_limit
from .forms import validate_customer
from .settings import current_settings
from . import routing, commission

bp = Blueprint('dispatch', __name__)
ACTIVE = ('accepted', 'confirmed', 'arriving', 'in_progress')
# A ride nobody has claimed yet: any available driver may take it.
OPEN = ('pending',)

@bp.before_request
def guard():
    held = hold_until_published()
    if held is not None:
        return held
    if request.method == 'POST':
        token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
        expected = session.get('_csrf_token', '')
        if not expected or not secrets.compare_digest(token, expected):
            return jsonify(error='Your session expired. Refresh and try again.'), 400

@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def payload():
    return request.get_json(silent=True) or request.form


def measured(data):
    points = [_coordinate(data.get(key), limit) for key, limit in
              [('pickup_lat',90),('pickup_lng',180),('dropoff_lat',90),('dropoff_lng',180)]]
    if None in points:
        return None, points
    try:
        return routing.route(points[:2], points[2:]), points
    except routing.RoutingUnavailable:
        return None, points


def offers(distance, passengers):
    # Only current, approved, unoccupied drivers with their own active vehicle.
    rows = db.session.query(OperatorFare, DriverState).join(
        DriverState, DriverState.operator_id == OperatorFare.operator_id).join(
        Operator, Operator.id == OperatorFare.operator_id).join(
        Vehicle, Vehicle.id == DriverState.vehicle_id).filter(
        Operator.status == 'approved', DriverState.available.is_(True),
        DriverState.active_booking_id.is_(None),
        DriverState.updated_at >= datetime.utcnow()-timedelta(minutes=2),
        Vehicle.is_active.is_(True), Vehicle.operator_id == Operator.id,
        Vehicle.seats >= passengers, OperatorFare.kind == 'ride',
        OperatorFare.is_active.is_(True), OperatorFare.pricing_model == 'distance').all()
    choices = []
    for fare, state in rows:
        amount, basis = fare.quote(distance)
        if amount is not None:
            choices.append((amount, fare, state))
    return sorted(choices, key=lambda row: (row[0], row[1].id))


@bp.get('/ride')
def ride():
    return render_template('dispatch/ride.html', map_settings=routing.map_settings())


@bp.post('/ride/estimate')
def estimate():
    if not _within_rate_limit('_dispatch_quote', 15):
        return jsonify(error='Please wait before requesting another estimate.'), 429
    data = payload()
    try:
        passengers = int(data.get('passengers', 1))
        if not 1 <= passengers <= 8: raise ValueError()
    except (ValueError, TypeError):
        return jsonify(error='Choose between 1 and 8 passengers.'), 400
    leg, points = measured(data)
    choices = offers(leg.distance_m, passengers) if leg else []
    session.pop('ride_quote', None)
    if not choices:
        return jsonify(route=leg.as_dict() if leg else None, quote=None,
            error='No priced ride is available right now. Try a scheduled journey.' if leg else
            'Route estimates are unavailable. You can request a scheduled quote instead.')
    token = secrets.token_urlsafe(24)
    # Keep only route context in the signed session. Every chosen fare and driver
    # is priced and checked again on submission; the customer supplies no price.
    session['ride_quote'] = dict(token=token, points=points, distance=leg.distance_m, duration=leg.duration_s,
        provider=leg.provider, passengers=passengers, expires=(datetime.utcnow()+timedelta(minutes=2)).timestamp())
    cards=[]
    for amount, fare, state in choices:
        car=db.session.get(Vehicle,state.vehicle_id)
        cards.append(dict(fare_id=fare.id, vehicle_id=car.id, price=float(amount),
            driver=fare.operator.contact_name or fare.operator.name,
            operator=fare.operator.name, vehicle=car.name, seats=car.seats,
            image=url_for('static',filename=car.image_url) if car.image_url.startswith('img/') else None,
            profile=url_for('dispatch.driver_profile',operator_id=fare.operator_id),
            car_profile=url_for('public.vehicle_detail',vehicle_id=car.id),
            **review_summary(operator_id=fare.operator_id)))
    return jsonify(route=leg.as_dict(), choices=cards, quote=(cards[0]['price'] if cards else None), token=token,
                   currency='GMD', message='Choose your driver. Availability is checked when you request.')


@bp.post('/ride/request')
def request_ride():
    data=payload()
    quote=session.get('ride_quote') or {}
    if quote.get('token') != data.get('token') or quote.get('expires',0)<datetime.utcnow().timestamp():
        return jsonify(error='Get a fresh estimate before requesting.'),400
    customer, errors=validate_customer(data)
    pickup=str(data.get('pickup_address','')).strip()[:240]
    dropoff=str(data.get('dropoff_address','')).strip()[:240]
    if len(pickup)<3 or len(dropoff)<3: errors.append('Enter pickup and destination.')
    if errors: return jsonify(error=' '.join(errors)),400
    eligible_offers=offers(quote['distance'],quote['passengers'])
    try:
        fare_id=int(data.get('fare_id'))
        vehicle_id=int(data.get('vehicle_id'))
        expected=Decimal(str(data.get('expected_price')))
        if not expected.is_finite(): raise ValueError()
        eligible=next((row for row in eligible_offers
                       if row[1].id==fare_id and row[2].vehicle_id==vehicle_id),None)
    except (TypeError, ValueError, ArithmeticError):
        # Keep older clients working while the public UI uses explicit choice cards.
        # New requests should always send the selected fare and vehicle.
        if not eligible_offers:
            return jsonify(error='Your selected driver is no longer available. Refresh the choices.'),409
        expected, fare, state = eligible_offers[0]
        fare_id, vehicle_id, eligible = fare.id, state.vehicle_id, (expected, fare, state)
    if eligible is None:
        return jsonify(error='Your selected driver is no longer available. Refresh the choices.'),409
    fare=db.session.get(OperatorFare,fare_id)
    if not fare or not fare.is_bookable: return jsonify(error='This ride is no longer available.'),409
    state=db.session.get(DriverState,fare.operator_id)
    vehicle=db.session.get(Vehicle, vehicle_id)
    if not state or not vehicle or not vehicle.is_active or vehicle.operator_id != fare.operator_id:
        return jsonify(error='Driver unavailable. Get another estimate.'),409
    now=datetime.utcnow()
    # Price is taken only from the signed server session; recheck the published rate.
    amount,_=fare.quote(quote['distance'])
    if amount is None or amount != expected:
        return jsonify(error='The fare changed. Get another estimate.'),409
    b=Booking(reference=Booking.new_reference(),booking_type='ride',operator_id=fare.operator_id,
        fare_id=fare.id,vehicle_id=vehicle.id, customer_name=customer['customer_name'],
        email=customer['email'],phone=customer['phone'],pickup_location=pickup,dropoff_location=dropoff,
        pickup_address=pickup,dropoff_address=dropoff,pickup_at=now,start_date=now.date(),end_date=now.date(),
        passengers=quote['passengers'],pickup_lat=quote['points'][0],pickup_lng=quote['points'][1],
        dropoff_lat=quote['points'][2],dropoff_lng=quote['points'][3],route_distance_m=quote['distance'],
        route_duration_s=quote['duration'],route_provider=quote['provider'],total_price=amount,
        quote_basis='distance',deposit_amount=0,status='pending')
    db.session.add(b); db.session.flush()
    won=db.session.execute(update(DriverState).where(
        DriverState.operator_id==fare.operator_id,DriverState.available.is_(True),
        DriverState.active_booking_id.is_(None),DriverState.vehicle_id==vehicle.id,
        DriverState.updated_at>=now-timedelta(minutes=2)).values(active_booking_id=b.id,available=False))
    if won.rowcount != 1:
        # That driver was taken a moment before us. Rather than throwing the
        # customer back to the start, the trip becomes an open offer that any
        # available driver can claim. The quote they were shown still stands.
        b.operator_id = None
        b.vehicle_id = None
    db.session.commit()
    session.pop('ride_quote',None);session['booking_reference']=b.reference
    return jsonify(url=url_for('dispatch.track',reference=b.reference)),201


def reviews_for(operator_id=None, vehicle_id=None):
    query=BookingReview.query.join(Booking).filter(Booking.status=='completed')
    if vehicle_id is not None:
        query=query.filter(Booking.vehicle_id==vehicle_id,Booking.booking_type=='rental')
    elif operator_id is not None:
        query=query.filter(Booking.operator_id==operator_id)
    return query


def review_summary(operator_id=None, vehicle_id=None):
    rows=reviews_for(operator_id,vehicle_id).all()
    return dict(review_count=len(rows), rating=round(sum(r.rating for r in rows)/len(rows),1) if rows else None)


@bp.app_context_processor
def profile_helpers():
    return dict(review_summary=review_summary, reviews_for=reviews_for)


@bp.get('/drivers')
def drivers():
    query=(request.args.get('q') or '').strip()[:120]
    operators=Operator.query.filter_by(status='approved').order_by(Operator.name).all()
    if query:
        operators=[op for op in operators if query.casefold() in
                   (op.name+' '+(op.contact_name or '')).casefold()]
    return render_template('dispatch/drivers.html',operators=operators,query=query)


@bp.get('/drivers/<int:operator_id>')
def driver_profile(operator_id):
    op=Operator.query.filter_by(id=operator_id,status='approved').first_or_404()
    return render_template('dispatch/profile.html',profile=op,
        cars=Vehicle.query.filter_by(operator_id=op.id,is_active=True).all())


@bp.route('/booking/<reference>/review',methods=['GET','POST'])
def review(reference):
    if session.get('booking_reference') != reference: abort(404)
    booking=Booking.query.filter_by(reference=reference).first_or_404()
    if booking.status != 'completed': abort(403)
    existing=BookingReview.query.filter_by(booking_id=booking.id).first()
    error=None
    if request.method=='POST' and existing is None:
        try:
            rating=int(request.form.get('rating',''))
            if not 1<=rating<=5: raise ValueError()
        except ValueError:
            rating=None;error='Choose a rating from 1 to 5.'
        comment=(request.form.get('comment') or '').strip()
        if not 3<=len(comment)<=2000: error='Write a review between 3 and 2,000 characters.'
        if error is None:
            db.session.add(BookingReview(booking_id=booking.id,rating=rating,comment=comment))
            try: db.session.commit()
            except IntegrityError: db.session.rollback()
            return redirect(url_for('dispatch.review',reference=reference))
    return render_template('dispatch/review.html',booking=booking,existing=existing,error=error)


def customer_booking(reference):
    if session.get('booking_reference') != reference: abort(404)
    return Booking.query.filter_by(reference=reference,booking_type='ride').first_or_404()

@bp.get('/ride/track/<reference>')
def track(reference):
    b=customer_booking(reference)
    return render_template('dispatch/track.html', booking=b, map_settings=routing.map_settings())

@bp.get('/ride/status/<reference>')
def status(reference):
    b=customer_booking(reference)
    state=db.session.get(DriverState,b.operator_id) if b.operator_id else None
    driver=None
    if b.status in ACTIVE:
        op=db.session.get(Operator,b.operator_id)
        car=db.session.get(Vehicle,b.vehicle_id)
        driver=dict(name=op.contact_name or op.name,phone=op.phone,
                    vehicle=car.name if car else None)
        if state and state.active_booking_id == b.id and state.updated_at and state.updated_at>=datetime.utcnow()-timedelta(seconds=45):
            driver.update(lat=state.lat,lng=state.lng,updated_at=state.updated_at.isoformat()+'Z')
    return jsonify(status=b.status,driver=driver,fare=float(b.total_price) if b.total_price is not None else None)

@bp.post('/ride/cancel/<reference>')
def cancel(reference):
    b=customer_booking(reference)
    changed=db.session.execute(update(Booking).where(Booking.id==b.id,Booking.status.in_(('pending','accepted','confirmed','arriving'))).values(status='cancelled'))
    if changed.rowcount != 1: return jsonify(error='This trip cannot be cancelled now.'),409
    db.session.execute(update(DriverState).where(DriverState.active_booking_id==b.id).values(active_booking_id=None,available=False))
    db.session.commit()
    return jsonify(status='cancelled')

@bp.get('/operator/drive')
@login_required
def drive():
    op=_current()
    state=db.session.get(DriverState,op.id)
    job=db.session.get(Booking,state.active_booking_id) if state and state.active_booking_id else None
    return render_template('dispatch/drive.html',state=state,job=job,
        vehicles=Vehicle.query.filter_by(operator_id=op.id,is_active=True).all())

@bp.post('/operator/drive/offline')
@login_required
def offline():
    state=db.session.get(DriverState,_current().id)
    if state:
        state.available=False
        db.session.commit()
    return jsonify(available=False)

@bp.post('/operator/drive/location')
@login_required
def location():
    op=_current(); data=payload()
    lat=_coordinate(data.get('lat'),90);lng=_coordinate(data.get('lng'),180)
    if lat is None or lng is None: return jsonify(error='Valid location required.'),400
    state=db.session.get(DriverState,op.id)
    if state is None: state=DriverState(operator_id=op.id);db.session.add(state)
    if not state.active_booking_id:
        try: vehicle_id=int(data.get('vehicle_id',0))
        except (ValueError,TypeError): vehicle_id=0
        vehicle=Vehicle.query.filter_by(id=vehicle_id,operator_id=op.id,is_active=True).first()
        if not vehicle: return jsonify(error='Select your assigned vehicle.'),400
        state.vehicle_id=vehicle.id;state.available=data.get('available') is True
    state.lat=lat;state.lng=lng;state.updated_at=datetime.utcnow()
    db.session.commit()
    return jsonify(active_booking_id=state.active_booking_id,available=state.available)

@bp.post('/operator/drive/status')
@login_required
def progress():
    op=_current();data=payload();state=db.session.get(DriverState,op.id)
    if not state or not state.active_booking_id: abort(404)
    b=Booking.query.filter_by(id=state.active_booking_id,operator_id=op.id).first_or_404()
    allowed={'pending':('confirmed','cancelled'),'accepted':('arriving','cancelled'),
             'confirmed':('arriving','cancelled'),
             'arriving':('in_progress','cancelled'),'in_progress':('completed',)}
    target=data.get('status')
    if target not in allowed.get(b.status,()): return jsonify(error='Invalid trip transition.'),409
    if target=='completed' and b.needs_quote: return jsonify(error='Fare required.'),400
    changed=db.session.execute(update(Booking).where(Booking.id==b.id,Booking.status==b.status).values(status=target))
    if changed.rowcount!=1: db.session.rollback();return jsonify(error='Trip changed. Refresh.'),409
    db.session.refresh(b)
    commission.sync_for(b)
    if target in ('completed','cancelled'):
        state.active_booking_id=None;state.available=False
    db.session.commit()
    return jsonify(status=target)


@bp.get('/operator/drive/offers')
@login_required
def offers_open():
    """Trips waiting for a driver.

    Deliberately says nothing about the customer. A driver deciding whether to
    take a job needs to know where it starts, where it goes and what it pays —
    not who is waiting. Name and phone appear only once they have accepted.
    """
    op = _current()
    state = db.session.get(DriverState, op.id)
    if not state or not state.available or state.active_booking_id:
        return jsonify(offers=[])

    waiting = (Booking.query
               .filter(Booking.booking_type == 'ride',
                       Booking.status.in_(OPEN),
                       Booking.operator_id.is_(None))
               .order_by(Booking.id).limit(20).all())
    return jsonify(offers=[dict(
        id=ride.id, reference=ride.reference,
        pickup=ride.pickup_address or ride.pickup_location,
        dropoff=ride.dropoff_address or ride.dropoff_location,
        distance_km=ride.route_distance_km,
        fare=float(ride.total_price) if ride.total_price is not None else None,
    ) for ride in waiting])


@bp.post('/operator/drive/accept')
@login_required
def accept():
    """Claim an open trip. Exactly one driver can win.

    Two guarded updates, both of which must match exactly one row: the trip must
    still be unclaimed, and this driver must still be free. If either has moved
    under us the whole thing rolls back, so a driver is never left holding a
    trip that someone else also holds.
    """
    op = _current()
    data = payload()
    try:
        booking_id = int(data.get('booking_id', 0))
    except (TypeError, ValueError):
        return jsonify(error='Which trip?'), 400

    state = db.session.get(DriverState, op.id)
    if not state or not state.available or state.active_booking_id or not state.vehicle_id:
        return jsonify(error='Go online with a vehicle before accepting a trip.'), 409
    if not state.updated_at or state.updated_at < datetime.utcnow() - timedelta(minutes=2):
        return jsonify(error='Share your location before accepting a trip.'), 409

    claimed = db.session.execute(update(Booking).where(
        Booking.id == booking_id,
        Booking.booking_type == 'ride',
        Booking.status.in_(OPEN),
        Booking.operator_id.is_(None),
    ).values(operator_id=op.id, vehicle_id=state.vehicle_id, status='accepted'))
    if claimed.rowcount != 1:
        db.session.rollback()
        return jsonify(error='Another driver took that trip.'), 409

    held = db.session.execute(update(DriverState).where(
        DriverState.operator_id == op.id,
        DriverState.available.is_(True),
        DriverState.active_booking_id.is_(None),
    ).values(active_booking_id=booking_id, available=False))
    if held.rowcount != 1:
        db.session.rollback()
        return jsonify(error='You picked up another trip a moment ago.'), 409

    db.session.commit()
    ride = db.session.get(Booking, booking_id)
    return jsonify(status='accepted', reference=ride.reference)
