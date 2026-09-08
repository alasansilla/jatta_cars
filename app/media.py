"""Image uploads for the staff area."""
import os
import secrets

from flask import current_app
from werkzeug.utils import secure_filename

from .models import MediaAsset, Vehicle, db
from .settings import FIELDS
from .models import Setting


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
    # A short random suffix keeps two uploads of "car.jpg" from colliding.
    filename = f"{stem}-{secrets.token_hex(4)}.{extension.lower()}"

    destination = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    storage.save(destination)

    asset = MediaAsset(
        filename=filename,
        original_name=original,
        alt_text=(alt_text or "").strip() or None,
        size_bytes=os.path.getsize(destination),
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

    path = os.path.join(current_app.config["UPLOAD_FOLDER"], asset.filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(asset)
    db.session.commit()
    return None
