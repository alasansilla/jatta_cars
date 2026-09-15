from tests.test_driver_cars import DriverCarsTests
from tests.test_dispatch import DispatchTests
from app.models import Vehicle, db
from app.public import bookable_vehicles
from app.dispatch import offers

class CarModes(DriverCarsTests):
    def test_taxi_submission_without_rental_prices(self):
        data = dict(self.data, service_mode='taxi', daily_rate='', deposit='')
        self.client.post('/operator/cars/new', data=data)
        car = Vehicle.query.one()
        self.assertEqual(car.service_mode, 'taxi')
        self.assertEqual(car.daily_rate, 0)
        self.assertFalse(car.is_active)

    def test_taxi_hidden_from_rental_search_and_direct_booking(self):
        car = self.add_car()
        car.service_mode = 'taxi'
        db.session.commit()
        self.assertEqual(bookable_vehicles().count(), 0)
        self.assertEqual(self.client.get(f'/fleet/{car.id}').status_code, 404)
        self.assertEqual(self.client.post(f'/fleet/{car.id}/book', data=self.data).status_code, 404)
        car.service_mode = 'rental'
        db.session.commit()
        self.assertEqual(bookable_vehicles().count(), 1)

    def test_invalid_mode_and_unpriced_rental_rejected(self):
        for mode in ['unknown', 'rental', 'both']:
            self.client.post('/operator/cars/new', data=dict(self.data, service_mode=mode, daily_rate=''))
        self.assertEqual(Vehicle.query.count(), 0)

class RideModes(DispatchTests):
    def test_rental_car_cannot_offer_or_go_online(self):
        self.car.service_mode = 'rental'
        db.session.commit()
        self.assertEqual(offers(3000, 2), [])
        driver = self._sign_in_operator()
        response = driver.post('/operator/drive/heartbeat', json={'vehicle_id':self.car.id,'available':True}, headers={'X-CSRF-Token':self._csrf(driver)})
        self.assertEqual(response.status_code, 400)
        self.car.service_mode = 'taxi'
        db.session.commit()
        self.assertTrue(offers(3000, 2))

class MigrationChecks(__import__('unittest').TestCase):
    def test_existing_cars_keep_both_and_migration_can_repeat(self):
        import importlib
        from sqlalchemy import create_engine, text
        step = importlib.import_module('migrations.versions.0012_vehicle_service_mode')
        engine = create_engine('sqlite:///:memory:')
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE vehicles (id INTEGER PRIMARY KEY)'))
            connection.execute(text('INSERT INTO vehicles (id) VALUES (1)'))
            step.upgrade(connection, None)
            step.upgrade(connection, None)
            self.assertEqual(connection.execute(text('SELECT service_mode FROM vehicles')).scalar(), 'both')

class ReviewQueue(DriverCarsTests):
    def test_submission_appears_for_admin_and_approval_clears_queue(self):
        self.client.post('/operator/cars/new', data=dict(self.data, review_pending='false'))
        car = Vehicle.query.one()
        self.assertTrue(car.review_pending)
        staff = self.app.test_client()
        with staff.session_transaction() as session:
            session['admin_id'] = 1
            session['_csrf_token'] = 'staff-review'
        self.assertIn(b'Cars awaiting review (1)', staff.get('/admin/').data)
        page = staff.get('/admin/vehicles?review=pending')
        self.assertIn(b'Approve and publish', page.data)
        self.assertIn(b'Corolla', page.data)
        staff.post(f'/admin/vehicles/{car.id}/toggle', data={'csrf_token':'staff-review'})
        db.session.refresh(car)
        self.assertTrue(car.is_active)
        self.assertFalse(car.review_pending)
        self.assertNotIn(b'Review details', staff.get('/admin/vehicles?review=pending').data)
        self.client.post(f'/operator/cars/{car.id}/edit', data=self.data)
        db.session.refresh(car)
        self.assertTrue(car.review_pending)
        self.assertFalse(car.is_active)
