"""Create every table the models describe.

Driven from SQLAlchemy metadata rather than hand-written DDL, so the same step
produces the right types on SQLite and on Postgres, and an existing database
that already has the tables is left alone.
"""
VERSION = "0001"
NAME = "initial schema"


def upgrade(connection, metadata):
    metadata.create_all(connection, checkfirst=True)
