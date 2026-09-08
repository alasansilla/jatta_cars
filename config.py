"""Machine-level configuration.

Business details, page copy, pick-up points and booking rules are *not* here —
they are edited in the staff area under Settings. Their starting values live in
`app/settings.py`.
"""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Change this in production: export JATTA_SECRET_KEY="..."
    SECRET_KEY = os.environ.get("JATTA_SECRET_KEY", "dev-only-not-for-production")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "JATTA_DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "jatta.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Image uploads.
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB per upload
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
