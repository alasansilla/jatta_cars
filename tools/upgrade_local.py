"""Back up and migrate the local SQLite preview; never touch remote databases."""
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app
from app.models import db
from migrations.runner import upgrade

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            raise SystemExit('Local SQLite only; no changes made.')
        path = db.engine.url.database
        if not path or path == ':memory:':
            raise SystemExit('No local database file.')
        backup = path + '.before-phone-' + datetime.now().strftime('%Y%m%d%H%M%S')
        with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        os.chmod(backup, 0o600)
        print('Local backup created.')
        upgrade(db.engine, db.metadata)
