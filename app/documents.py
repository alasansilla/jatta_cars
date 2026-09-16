"""Driver identity documents: a licence and a photo ID, kept out of sight.

These are the most sensitive files the site holds, and they are nothing like
the car pictures. A picture is meant to be seen by everyone and is served from
a public bucket; a passport scan must never be reachable by guessing a URL. So
they are kept apart:

* bytes go to their own private place — a directory outside ``static`` locally,
  or a separate Supabase bucket that must not be public — under a random name
  that says nothing about the person;
* nothing ever builds a public link to them. Staff read a document back through
  a signed-in route that streams the bytes;
* what a file claims to be is not trusted. The first bytes have to match a JPEG,
  a PNG or a PDF, and the stored name takes its extension from that, not from
  the upload;
* a replaced document is deleted. Keeping the old passport as history would
  mean holding more of somebody's identity than the check needs.
"""
import hashlib
import os
import secrets
import urllib.error
import urllib.request

from flask import current_app

from .models import DriverDocument, db
from .storage import StorageError

# What a file has to start with to be what it says it is.
SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"%PDF-", "application/pdf", "pdf"),
)
KINDS = {"licence": "Driving licence", "identity": "Photo identification"}
# Flask refuses a request larger than MAX_CONTENT_LENGTH (8 MB) before any of
# this runs, and that answer is a bare "file too large" page. Holding the
# document limit below it means an oversized photo gets an explanation instead.
MAX_BYTES = 6 * 1024 * 1024


class DocumentError(ValueError):
    """The upload cannot be accepted, with a reason fit to show someone."""


def detect(data):
    """What these bytes actually are, or None."""
    for signature, content_type, extension in SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    return None


# --- where the bytes live ----------------------------------------------------

class LocalDocuments:
    """A directory on disk, deliberately outside anything Flask serves."""

    name = "local"

    def __init__(self, root):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        # Keys are generated here, never by a request, but a traversal would
        # write outside the directory, so it is checked rather than trusted.
        if "/" in key or "\\" in key or key in ("", ".", ".."):
            raise StorageError(f"Refusing to touch {key!r}.")
        return os.path.join(self.root, key)

    def save(self, key, data, content_type=None):
        path = self._path(key)
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)

    def read(self, key):
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except FileNotFoundError as error:
            raise StorageError("That document is no longer stored.") from error

    def delete(self, key):
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)


class SupabaseDocuments:
    """A Supabase bucket that must be private: every read is authenticated."""

    name = "supabase"

    def __init__(self, base_url, service_key, bucket):
        if not base_url or not service_key:
            raise StorageError(
                "Private document storage needs SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY.")
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.bucket = bucket

    def _url(self, key):
        return f"{self.base_url}/storage/v1/object/{self.bucket}/{key}"

    def _request(self, method, key, data=None, content_type=None):
        headers = {"Authorization": f"Bearer {self.service_key}", "apikey": self.service_key}
        if content_type:
            headers["Content-Type"] = content_type
            # Never cached by anything in between: this is somebody's passport.
            headers["Cache-Control"] = "no-store"
        request = urllib.request.Request(self._url(key), data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise StorageError(f"Supabase storage said {error.code}") from error
        except urllib.error.URLError as error:
            raise StorageError(f"Could not reach Supabase storage: {error.reason}") from error

    def save(self, key, data, content_type=None):
        self._request("POST", key, data=data, content_type=content_type or "application/octet-stream")

    def read(self, key):
        return self._request("GET", key)

    def delete(self, key):
        try:
            self._request("DELETE", key)
        except StorageError as error:
            if "404" not in str(error):
                raise


def get_store():
    """The configured private store, built once per application."""
    store = current_app.extensions.get("document_store")
    if store is None:
        backend = current_app.config.get("DOCUMENT_STORAGE_BACKEND")
        if backend is None:
            backend = "supabase" if current_app.config.get("SUPABASE_URL") else "local"
        if backend == "supabase":
            store = SupabaseDocuments(
                current_app.config["SUPABASE_URL"],
                current_app.config["SUPABASE_SERVICE_ROLE_KEY"],
                current_app.config["SUPABASE_DOCUMENTS_BUCKET"])
        else:
            store = LocalDocuments(current_app.config["DOCUMENT_ROOT"])
        current_app.extensions["document_store"] = store
    return store


# --- what the application does with them -------------------------------------

def save_document(operator, kind, upload):
    """Store one document for a driver, replacing whatever it supersedes.

    Returns the row. Raises DocumentError with something worth showing when the
    upload cannot be accepted.
    """
    if kind not in KINDS:
        raise DocumentError("Choose which document this is.")
    if upload is None or not getattr(upload, "filename", ""):
        raise DocumentError("Choose a file to upload.")

    data = upload.read(MAX_BYTES + 1)
    if not data:
        raise DocumentError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise DocumentError("That file is larger than 6 MB. Send a smaller photo or scan.")

    detected = detect(data)
    if detected is None:
        raise DocumentError(
            "That file is not a photo or a PDF. Send a JPEG, a PNG or a PDF.")
    content_type, extension = detected

    key = f"{secrets.token_urlsafe(24)}.{extension}"
    get_store().save(key, data, content_type)

    # One current document per kind, and the database says so. The row it
    # replaces has to go first, or the new one collides with it.
    previous = DriverDocument.query.filter_by(operator_id=operator.id, kind=kind).all()
    for old in previous:
        db.session.delete(old)
    db.session.flush()

    document = DriverDocument(
        operator_id=operator.id, kind=kind, key=key, content_type=content_type,
        size_bytes=len(data), digest=hashlib.sha256(data).hexdigest(),
        original_name=(upload.filename or "")[:120] or None,
    )
    db.session.add(document)
    db.session.commit()

    # Only once the replacement is safely recorded: the old scan is deleted
    # rather than kept, because holding more of someone's identity than the
    # check needs is a risk with no purpose.
    for old in previous:
        try:
            get_store().delete(old.key)
        except StorageError:
            current_app.logger.exception("Could not delete a replaced document")
    return document


def read_document(document):
    """The bytes, for a staff member who is signed in."""
    return get_store().read(document.key)


def for_operator(operator):
    """What is on file, by kind."""
    rows = DriverDocument.query.filter_by(operator_id=operator.id).all()
    return {document.kind: document for document in rows}


def missing_for(operator):
    """The kinds this driver has not sent yet."""
    held = for_operator(operator)
    return [kind for kind in KINDS if kind not in held]
