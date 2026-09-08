"""Image uploads for the staff area.

Bytes go wherever the configured storage backend puts them; this module only
decides what a file is called, records it, and refuses to delete one that a page
still points at.
"""
import mimetypes
import secrets

from flask import current_app
from werkzeug.utils import secure_filename

from .models import MediaAsset, Setting, Vehicle, db
from .settings import FIELDS
from .storage import UPLOAD_PREFIX, StorageError, get_storage


def _extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_allowed(filename):
    return _extension(filename) in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]


def save_upload(storage, alt_text=None):
    """Store an uploaded file and record it. Returns (asset, error)."""
    if storage is None or not storage.filename:
        return None, "Choose a file to upload."
    if not is_allowed(storage.filename):
        allowed = ", ".join(sorted(current_app.config["ALLOWED_IMAGE_EXTENSIONS"]))
        return None, f"That file type is not supported. Use one of: {allowed}."

    original = secure_filename(storage.filename) or "image"
    stem, _, extension = original.rpartition(".")
    stem = (stem or "image")[:60]
    # A short random suffix keeps two uploads of "car.jpg" from colliding, and
    # means a replaced picture is a new URL rather than a cached stale one.
    filename = f"{stem}-{secrets.token_hex(4)}.{extension.lower()}"
    key = UPLOAD_PREFIX + filename

    data = storage.read()
    if not data:
        return None, "That file is empty."

    content_type = storage.mimetype or mimetypes.guess_type(filename)[0]
    try:
        get_storage().save(key, data, content_type)
    except StorageError as error:
        current_app.logger.exception("Upload failed")
        return None, str(error)

    asset = MediaAsset(
        filename=filename,
        original_name=original,
        alt_text=(alt_text or "").strip() or None,
        size_bytes=len(data),
    )
    db.session.add(asset)
    db.session.commit()
    return asset, None


def usages(asset):
    """Where an image is referenced, so nothing is deleted out from under a page."""
    found = []

    for vehicle in Vehicle.query.filter_by(image=asset.path).all():
        found.append(f"the {vehicle.name}")

    image_keys = [key for key, field in FIELDS.items() if field.type == "image"]
    if image_keys:
        rows = Setting.query.filter(Setting.key.in_(image_keys)).all()
        for row in rows:
            if row.value == asset.path:
                found.append(f"the “{FIELDS[row.key].label}” setting")

    return found


def delete_asset(asset):
    """Remove the file and its record. Returns an error message, or None."""
    used_by = usages(asset)
    if used_by:
        return "Still in use by " + ", ".join(used_by) + ". Change those first."

    try:
        get_storage().delete(asset.path)
    except StorageError as error:
        current_app.logger.exception("Delete failed")
        return str(error)

    db.session.delete(asset)
    db.session.commit()
    return None
