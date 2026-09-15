"""Phone sign-in under real concurrency, and the 0008 migration on old data.

One verified number must open exactly one driver account, a texted code must
work exactly once, and a code's guess counter must never be pushed past its cap
— not just when calls happen one after another, but when they arrive at the
same instant from different workers.

These tests race real threads, each with its own application context and
therefore its own database session, against a file-backed SQLite database in a
temporary directory (threads cannot share an in-memory SQLite). Nothing here
touches the real database, a real SMS provider or a routing provider: texts go
to the in-memory fake outbox.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest

from migrations import runner
from datetime import datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app import create_app, phone_auth, sms
from app.models import Operator, PhoneCode, db
from app.phone import normalise
from tests.test_marketplace import TestConfig

THREADS = 6
JOIN_TIMEOUT = 30


def _file_config(path):
    class FileConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        # A generous busy timeout: SQLite serialises writers, and a writer that
        # gives up after the 5 s default would be a test artefact, not a result.
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}
        SMS_BACKEND = "fake"
        SMS_FAKE_OUTBOX = ""
        ENV_NAME = "development"
        SMS_RESEND_SECONDS = 0
        SMS_CODE_MAX_ATTEMPTS = 5
        SMS_WRONG_CODES_PER_IP_HOUR = 1000
        SMS_MAX_PER_NUMBER_HOUR = 1000
        SMS_MAX_PER_NUMBER_DAY = 1000
        SMS_MAX_PER_IP_HOUR = 1000
        SMS_MAX_PER_DAY = 100000

    return FileConfig


class RaceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="phone-races-")
        self.path = os.path.join(self.directory, "races.db")
        self.app = create_app(_file_config(self.path))
        with self.app.app_context():
            db.create_all()
            db.session.remove()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.directory, ignore_errors=True)

    # --- helpers ------------------------------------------------------------

    def race(self, jobs):
        """Run each callable in its own thread and app context, all at once.

        Returns a list of (result, exception) in job order. Every thread opens
        its own application context, so each has its own session; the barrier
        releases them together.
        """
        barrier = threading.Barrier(len(jobs))
        outcomes = [None] * len(jobs)

        def run(index, job):
            with self.app.app_context():
                try:
                    barrier.wait(timeout=JOIN_TIMEOUT)
                    outcomes[index] = (job(), None)
                except BaseException as error:  # noqa: BLE001 — reported by the test
                    try:
                        db.session.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    outcomes[index] = (None, error)
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
        self.assertTrue(all(not thread.is_alive() for thread in threads),
                        "a racing thread never finished")
        self.assertNotIn(None, outcomes, "a racing thread reported nothing")
        return outcomes

    def assertNoUnexpectedErrors(self, outcomes, allowed=()):
        unexpected = [repr(error) for _, error in outcomes
                      if error is not None and not isinstance(error, allowed)]
        self.assertEqual(unexpected, [], "racing calls failed with unexpected errors")

    def make_operator(self, name, email, password="operator-password", phone=None):
        with self.app.app_context():
            operator = Operator(name=name, slug=Operator.make_slug(name), email=email,
                                contact_name=name, phone=phone, status="approved")
            operator.set_password(password)
            db.session.add(operator)
            db.session.commit()
            operator_id = operator.id
            db.session.remove()
        return operator_id

    def send_code(self, phone_e164, purpose=phone_auth.SIGN_IN, nonce=None, ip="10.0.0.1"):
        nonce = nonce or phone_auth.new_nonce()
        with self.app.app_context():
            before = len(sms.outbox())
            row = phone_auth.request_code(phone_e164, purpose, nonce, ip)
            row_id = row.id
            messages = sms.outbox()
            self.assertEqual(len(messages), before + 1, "no fake text was recorded")
            self.assertEqual(messages[-1]["to"], phone_e164)
            code = re.search(r"\b\d{6}\b", messages[-1]["body"]).group()
            db.session.remove()
        return nonce, code, row_id


# --- one number, one account ------------------------------------------------

class FindOrCreateRaceTests(RaceCase):
    NUMBER = "+220877701234"

    def test_simultaneous_registrations_for_one_number_create_one_account(self):
        jobs = [
            (lambda i=i: (lambda d_c: (d_c[0].id, d_c[1]))(
                phone_auth.find_or_create_driver(self.NUMBER, f"Driver {i}")))
            for i in range(THREADS)
        ]
        outcomes = self.race(jobs)
        self.assertNoUnexpectedErrors(outcomes)

        ids = {result[0] for result, _ in outcomes}
        created = [result[1] for result, _ in outcomes]
        self.assertEqual(len(ids), 1, f"one number opened several accounts: {ids}")
        self.assertEqual(created.count(True), 1,
                         f"expected exactly one call to create the account, got {created}")

        with self.app.app_context():
            self.assertEqual(Operator.query.count(), 1)
            driver = Operator.query.one()
            self.assertEqual(driver.id, ids.pop())
            self.assertEqual(driver.phone_e164, self.NUMBER)
            self.assertEqual(driver.status, "pending")
            self.assertIsNone(driver.email)
            self.assertIsNotNone(driver.phone_verified_at)

    def test_simultaneous_registrations_with_different_spellings_create_one_account(self):
        spellings = ["7701234", "770 1234", "+220877701234", "002207701234",
                     "2207701234", "+220 770-1234"]

        def register(spelling, i):
            number = normalise("+220", spelling)
            driver, created = phone_auth.find_or_create_driver(number, f"Driver {i}")
            return driver.id, created, number

        outcomes = self.race([lambda s=s, i=i: register(s, i)
                              for i, s in enumerate(spellings)])
        self.assertNoUnexpectedErrors(outcomes)

        self.assertEqual({result[2] for result, _ in outcomes}, {self.NUMBER},
                         "spellings of one number normalised differently")
        self.assertEqual(len({result[0] for result, _ in outcomes}), 1)
        self.assertEqual([result[1] for result, _ in outcomes].count(True), 1)
        with self.app.app_context():
            self.assertEqual(Operator.query.count(), 1)
            self.assertEqual(Operator.query.filter_by(phone_e164=self.NUMBER).count(), 1)

    def test_simultaneous_registrations_for_different_numbers_each_get_an_account(self):
        """The guard must not be so coarse that it merges different people."""
        numbers = [f"+22077012{30 + i}" for i in range(THREADS)]
        outcomes = self.race([
            lambda n=n: (lambda d_c: (d_c[0].id, d_c[1]))(
                phone_auth.find_or_create_driver(n, "Same Name"))
            for n in numbers
        ])
        self.assertNoUnexpectedErrors(outcomes)
        self.assertEqual(len({result[0] for result, _ in outcomes}), THREADS)
        self.assertTrue(all(result[1] for result, _ in outcomes))
        with self.app.app_context():
            self.assertEqual(Operator.query.count(), THREADS)
            self.assertEqual(sorted(o.phone_e164 for o in Operator.query.all()),
                             sorted(numbers))
            self.assertEqual(len({o.slug for o in Operator.query.all()}), THREADS)


class DatabaseUniquenessTests(RaceCase):
    NUMBER = "+220877701234"

    def test_model_metadata_declares_a_unique_index_on_phone_e164(self):
        table = Operator.__table__
        matching = [index for index in table.indexes
                    if [column.name for column in index.columns] == ["phone_e164"]]
        self.assertEqual(len(matching), 1, "no index on operators.phone_e164 in the model")
        self.assertTrue(matching[0].unique, "operators.phone_e164 index is not unique")
        self.assertTrue(table.c.email.nullable)

    def test_sqlite_schema_has_a_unique_index_on_phone_e164(self):
        with self.app.app_context():
            indexes = inspect(db.engine).get_indexes("operators")
        matching = [index for index in indexes if index["column_names"] == ["phone_e164"]]
        self.assertEqual(len(matching), 1, indexes)
        self.assertTrue(matching[0]["unique"])

    def test_orm_insert_of_a_duplicate_phone_e164_raises_integrity_error(self):
        with self.app.app_context():
            db.session.add(Operator(name="One", slug="one", phone_e164=self.NUMBER,
                                    status="pending"))
            db.session.commit()
            db.session.add(Operator(name="Two", slug="two", phone_e164=self.NUMBER,
                                    status="pending"))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(Operator.query.filter_by(phone_e164=self.NUMBER).count(), 1)

    def test_raw_sql_insert_of_a_duplicate_phone_e164_raises_integrity_error(self):
        """The database, not the application code, is what refuses the second row."""
        statement = text(
            "INSERT INTO operators (name, slug, phone_e164, status, created_at) "
            "VALUES (:name, :slug, :phone, 'pending', :now)")
        with self.app.app_context():
            with db.engine.begin() as connection:
                connection.execute(statement, {"name": "One", "slug": "one",
                                               "phone": self.NUMBER, "now": datetime.utcnow()})
            with self.assertRaises(IntegrityError):
                with db.engine.begin() as connection:
                    connection.execute(statement, {"name": "Two", "slug": "two",
                                                   "phone": self.NUMBER,
                                                   "now": datetime.utcnow()})
            with db.engine.connect() as connection:
                count = connection.execute(text(
                    "SELECT COUNT(*) FROM operators WHERE phone_e164 = :p"),
                    {"p": self.NUMBER}).scalar()
            self.assertEqual(count, 1)

    def test_many_accounts_without_a_verified_number_or_email_coexist(self):
        """Uniqueness must not stop legacy or email-less accounts existing side by side."""
        with self.app.app_context():
            for i in range(3):
                db.session.add(Operator(name=f"Legacy {i}", slug=f"legacy-{i}",
                                        phone_e164=None, email=None, status="pending"))
            db.session.commit()
            self.assertEqual(Operator.query.filter(Operator.phone_e164.is_(None)).count(), 3)


# --- one code, one use ------------------------------------------------------

class VerifyCodeRaceTests(RaceCase):
    NUMBER = "+220877701234"

    def test_two_simultaneous_correct_codes_succeed_exactly_once(self):
        nonce, code, row_id = self.send_code(self.NUMBER)

        def verify(ip):
            return phone_auth.verify_code(self.NUMBER, phone_auth.SIGN_IN, nonce, code, ip)

        outcomes = self.race([lambda: verify("10.0.0.2"), lambda: verify("10.0.0.3")])
        self.assertNoUnexpectedErrors(outcomes, allowed=(phone_auth.Refused,))

        successes = [result for result, error in outcomes if error is None]
        refusals = [error for _, error in outcomes if isinstance(error, phone_auth.Refused)]
        self.assertEqual(successes, [True], f"outcomes: {outcomes}")
        self.assertEqual(len(refusals), 1, f"outcomes: {outcomes}")

        with self.app.app_context():
            row = db.session.get(PhoneCode, row_id)
            self.assertIsNotNone(row.closed_at)
            self.assertEqual(row.outcome, "used")

    def test_many_simultaneous_correct_codes_succeed_exactly_once(self):
        nonce, code, _ = self.send_code(self.NUMBER)
        outcomes = self.race([
            lambda i=i: phone_auth.verify_code(self.NUMBER, phone_auth.SIGN_IN, nonce,
                                               code, f"10.0.1.{i}")
            for i in range(THREADS)
        ])
        self.assertNoUnexpectedErrors(outcomes, allowed=(phone_auth.Refused,))
        self.assertEqual(sum(1 for result, error in outcomes if error is None and result is True),
                         1, f"outcomes: {outcomes}")
        self.assertEqual(sum(1 for _, error in outcomes
                             if isinstance(error, phone_auth.Refused)), THREADS - 1)

    def test_simultaneous_wrong_guesses_cannot_exceed_the_attempt_cap(self):
        maximum = 3
        self.app.config["SMS_CODE_MAX_ATTEMPTS"] = maximum
        nonce, code, row_id = self.send_code(self.NUMBER)
        wrong = "000000" if code != "000000" else "111111"
        guesses = 12

        outcomes = self.race([
            lambda i=i: phone_auth.verify_code(self.NUMBER, phone_auth.SIGN_IN, nonce,
                                               wrong, f"10.0.2.{i}")
            for i in range(guesses)
        ])
        self.assertNoUnexpectedErrors(outcomes, allowed=(phone_auth.Refused,))
        self.assertTrue(all(isinstance(error, phone_auth.Refused) for _, error in outcomes),
                        "a wrong guess was accepted")

        with self.app.app_context():
            row = db.session.get(PhoneCode, row_id)
            self.assertLessEqual(row.attempts, maximum,
                                 f"{row.attempts} guesses counted against a cap of {maximum}")
            self.assertEqual(row.attempts, maximum,
                             "fewer guesses counted than were made up to the cap")
            self.assertIsNotNone(row.closed_at, "an exhausted code stayed open")
            db.session.remove()

        # With the tries used up, even the right code must not work any more.
        with self.app.app_context():
            with self.assertRaises(phone_auth.Refused):
                phone_auth.verify_code(self.NUMBER, phone_auth.SIGN_IN, nonce, code, "10.0.3.1")
            row = db.session.get(PhoneCode, row_id)
            self.assertLessEqual(row.attempts, maximum)

    def test_right_and_wrong_guesses_racing_never_exceed_the_cap_or_succeed_twice(self):
        maximum = 3
        self.app.config["SMS_CODE_MAX_ATTEMPTS"] = maximum
        nonce, code, row_id = self.send_code(self.NUMBER)
        wrong = "000000" if code != "000000" else "111111"
        guesses = [code, code] + [wrong] * 8

        outcomes = self.race([
            lambda g=g, i=i: phone_auth.verify_code(self.NUMBER, phone_auth.SIGN_IN, nonce,
                                                    g, f"10.0.4.{i}")
            for i, g in enumerate(guesses)
        ])
        self.assertNoUnexpectedErrors(outcomes, allowed=(phone_auth.Refused,))
        successes = [i for i, (result, error) in enumerate(outcomes) if error is None]
        self.assertLessEqual(len(successes), 1, f"a code worked twice: {outcomes}")
        for index in successes:
            self.assertEqual(guesses[index], code, "a wrong guess was accepted")
        with self.app.app_context():
            self.assertLessEqual(db.session.get(PhoneCode, row_id).attempts, maximum)


# --- attaching a number while someone else joins with it --------------------

class AttachPhoneRaceTests(RaceCase):
    def test_attach_racing_registration_leaves_the_number_on_one_account(self):
        rounds = 5
        for round_number in range(rounds):
            number = normalise("+220", f"77012{40 + round_number}")
            email = f"owner{round_number}@example.com"
            owner_id = self.make_operator(f"Owner {round_number}", email,
                                          password="owner-password-long")
            with self.app.app_context():
                before_ids = {o.id for o in Operator.query.all()}
                db.session.remove()

            def attach(owner_id=owner_id, number=number):
                return phone_auth.attach_phone(owner_id, number)

            def join(number=number, round_number=round_number):
                driver, created = phone_auth.find_or_create_driver(
                    number, f"Newcomer {round_number}")
                return driver.id, created

            (attached, attach_error), (joined, join_error) = self.race([attach, join])
            self.assertIsNone(attach_error, repr(attach_error))
            self.assertIsNone(join_error, repr(join_error))
            joined_id, created = joined

            with self.app.app_context():
                holders = Operator.query.filter_by(phone_e164=number).all()
                self.assertEqual(len(holders), 1,
                                 f"round {round_number}: {len(holders)} accounts hold the number")
                owner = db.session.get(Operator, owner_id)
                # Nothing about the existing account is changed or merged.
                self.assertEqual(owner.email, email)
                self.assertEqual(owner.name, f"Owner {round_number}")
                self.assertEqual(owner.status, "approved")
                self.assertTrue(owner.check_password("owner-password-long"))
                after_ids = {o.id for o in Operator.query.all()}

                if attached == "attached":
                    self.assertEqual(holders[0].id, owner_id)
                    self.assertIsNotNone(owner.phone_verified_at)
                    self.assertFalse(created, "a second account was created for a held number")
                    self.assertEqual(joined_id, owner_id,
                                     "registration did not return the account holding the number")
                    self.assertEqual(after_ids, before_ids)
                else:
                    self.assertEqual(attached, "taken", f"unexpected attach result {attached!r}")
                    self.assertTrue(created)
                    self.assertNotEqual(joined_id, owner_id)
                    self.assertEqual(holders[0].id, joined_id)
                    self.assertIsNone(owner.phone_e164, "the number was put on both accounts")
                    self.assertIsNone(owner.phone_verified_at)
                    newcomer = db.session.get(Operator, joined_id)
                    self.assertIsNone(newcomer.email, "the owner's email was merged in")
                    self.assertIsNone(newcomer.password_hash)
                    self.assertEqual(after_ids, before_ids | {joined_id})
                db.session.remove()

    def test_two_accounts_attaching_one_number_at_once_only_one_gets_it(self):
        number = "+220877701299"
        first = self.make_operator("First Owner", "first@example.com")
        second = self.make_operator("Second Owner", "second@example.com")

        outcomes = self.race([lambda: phone_auth.attach_phone(first, number),
                              lambda: phone_auth.attach_phone(second, number)])
        self.assertNoUnexpectedErrors(outcomes)
        self.assertEqual(sorted(result for result, _ in outcomes), ["attached", "taken"])
        with self.app.app_context():
            holders = Operator.query.filter_by(phone_e164=number).all()
            self.assertEqual(len(holders), 1)
            winner = first if outcomes[0][0] == "attached" else second
            self.assertEqual(holders[0].id, winner)
            self.assertEqual(Operator.query.count(), 2)


# --- migration 0008 on a database that predates it ---------------------------

OLD_OPERATORS_DDL = """
CREATE TABLE operators (
    id INTEGER NOT NULL,
    name VARCHAR(120) NOT NULL,
    slug VARCHAR(120) NOT NULL,
    contact_name VARCHAR(120),
    email VARCHAR(160) NOT NULL,
    phone VARCHAR(40),
    password_hash VARCHAR(255),
    status VARCHAR(20) NOT NULL,
    commission_rate NUMERIC(5, 2),
    service_area VARCHAR(200),
    terms TEXT,
    notes TEXT,
    created_at DATETIME NOT NULL,
    approved_at DATETIME,
    PRIMARY KEY (id)
)
"""

OLD_OPERATOR_INDEXES = [
    "CREATE UNIQUE INDEX ix_operators_slug ON operators (slug)",
    "CREATE UNIQUE INDEX ix_operators_email ON operators (email)",
    "CREATE INDEX ix_operators_status ON operators (status)",
]

PRE_0008_VERSIONS = ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
TABLES_ADDED_BY_0008 = ("phone_codes", "auth_events")


class Migration0008Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="migration-0008-")
        self.path = os.path.join(self.directory, "pre0008.db")
        self.url = f"sqlite:///{self.path}"
        self.password_hash = generate_password_hash("legacy-password", method="pbkdf2:sha256")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    # --- building the old database -----------------------------------------

    def _build_pre_0008(self):
        from migrations import runner

        engine = create_engine(self.url)
        try:
            with engine.begin() as connection:
                connection.execute(text(OLD_OPERATORS_DDL))
                for statement in OLD_OPERATOR_INDEXES:
                    connection.execute(text(statement))
                others = [table for name, table in db.metadata.tables.items()
                          if name != "operators" and name not in TABLES_ADDED_BY_0008]
                db.metadata.create_all(connection, tables=others)
                # bookings as of 0007 had no request_token / requested_at.
                connection.execute(text("DROP INDEX ix_bookings_request_token"))
                connection.execute(text("ALTER TABLE bookings DROP COLUMN request_token"))
                connection.execute(text("ALTER TABLE bookings DROP COLUMN requested_at"))

                runner._tracking_metadata.create_all(connection)
                for version in PRE_0008_VERSIONS:
                    connection.execute(runner.schema_migrations.insert().values(
                        version=version, name=f"step {version}",
                        applied_at=datetime(2025, 1, 1)))
                self._insert_legacy_rows(connection)
        finally:
            engine.dispose()

    def _insert_legacy_rows(self, connection):
        run = lambda sql, **params: connection.execute(text(sql), params)  # noqa: E731
        operators = [
            dict(id=1, name="Kololi Cabs", slug="kololi-cabs", contact_name="Awa Jallow",
                 email="kololi@example.com", phone="+220877701234", password_hash=self.password_hash,
                 status="approved", commission_rate="12.50", service_area="Kololi, Senegambia",
                 terms="Cash only", notes="staff note: reliable",
                 created_at="2024-03-01 09:15:00.000000", approved_at="2024-03-02 10:00:00.000000"),
            dict(id=2, name="Rival Rides", slug="rival-rides", contact_name=None,
                 email="rival@example.com", phone="770 9999", password_hash=None,
                 status="pending", commission_rate=None, service_area=None, terms=None,
                 notes=None, created_at="2024-04-01 08:00:00.000000", approved_at=None),
            dict(id=5, name="Banjul Transfers", slug="banjul-transfers", contact_name="Lamin",
                 email="banjul@example.com", phone=None, password_hash=self.password_hash,
                 status="suspended", commission_rate="0.00", service_area="Banjul",
                 terms=None, notes="", created_at="2024-05-01 07:30:00.000000",
                 approved_at="2024-05-03 12:00:00.000000"),
        ]
        for row in operators:
            run("INSERT INTO operators (id, name, slug, contact_name, email, phone, "
                "password_hash, status, commission_rate, service_area, terms, notes, "
                "created_at, approved_at) VALUES (:id, :name, :slug, :contact_name, :email, "
                ":phone, :password_hash, :status, :commission_rate, :service_area, :terms, "
                ":notes, :created_at, :approved_at)", **row)

        for vid, operator_id in ((1, 1), (2, 5), (3, None)):
            run("INSERT INTO vehicles (id, make, model, year, category, transmission, fuel, "
                "seats, doors, luggage, daily_rate, deposit, is_active, created_at, operator_id) "
                "VALUES (:id, 'Toyota', 'Corolla', 2021, 'Economy', 'Manual', 'Petrol', 5, 5, 2, "
                "'1500.00', '500.00', 1, '2024-03-05 00:00:00.000000', :op)",
                id=vid, op=operator_id)
        run("INSERT INTO operator_fares (id, operator_id, kind, title, from_location, "
            "to_location, pricing_model, base_price, per_km, is_active, created_at) VALUES "
            "(1, 1, 'ride', 'Around town', 'Kololi', 'Anywhere', 'distance', '100.00', '25.00', "
            "1, '2024-03-06 00:00:00.000000')")
        run("INSERT INTO operator_fares (id, operator_id, kind, title, from_location, "
            "to_location, pricing_model, price, is_active, created_at) VALUES "
            "(2, 5, 'transfer', 'Airport', 'Banjul airport', 'Kololi', 'fixed', '2500.00', "
            "0, '2024-05-06 00:00:00.000000')")
        for bid, operator_id, vehicle_id, fare_id, status in (
                (1, 1, 1, 1, "completed"), (2, 5, 2, 2, "confirmed"), (3, None, 3, None, "pending")):
            run("INSERT INTO bookings (id, reference, booking_type, vehicle_id, operator_id, "
                "fare_id, customer_name, email, phone, pickup_location, dropoff_location, "
                "start_date, end_date, total_price, deposit_amount, status, created_at) VALUES "
                "(:id, :ref, 'ride', :vid, :op, :fare, 'Customer', 'c@example.com', '7000000', "
                "'Kololi', 'Banjul', '2024-06-01', '2024-06-01', '1750.00', '0', :status, "
                "'2024-06-01 10:00:00.000000')",
                id=bid, ref=f"REF{bid:05d}", vid=vehicle_id, op=operator_id, fare=fare_id,
                status=status)
        run("INSERT INTO driver_states (operator_id, vehicle_id, available, active_booking_id, "
            "lat, lng, updated_at) VALUES (1, 1, 1, NULL, 13.44, -16.68, "
            "'2024-06-01 10:00:00.000000')")
        run("INSERT INTO commission_entries (id, booking_id, operator_id, rate_percent, "
            "base_amount, amount, recorded_at) VALUES (1, 1, 1, '12.50', '1750.00', '218.75', "
            "'2024-06-01 11:00:00.000000')")
        run("INSERT INTO booking_reviews (id, booking_id, rating, comment, created_at) VALUES "
            "(1, 1, 5, 'Great', '2024-06-02 00:00:00.000000')")

    # --- reading it back ----------------------------------------------------

    def _snapshot(self):
        """Every row of every table, raw, keyed by table then rowid."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'")]
            snapshot = {}
            for table in tables:
                rows = connection.execute(f'SELECT rowid AS "__rowid__", * FROM "{table}"')
                snapshot[table] = {row["__rowid__"]: dict(row) for row in rows}
            return snapshot
        finally:
            connection.close()

    def _pragma(self, sql):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _upgrade(self):
        from migrations import runner

        engine = create_engine(self.url)
        try:
            return runner.upgrade(engine, db.metadata, log=lambda *_: None)
        finally:
            engine.dispose()

    # --- the tests ----------------------------------------------------------

    def test_the_hand_built_database_really_is_pre_0008(self):
        self._build_pre_0008()
        columns = {row[1]: row for row in self._pragma("PRAGMA table_info(operators)")}
        self.assertNotIn("phone_e164", columns)
        self.assertEqual(columns["email"][3], 1, "email should start NOT NULL")
        from migrations import runner
        engine = create_engine(self.url)
        try:
            with engine.connect() as connection:
                self.assertEqual([step.VERSION for step in runner.pending(connection)], [step.VERSION for step in runner.discover() if step.VERSION >= "0008"])
        finally:
            engine.dispose()

    def test_upgrade_runs_only_0008_and_preserves_every_row_and_value(self):
        self._build_pre_0008()
        before = self._snapshot()

        self.assertEqual(self._upgrade(), [step.VERSION for step in runner.discover() if step.VERSION >= "0008"])
        after = self._snapshot()

        for table, rows in before.items():
            self.assertIn(table, after, f"table {table} disappeared")
            self.assertEqual(set(after[table]), set(rows), f"rows lost or added in {table}")
            for rowid, old in rows.items():
                new = after[table][rowid]
                for column, value in old.items():
                    if column == "__rowid__":
                        continue
                    self.assertIn(column, new, f"{table}.{column} disappeared")
                    if table == "driver_states" and column in ("lat", "lng"):
                        # Migration 0011 keeps a driver's position only during an
                        # accepted trip; this fixture's driver holds none.
                        self.assertIsNone(new[column])
                        continue
                    self.assertEqual(new[column], value,
                                     f"{table} row {rowid} column {column} changed")

        for table in TABLES_ADDED_BY_0008:
            self.assertIn(table, after, f"0008 did not create {table}")

    def test_upgrade_adds_phone_e164_empty_and_never_copies_the_contact_phone(self):
        self._build_pre_0008()
        self._upgrade()
        rows = self._snapshot()["operators"]
        self.assertEqual(len(rows), 3)
        for row in rows.values():
            self.assertIn("phone_e164", row)
            self.assertIsNone(row["phone_e164"],
                              f"operator {row['id']} got a verified number from its contact phone")
            self.assertIsNone(row["phone_verified_at"])
        # The typed contact numbers themselves are left alone.
        self.assertEqual(rows[1]["phone"], "+220877701234")
        self.assertEqual(rows[2]["phone"], "770 9999")

    def test_upgrade_makes_email_nullable_and_creates_the_unique_phone_index(self):
        self._build_pre_0008()
        self._upgrade()

        columns = {row[1]: row for row in self._pragma("PRAGMA table_info(operators)")}
        self.assertEqual(columns["email"][3], 0, "operators.email is still NOT NULL")
        self.assertIn("phone_e164", columns)
        self.assertIn("phone_verified_at", columns)

        indexes = {row[1]: row for row in self._pragma("PRAGMA index_list(operators)")}
        self.assertIn("ix_operators_phone_e164", indexes)
        self.assertEqual(indexes["ix_operators_phone_e164"][2], 1, "phone index is not unique")
        indexed = [row[2] for row in self._pragma("PRAGMA index_info(ix_operators_phone_e164)")]
        self.assertEqual(indexed, ["phone_e164"])
        # Existing uniqueness survives the rebuild.
        for name in ("ix_operators_email", "ix_operators_slug"):
            self.assertIn(name, indexes, f"{name} lost in the rebuild")
            self.assertEqual(indexes[name][2], 1)

        booking_indexes = {row[1]: row for row in self._pragma("PRAGMA index_list(bookings)")}
        self.assertIn("ix_bookings_request_token", booking_indexes)
        self.assertEqual(booking_indexes["ix_bookings_request_token"][2], 1)

        self.assertEqual(self._pragma("PRAGMA foreign_key_check"), [],
                         "foreign keys point at rows or tables that are gone")
        self.assertEqual(self._pragma("PRAGMA integrity_check"), [("ok",)])

        engine = create_engine(self.url)
        try:
            insert = text("INSERT INTO operators (name, slug, email, phone_e164, status, "
                          "created_at) VALUES (:name, :slug, NULL, :phone, 'pending', :now)")
            with engine.begin() as connection:
                connection.execute(insert, {"name": "Phone One", "slug": "phone-one",
                                            "phone": "+220877705555", "now": datetime.utcnow()})
            with self.assertRaises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(insert, {"name": "Phone Two", "slug": "phone-two",
                                                "phone": "+220877705555",
                                                "now": datetime.utcnow()})
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent_and_the_app_works_on_the_migrated_file(self):
        self._build_pre_0008()
        self.assertEqual(self._upgrade(), [step.VERSION for step in runner.discover() if step.VERSION >= "0008"])
        self.assertEqual(self._upgrade(), [], "a second upgrade ran steps again")

        app = create_app(_file_config(self.path))
        with app.app_context():
            try:
                self.assertEqual(Operator.query.count(), 3)
                legacy = db.session.get(Operator, 1)
                self.assertTrue(legacy.check_password("legacy-password"))
                self.assertEqual([o.id for o in legacy.vehicles], [1])
                self.assertEqual([b.id for b in legacy.bookings], [1])
                # The legacy contact number is not a way in, but it does block a
                # second account being opened for it.
                self.assertEqual([o.id for o in phone_auth.unverified_claims("+220877701234")], [1])
                driver, created = phone_auth.find_or_create_driver("+220877706666", "New Driver")
                self.assertTrue(created)
                self.assertIsNone(driver.email)
                self.assertEqual(Operator.query.count(), 4)
            finally:
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
