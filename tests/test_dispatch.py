from unittest.mock import patch
from datetime import datetime
from app.models import DriverState, Booking, db
from app.routing import Route, _as_route, RoutingUnavailable
from tests.test_marketplace import MarketplaceCase

class DispatchTests(MarketplaceCase):
    def setUp(self):
        super().setUp()
        self.car.operator_id=self.operator.id;self.car.seats=4
        self.fare.kind='ride';self.fare.pricing_model='distance';self.fare.base_price=50;self.fare.per_km=20
        db.session.add(DriverState(operator_id=self.operator.id,vehicle_id=self.car.id,
                                   available=True,updated_at=datetime.utcnow(),lat=13.4,lng=-16.7))
        db.session.commit()
        self.client.get('/ride')
        with self.client.session_transaction() as s:self.token=s['_csrf_token']

    def post(self,path,data):
        return self.client.post(path,json=data,headers={'X-CSRF-Token':self.token})

    def estimate(self):
        with patch('app.dispatch.routing.route',return_value=Route(3000,600,'test',[[13.4,-16.7],[13.5,-16.6]])):
            return self.post('/ride/estimate',dict(pickup_lat=13.4,pickup_lng=-16.7,dropoff_lat=13.5,dropoff_lng=-16.6,passengers=2)).get_json()

    def request(self,token):
        return self.post('/ride/request',dict(token=token,pickup_address='Kololi hotel',dropoff_address='Bakau market',customer_name='Awa',email='awa@example.com',phone='+220700111'))

    def test_full_ride_and_private_tracking(self):
        quote=self.estimate();self.assertEqual(quote['quote'],110)
        response=self.request(quote['token']);self.assertEqual(response.status_code,201)
        b=Booking.query.filter_by(booking_type='ride').one()
        self.assertIsNone(self.client.get('/ride/status/'+b.reference).json['driver'])
        stranger=self.app.test_client();self.assertEqual(stranger.get('/ride/status/'+b.reference).status_code,404)
        driver=self._sign_in_operator();csrf=self._csrf(driver)
        for stage in ['confirmed','arriving','in_progress','completed']:
            r=driver.post('/operator/drive/status',json={'status':stage},headers={'X-CSRF-Token':csrf})
            self.assertEqual(r.status_code,200,r.data)
        db.session.refresh(b);self.assertEqual(b.status,'completed');self.assertIsNotNone(b.commission)
        state=db.session.get(DriverState,self.operator.id);self.assertIsNone(state.active_booking_id)
        self.assertIsNone(self.client.get('/ride/status/'+b.reference).json['driver'])

    def test_replay_cannot_book_twice(self):
        q=self.estimate();self.assertEqual(self.request(q['token']).status_code,201)
        self.assertEqual(self.request(q['token']).status_code,400)
        self.assertEqual(Booking.query.count(),1)

    def test_busy_driver_cannot_be_reserved(self):
        q=self.estimate();state=db.session.get(DriverState,self.operator.id);state.available=False;db.session.commit()
        self.assertEqual(self.request(q['token']).status_code,409)
        self.assertEqual(Booking.query.count(),0)

    def test_no_csrf_no_booking(self):
        self.assertEqual(self.client.post('/ride/request',json={}).status_code,400)

    def test_route_geometry_is_real_and_validated(self):
        r=_as_route({'routes':[{'distance':100,'duration':20,'geometry':{'type':'LineString','coordinates':[[-16.7,13.4],[-16.6,13.5]]}}]},'test')
        self.assertEqual(r.as_dict()['geometry'],[[13.4,-16.7],[13.5,-16.6]])
        with self.assertRaises(RoutingUnavailable):_as_route({'distance':float('inf'),'duration':20},'test')
