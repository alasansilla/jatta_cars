import re
import unittest
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from app import create_app, sms, phone_auth
from app.models import db, Operator, PhoneCode
from app.phone import normalise
from tests.test_marketplace import TestConfig


class DriverPhoneTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config.update(SMS_BACKEND='fake', SMS_FAKE_OUTBOX='', TESTING=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def post(self, path, **data):
        with self.client.session_transaction() as session:
            session.setdefault('_csrf_token', 'test-csrf')
            data['csrf_token'] = session['_csrf_token']
        return self.client.post(path, data=data)

    def code(self):
        response = self.post('/driver/send-code', country_code='+220',
                             phone='7701234', intent='join')
        self.assertEqual(response.status_code, 302)
        return re.search(r'\b\d{6}\b', sms.outbox()[-1]['body']).group()

    def test_join_and_return_to_same_account(self):
        self.assertEqual(self.client.get('/driver/join').status_code, 200)
        code = self.code()
        self.assertEqual(Operator.query.count(), 0)
        self.assertEqual(self.client.get('/driver/code').status_code, 200)
        self.post('/driver/code', code=code)
        self.assertEqual(self.client.get('/driver/name').status_code, 200)
        self.post('/driver/name', name='Test Driver')
        driver = Operator.query.one()
        self.assertEqual(driver.status, 'pending')
        self.assertIsNone(driver.email)
        self.assertEqual(driver.phone_e164, '+220877701234')
        self.assertEqual(self.client.get('/driver/status').status_code, 200)
        self.assertIn('/driver/status', self.client.get('/operator/').location)
        self.client.get('/driver/sign-out')
        self.app.config['SMS_RESEND_SECONDS'] = 0
        self.post('/driver/code', code=self.code())
        self.assertEqual(Operator.query.count(), 1)
        with self.client.session_transaction() as session:
            self.assertEqual(session['operator_id'], driver.id)

    def test_same_number_formats(self):
        for number in ['7701234', '770 1234', '+220877701234', '002207701234', '2207701234']:
            self.assertEqual(normalise('+220', number), '+220877701234')

    def test_wrong_expired_and_replayed_codes_fail(self):
        code = self.code()
        with self.client.session_transaction() as session:
            flow = session['phone_flow']
        args = (flow['phone'], flow['purpose'], flow['nonce'])
        wrong = '000000' if code != '000000' else '111111'
        with self.assertRaises(phone_auth.Refused):
            phone_auth.verify_code(*args, wrong, 'test')
        phone_auth.verify_code(*args, code, 'test')
        with self.assertRaises(phone_auth.Refused):
            phone_auth.verify_code(*args, code, 'test')
        self.app.config['SMS_RESEND_SECONDS'] = 0
        code = self.code()
        row = PhoneCode.query.order_by(PhoneCode.id.desc()).first()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.post('/driver/code', code=code).status_code, 400)
        self.assertEqual(Operator.query.count(), 0)

    def test_no_sms_and_csrf_fail_closed(self):
        self.assertEqual(self.client.post('/driver/send-code', data={}).status_code, 302)
        self.assertEqual(len(sms.outbox()), 0)
        self.app.config['SMS_BACKEND'] = ''
        self.assertEqual(self.post('/driver/send-code', phone='7701234').status_code, 400)
        self.assertEqual(PhoneCode.query.count(), 0)

    def test_cooldown_and_no_unverified_claim(self):
        self.code()
        self.assertEqual(self.post('/driver/send-code', phone='7701234').status_code, 429)
        self.assertEqual(len(sms.outbox()), 1)
        self.assertEqual(Operator.query.count(), 0)

    def test_existing_unverified_contact_not_taken_over(self):
        db.session.add(Operator(name='Existing', slug='existing', phone='+220877701234', status='approved'))
        db.session.commit()
        self.post('/driver/code', code=self.code())
        response = self.post('/driver/name', name='Someone Else')
        self.assertIn(b'You may already have an account', response.data)
        self.assertEqual(Operator.query.count(), 1)
        with self.client.session_transaction() as session:
            self.assertNotIn('operator_id', session)

    def test_parallel_registration_uses_one_account(self):
        with tempfile.TemporaryDirectory() as directory:
            class FileConfig(TestConfig):
                SQLALCHEMY_DATABASE_URI = 'sqlite:///' + directory + '/phone.db'
            app = create_app(FileConfig)
            with app.app_context():
                db.create_all()
            barrier = threading.Barrier(2)
            def register():
                with app.app_context():
                    barrier.wait(timeout=10)
                    driver, created = phone_auth.find_or_create_driver('+220877701234', 'Test Driver')
                    return driver.id, created
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(register) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
            self.assertEqual(len({result[0] for result in results}), 1)
            self.assertEqual(sum(result[1] for result in results), 1)
            with app.app_context():
                self.assertEqual(Operator.query.count(), 1)
                db.session.remove()
                db.engine.dispose()


