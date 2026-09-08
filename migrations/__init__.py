"""Versioned schema setup.

Deliberately small and explicit rather than Alembic: the schema is defined by
the SQLAlchemy models, and what is needed on top of that is a record of which
steps have run and a place to put the handful of things models cannot express
— the booking exclusion constraint, for one.

Each step lives in ``migrations/versions`` as ``NNNN_name.py`` and defines::

    VERSION = "0002"
    NAME = "what it does"
    def upgrade(connection, metadata): ...

Steps run in order, once each, inside a transaction, recorded in
``schema_migrations``. Run them with::

    python -m migrations
"""
