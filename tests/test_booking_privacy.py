import unittest
from datetime import date, timedelta
from app import create_app
from app.models import db, Vehicle, Booking
from app.settings import current_settings
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SECRET_KEY = 'test-only'

class BookingPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # A fresh install is a draft; these tests are about the live public site.
        from app.settings import save_settings
        save_settings({'site_live': True})
        car = Vehicle(make='Test', model='Car', year=2024, daily_rate=50)
        db.session.add(car)
        db.session.flush()
        self.car_id = car.id
        self.start = date.today() + timedelta(days=2)
        db.session.add(Booking(reference='JC-TEST01', vehicle=car,
            customer_name='Private Customer', email='customer@example.com',
            phone='1234567', pickup_location='Test', dropoff_location='Test',
            start_date=self.start, end_date=self.start + timedelta(days=2),
            total_price=100))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_direct_link_does_not_expose_customer(self):
        response = self.client.get('/booking/JC-TEST01', follow_redirects=True)
        self.assertNotIn(b'Private Customer', response.data)
        self.assertNotIn(b'customer@example.com', response.data)

    def test_wrong_email_does_not_unlock_link(self):
        self.client.post('/booking', data={'reference':'JC-TEST01','email':'wrong@example.com'})
        self.assertEqual(self.client.get('/booking/JC-TEST01').status_code, 302)

    def test_correct_lookup_unlocks_only_this_session(self):
        response = self.client.post('/booking', data={'reference':'jc-test01','email':'CUSTOMER@example.com'}, follow_redirects=True)
        self.assertIn(b'Private Customer', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'usually within the hour', response.data)
        self.assertEqual(self.app.test_client().get('/booking/JC-TEST01').status_code, 302)

    def test_new_booking_can_view_confirmation(self):
        start = self.start + timedelta(days=10)
        location = current_settings()['locations'][0]
        response = self.client.post(f'/fleet/{self.car_id}/book', data={
            'start':start.isoformat(), 'end':(start+timedelta(days=3)).isoformat(),
            'customer_name':'New Customer', 'email':'new@example.com',
            'pickup_location':location}, follow_redirects=True)
        self.assertIn(b'New Customer', response.data)
        self.assertIn(b'No online payment has been taken', response.data)

    def test_editor_rejects_invalid_prices_without_partial_save(self):
        with self.client.session_transaction() as state:
            state['admin_id'] = 1
            state['_csrf_token'] = 'test-token'
        for value in ('NaN', 'Infinity', '-1', '1e999'):
            response = self.client.post('/admin/api/save', json={'records': {
                f'vehicle:{self.car_id}:make': 'Changed',
                f'vehicle:{self.car_id}:daily_rate': value}},
                headers={'X-CSRF-Token':'test-token'})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(db.session.get(Vehicle, self.car_id).make, 'Test')

if __name__ == '__main__':
    unittest.main()
