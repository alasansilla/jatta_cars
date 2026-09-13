"""Customer ride requests and authenticated single-vehicle operator dispatch."""
from datetime import datetime, timedelta
from decimal import Decimal
import secrets
from flask import Blueprint, abort, jsonify, render_template, request, session, redirect, url_for
from sqlalchemy import update
from .models import db, Booking, DriverState, OperatorFare, Operator, Vehicle
from .operator import _current, login_required
from .public import hold_until_published, _coordinate, _within_rate_limit
from .forms import validate_customer
from .settings import current_settings
from . import routing, commission

bp = Blueprint('dispatch', __name__)
ACTIVE = ('confirmed', 'arriving', 'in_progress')

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
    amount, fare, state = choices[0]
    token = secrets.token_urlsafe(24)
    session['ride_quote'] = dict(token=token, fare_id=fare.id, vehicle_id=state.vehicle_id,
        amount=str(amount), points=points, distance=leg.distance_m, duration=leg.duration_s,
        provider=leg.provider, passengers=passengers, expires=(datetime.utcnow()+timedelta(minutes=2)).timestamp())
    return jsonify(route=leg.as_dict(), quote=float(amount), token=token,
                   currency='GMD', message='Estimated fare. Driver availability is checked when you request.')


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
    fare=db.session.get(OperatorFare,quote['fare_id'])
    if not fare or not fare.is_bookable: return jsonify(error='This ride is no longer available.'),409
    state=db.session.get(DriverState,fare.operator_id)
    vehicle=db.session.get(Vehicle, quote['vehicle_id'])
    if not state or not vehicle or not vehicle.is_active or vehicle.operator_id != fare.operator_id:
        return jsonify(error='Driver unavailable. Get another estimate.'),409
    now=datetime.utcnow()
    # Price is taken only from the signed server session; recheck the published rate.
    amount,_=fare.quote(quote['distance'])
    if amount is None or amount != Decimal(quote['amount']):
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
        db.session.rollback()
        return jsonify(error='That driver just became busy. Get another estimate.'),409
    db.session.commit()
    session.pop('ride_quote',None);session['booking_reference']=b.reference
    return jsonify(url=url_for('dispatch.track',reference=b.reference)),201


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
    changed=db.session.execute(update(Booking).where(Booking.id==b.id,Booking.status.in_(('pending','confirmed','arriving'))).values(status='cancelled'))
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
    allowed={'pending':('confirmed','cancelled'),'confirmed':('arriving','cancelled'),
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
