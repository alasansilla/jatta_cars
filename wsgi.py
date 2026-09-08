"""WSGI entrypoint.

Vercel looks for a Flask instance named `app` in one of a fixed set of
filenames; `wsgi.py` is one of them. Gunicorn and friends use the same target:

    gunicorn wsgi:app
"""
from app import create_app

app = create_app()
