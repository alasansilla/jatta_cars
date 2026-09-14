"""Drivers can submit only their own fleet, without bypassing staff review."""
import unittest
from app import create_app
from app.models import db, Operator, Vehicle, DriverState
from app.settings import save_settings
from config import Config


class DriverCarsTests(unittest.TestCase):
    def setUp(self):
        class Local(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = 'test-' + 'x' * 40
        self.app = create_app(Local)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({'site_live': True})
        self.owner = Operator(name='Owner', slug='owner', status='approved')
        self.other = Operator(name='Other', slug='other', status='approved')
        db.session.add_all([self.owner, self.other])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['operator_id'] = self.owner.id
            session['_csrf_token'] = 'cars-test'
        self.data = dict(csrf_token='cars-test', make='Toyota', model='Corolla',
            year='2021', seats='5', doors='4', luggage='2', category='Economy',
            transmission='Automatic', fuel='Petrol', daily_rate='10000',
            weekly_rate='', deposit='5000', description='Clean car', features='AC')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def add_car(self, owner=None):
        car = Vehicle(make='Toyota', model='Yaris', year=2020, daily_rate=2000,
                      operator_id=(owner or self.owner).id, is_active=True)
        db.session.add(car)
        db.session.commit()
        return car

    def test_submission_owner_and_review_are_server_controlled(self):
        response = self.client.post('/operator/cars/new', data=dict(self.data,
            operator_id=self.other.id, is_active='on'), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        car = Vehicle.query.one()
        self.assertEqual(car.operator_id, self.owner.id)
        self.assertFalse(car.is_active)
        self.assertIn(b'Toyota Corolla', response.data)
        self.assertEqual(self.client.get(f'/operator/cars/{car.id}/edit').status_code, 200)

    def test_other_drivers_car_is_inaccessible(self):
        car = self.add_car(self.other)
        path = f'/operator/cars/{car.id}/edit'
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(path, data=self.data).status_code, 404)
        self.assertNotIn(b'Yaris', self.client.get('/operator/cars').data)

    def test_invalid_money_and_missing_csrf_do_not_create_cars(self):
        for bad in ('NaN', 'Infinity', '-1', '100000000', '0'):
            self.client.post('/operator/cars/new', data=dict(self.data, daily_rate=bad))
        self.client.post('/operator/cars/new', data=dict(self.data, csrf_token='wrong'))
        self.assertEqual(Vehicle.query.count(), 0)

    def test_edits_require_review_and_online_cars_cannot_change(self):
        car = self.add_car()
        state = DriverState(operator_id=self.owner.id, vehicle_id=car.id, available=True)
        db.session.add(state)
        db.session.commit()
        path = f'/operator/cars/{car.id}/edit'
        self.client.post(path, data=self.data)
        self.assertEqual(db.session.get(Vehicle, car.id).model, 'Yaris')
        state.available = False
        db.session.commit()
        self.client.post(path, data=self.data)
        db.session.refresh(car)
        self.assertEqual(car.model, 'Corolla')
        self.assertFalse(car.is_active)

    def test_pending_driver_cannot_manage_fleet(self):
        self.owner.status = 'pending'
        db.session.commit()
        self.assertEqual(self.client.post('/operator/cars/new', data=self.data).status_code, 302)
        self.assertEqual(Vehicle.query.count(), 0)