class GambianNumberingChangeTests(unittest.TestCase):
    """September 2026: Africell, Comium and QCell numbers gained an 87/86/83 prefix."""

    def test_old_and_new_forms_are_one_number(self):
        pairs = [('770 1234', '87 770 1234'),     # Africell
                 ('390 1234', '83 390 1234'),     # QCell
                 ('612 3456', '86 612 3456')]     # Comium
        for old, new in pairs:
            self.assertEqual(normalise('+220', old), normalise('+220', new), old)
            self.assertEqual(len(normalise('+220', new)), len('+220') + 9)

    def test_gamcel_and_foreign_numbers_are_left_alone(self):
        self.assertEqual(normalise('+220', '990 1234'), '+2209901234')
        self.assertEqual(normalise('+44', '07911 123456'), '+447911123456')

    def test_an_old_spelling_signs_into_the_account_made_with_the_new_one(self):
        app = create_app(TestConfig)
        with app.app_context():
            db.create_all()
            first, created = phone_auth.find_or_create_driver(normalise('+220', '87 770 1234'), 'A')
            second, created_again = phone_auth.find_or_create_driver(normalise('+220', '7701234'), 'B')
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first.id, second.id)
            db.session.remove()
            db.drop_all()


class AddNumberHeldElsewhereTests(DriverPhoneTests):
    def test_no_code_is_texted_to_another_drivers_number(self):
        holder = Operator(name='Holder', slug='holder', status='approved',
                          phone_e164=normalise('+220', '7701234'))
        asker = Operator(name='Asker', slug='asker', status='approved')
        db.session.add_all([holder, asker])
        db.session.commit()
        with self.client.session_transaction() as session:
            session['operator_id'] = asker.id
            session['driver_signed_in_at'] = int(__import__('time').time())
        response = self.post('/driver/send-code', country_code='+220', phone='770 1234', intent='add')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(sms.outbox()), 0)
        self.assertEqual(PhoneCode.query.count(), 0)


class SqlParametersHiddenTests(unittest.TestCase):
    def test_engine_hides_bound_parameters(self):
        import config
        self.assertTrue(config._engine_options('sqlite:///x.db').get('hide_parameters'))
        self.assertTrue(config._engine_options(
            'postgresql+psycopg://u:p@h:5432/postgres').get('hide_parameters'))


class NineDigitMigrationTests(unittest.TestCase):
    def test_old_verified_numbers_move_to_nine_digits_without_merging(self):
        import importlib
        step = importlib.import_module('migrations.versions.0009_gambian_nine_digit_numbers')
        app = create_app(TestConfig)
        with app.app_context():
            db.create_all()
            db.session.add_all([
                Operator(name='Old only', slug='old-only', status='approved', phone_e164='+2207701234'),
                Operator(name='Clash old', slug='clash-old', status='approved', phone_e164='+2203901234'),
                Operator(name='Clash new', slug='clash-new', status='pending', phone_e164='+220833901234'),
                Operator(name='Gamcel', slug='gamcel', status='approved', phone_e164='+2209901234'),
            ])
            db.session.commit()
            with db.engine.begin() as connection:
                step.upgrade(connection, db.metadata)
            db.session.expire_all()
            phones = {o.slug: o.phone_e164 for o in Operator.query.all()}
            self.assertEqual(phones['old-only'], '+220877701234')
            self.assertEqual(phones['clash-old'], '+2203901234')    # left for staff
            self.assertEqual(phones['clash-new'], '+220833901234')
            self.assertEqual(phones['gamcel'], '+2209901234')
            self.assertEqual(Operator.query.count(), 4)
            db.session.remove()
            db.drop_all()
