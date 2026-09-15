import os
import sys
import tempfile
import unittest

from sqlalchemy import create_engine, text

from app.models import db
from migrations import runner

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import audit_schema  # noqa: E402


class SchemaAuditTests(unittest.TestCase):
    def test_reports_pending_steps_and_missing_indexes_and_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{directory}/a.db")
            runner.upgrade(engine, db.metadata, log=lambda *_: None)
            self.assertEqual(audit_schema.audit(engine, db.metadata), [])
            with engine.begin() as connection:
                connection.execute(text("DROP INDEX ix_bookings_operator_id"))
                connection.execute(text("DELETE FROM schema_migrations WHERE version = '0010'"))
            problems = audit_schema.audit(engine, db.metadata)
            self.assertIn("missing index: ix_bookings_operator_id", problems)
            self.assertIn("migrations not applied: 0010", problems)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
