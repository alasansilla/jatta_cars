from unittest.mock import patch
from datetime import datetime
from app.models import DriverState, Booking, db
from app.routing import Route, _as_route, RoutingUnavailable
from tests.test_marketplace import MarketplaceCase


class DispatchTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        self.car.operator_id = self.operator.id
        self.car.seats = 4
        self.fare.kind = 'ride'
        self.fare.pricing_model = 'distance'
        self.fare.base_price = 50
        self.fare.per_km = 20
        db.session.add(DriverState(operator_id=self.operator.id, vehicle_id=self.car.id,
                                   available=True, updated_at=datetime.utcnow(), lat=13.4, lng=-16.7))
        db.session.commit()
        self.client.get('/ride')
        with self.client.session_transaction() as s:
            self.token = s['_csrf_token']

    def post(self, path, data, client=None, token=None):
        return (client or self.client).post(path, json=data,
                                             headers={'X-CSRF-Token': token or self.token})

    def estimate(self):
        with patch('app.dispatch.routing.routing_available', return_value=True), \
                patch('app.dispatch.routing.route',
                      return_value=Route(3000, 600, 'test', [[13.4, -16.7], [13.5, -16.6]])):
            return self.post('/ride/estimate', dict(pickup_lat=13.4, pickup_lng=-16.7,
                                                    dropoff_lat=13.5, dropoff_lng=-16.6,
                                                    passengers=2)).get_json()

    def request(self, token, **overrides):
        data = dict(token=token, fare_id=self.fare.id, vehicle_id=self.car.id, expected_price=110,
                    pickup_address='Kololi hotel', dropoff_address='Bakau market',
                    customer_name='Awa', email='awa@example.com', phone='+220700111')
        data.update(overrides)
        return self.post('/ride/request', data)

    def test_full_ride_and_private_tracking(self):
        quote = self.estimate()
        self.assertEqual(quote['quote'], 110)
        response = self.request(quote['token'])
        self.assertEqual(response.status_code, 201)
        b = Booking.query.filter_by(booking_type='ride').one()
        self.assertTrue(b.is_on_demand)
        self.assertIsNone(self.client.get('/ride/status/' + b.reference).json['driver'])
        stranger = self.app.test_client()
        self.assertEqual(stranger.get('/ride/status/' + b.reference).status_code, 404)

        driver = self._sign_in_operator()
        csrf = self._csrf(driver)
        r = driver.post('/operator/drive/accept', json={'booking_id': b.id}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.client.get('/ride/status/' + b.reference).json['driver']['name'],
                         self.operator.display_name)
        for stage in ['arriving', 'in_progress', 'completed']:
            r = driver.post('/operator/drive/status', json={'booking_id': b.id, 'status': stage},
                            headers={'X-CSRF-Token': csrf})
            self.assertEqual(r.status_code, 200, r.data)
        db.session.refresh(b)
        self.assertEqual(b.status, 'completed')
        self.assertIsNotNone(b.commission)
        state = db.session.get(DriverState, self.operator.id)
        self.assertIsNone(state.active_booking_id)
        status = self.client.get('/ride/status/' + b.reference).json
        self.assertIsNone(status['driver'])
        self.assertIsNotNone(status['review_url'])

    def test_stages_cannot_be_skipped(self):
        quote = self.estimate()
        self.request(quote['token'])
        b = Booking.query.one()
        driver = self._sign_in_operator()
        csrf = self._csrf(driver)
        for stage in ['arriving', 'in_progress', 'completed']:
            r = driver.post('/operator/drive/status', json={'booking_id': b.id, 'status': stage},
                            headers={'X-CSRF-Token': csrf})
            self.assertEqual(r.status_code, 409, stage)
        driver.post('/operator/drive/accept', json={'booking_id': b.id}, headers={'X-CSRF-Token': csrf})
        r = driver.post('/operator/drive/status', json={'booking_id': b.id, 'status': 'completed'},
                        headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status_code, 409)

    def test_replay_cannot_book_twice(self):
        q = self.estimate()
        self.assertEqual(self.request(q['token']).status_code, 201)
        self.assertEqual(self.request(q['token']).status_code, 400)
        self.assertEqual(Booking.query.count(), 1)

    def test_busy_driver_cannot_be_reserved(self):
        q = self.estimate()
        state = db.session.get(DriverState, self.operator.id)
        state.available = False
        db.session.commit()
        response = self.request(q['token'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['choices'], [])
        self.assertEqual(Booking.query.count(), 0)

    def test_no_csrf_no_booking(self):
        self.assertEqual(self.client.post('/ride/request', json={}).status_code, 400)

    def test_missing_selection_does_not_pick_a_driver(self):
        quote = self.estimate()
        for bad in [dict(fare_id=None), dict(vehicle_id=None), dict(expected_price=None),
                    dict(fare_id=True), dict(fare_id='1.0'), dict(expected_price='NaN'),
                    dict(expected_price='1e3'), dict(vehicle_id=[1])]:
            response = self.request(quote['token'], **bad)
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(Booking.query.count(), 0)
        self.assertIsNone(db.session.get(DriverState, self.operator.id).active_booking_id)

    def test_a_lower_expected_price_is_refused_not_charged(self):
        quote = self.estimate()
        response = self.request(quote['token'], expected_price=90)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Booking.query.count(), 0)

    def test_route_geometry_is_real_and_validated(self):
        r = _as_route({'routes': [{'distance': 100, 'duration': 20, 'geometry': {
            'type': 'LineString', 'coordinates': [[-16.7, 13.4], [-16.6, 13.5]]}}]}, 'test')
        self.assertEqual(r.as_dict()['geometry'], [[13.4, -16.7], [13.5, -16.6]])
        with self.assertRaises(RoutingUnavailable):
            _as_route({'distance': float('inf'), 'duration': 20}, 'test')
