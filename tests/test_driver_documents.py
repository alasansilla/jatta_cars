"""A driver sends their licence and photo identification when they join.

These are the most sensitive files the site holds, so the tests check the two
things that matter: the bytes really are stored and come back unchanged, and
nobody but signed-in staff can reach them. Files are kept outside anything the
web server hands out by path, under a random name, and a replaced document is
deleted rather than kept.
"""
import io
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from datetime import datetime

from app import create_app, documents
from app.models import AdminUser, DriverDocument, Operator, db
from app.settings import save_settings
from config import Config


def png_bytes(width=1, height=1):
    """A real PNG, so nothing passes on the strength of its file name."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 64 + b"\xff\xd9"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class DocumentCase(unittest.TestCase):
    def setUp(self):
        self.store_root = tempfile.mkdtemp(prefix="gogo-documents-")

        class Local(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-" + "x" * 40
            GEOCODER_URL = ""
            REVERSE_GEOCODER_URL = ""
            ROUTER_URL = ""
            SMS_BACKEND = "fake"
            DOCUMENT_STORAGE_BACKEND = "local"
            DOCUMENT_ROOT = self.store_root
        self.app = create_app(Local)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})
        self.staff = AdminUser(username="admin")
        self.staff.set_password("admin-password-long")
        self.driver = Operator(name="Awa Jallow", slug="awa-jallow", status="pending",
                               phone_e164="+2208770001", phone_verified_at=datetime.utcnow())
        self.other = Operator(name="Musa Touray", slug="musa-touray", status="pending",
                              phone_e164="+2208770002", phone_verified_at=datetime.utcnow())
        db.session.add_all([self.staff, self.driver, self.other])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.store_root, ignore_errors=True)

    def as_driver(self, operator=None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = (operator or self.driver).id
        client.get("/driver/documents")          # issues the form token
        return client

    def upload(self, client, data=None, name="licence.png", kind="licence", token=None):
        with client.session_transaction() as session:
            supplied = session.get("_csrf_token", "") if token is None else token
        return client.post("/driver/documents", follow_redirects=True,
                           content_type="multipart/form-data",
                           data={"csrf_token": supplied, "kind": kind,
                                 "document": (io.BytesIO(png_bytes() if data is None else data),
                                              name)})

    def stored_files(self):
        return sorted(os.listdir(self.store_root))

    def admin(self):
        client = self.app.test_client()
        client.post("/admin/login", data={"username": "admin", "password": "admin-password-long"})
        client.get("/admin/operators")
        return client


class UploadingReallyStoresTheFile(DocumentCase):
    def test_the_bytes_are_saved_and_come_back_unchanged(self):
        data = png_bytes(4, 4)
        response = self.upload(self.as_driver(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("that document is with us", response.get_data(as_text=True))

        document = DriverDocument.query.one()
        self.assertEqual((document.operator_id, document.kind), (self.driver.id, "licence"))
        self.assertEqual(document.content_type, "image/png")
        self.assertEqual(document.size_bytes, len(data))
        self.assertEqual(document.original_name, "licence.png")

        self.assertEqual(self.stored_files(), [document.key])
        with open(os.path.join(self.store_root, document.key), "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertEqual(documents.read_document(document), data)

    def test_a_photo_a_scan_or_a_pdf_are_all_accepted(self):
        client = self.as_driver()
        for data, name, kind, expected in [(JPEG, "licence.jpg", "licence", "image/jpeg"),
                                           (PDF, "passport.pdf", "identity", "application/pdf")]:
            self.upload(client, data, name=name, kind=kind)
            document = DriverDocument.query.filter_by(kind=kind).one()
            self.assertEqual(document.content_type, expected)
            self.assertEqual(documents.read_document(document), data)
        self.assertEqual(len(self.stored_files()), 2)

    def test_the_stored_name_gives_nothing_away_and_is_not_the_one_sent(self):
        self.upload(self.as_driver(), name="awa-jallow-passport.png")
        document = DriverDocument.query.one()
        self.assertNotIn("awa", document.key.lower())
        self.assertNotIn("passport", document.key.lower())
        self.assertTrue(document.key.endswith(".png"))
        self.assertGreaterEqual(len(document.key), 24)

    def test_sending_a_new_one_replaces_the_old_file_and_row(self):
        client = self.as_driver()
        self.upload(client, png_bytes(2, 2))
        first = DriverDocument.query.one()
        first_key = first.key

        self.upload(client, png_bytes(8, 8))
        document = DriverDocument.query.one()               # still exactly one
        self.assertNotEqual(document.key, first_key)
        self.assertEqual(self.stored_files(), [document.key])
        self.assertFalse(os.path.exists(os.path.join(self.store_root, first_key)),
                         "the replaced document should not be kept")

    def test_each_kind_is_held_separately(self):
        client = self.as_driver()
        self.upload(client, kind="licence")
        self.upload(client, JPEG, name="id.jpg", kind="identity")
        self.assertEqual({row.kind for row in DriverDocument.query.all()},
                         {"licence", "identity"})
        self.assertEqual(documents.missing_for(self.driver), [])


class WhatIsRefused(DocumentCase):
    def refusal(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DriverDocument.query.count(), 0)
        self.assertEqual(self.stored_files(), [])
        return response.get_data(as_text=True)

    def test_a_text_file_renamed_to_jpg_is_not_a_photo(self):
        page = self.refusal(self.upload(self.as_driver(), b"just typing, not a photo",
                                        name="licence.jpg"))
        self.assertIn("not a photo or a PDF", page)

    def test_an_empty_file(self):
        self.assertIn("empty", self.refusal(self.upload(self.as_driver(), b"")))

    def test_a_file_over_the_size_limit(self):
        oversized = png_bytes() + b"\x00" * (documents.MAX_BYTES + 1)
        self.assertIn("larger than 6 MB", self.refusal(self.upload(self.as_driver(), oversized)))

    def test_a_file_too_large_for_the_request_is_still_refused_cleanly(self):
        """Past the request-wide cap nothing of ours runs, so check the answer
        is the file-too-large page and not a crash."""
        enormous = png_bytes() + b"\x00" * self.app.config["MAX_CONTENT_LENGTH"]
        response = self.upload(self.as_driver(), enormous)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(DriverDocument.query.count(), 0)
        self.assertEqual(self.stored_files(), [])

    def test_an_unknown_kind_of_document(self):
        self.assertIn("Choose which document this is",
                      self.refusal(self.upload(self.as_driver(), kind="bank-details")))

    def test_a_post_from_another_site(self):
        """Refused for the whole driver area before the view is reached, which
        lands on sign-in with the form-expired message."""
        response = self.upload(self.as_driver(), token="not-the-token")
        self.assertIn("had expired", response.get_data(as_text=True))
        self.assertEqual(DriverDocument.query.count(), 0)
        self.assertEqual(self.stored_files(), [])

    def test_somebody_who_is_not_signed_in_is_sent_to_sign_in(self):
        anonymous = self.app.test_client()
        response = anonymous.get("/driver/documents")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/driver/sign-in", response.headers["Location"])
        self.assertEqual(DriverDocument.query.count(), 0)


class WhoCanSeeThem(DocumentCase):
    def uploaded(self, operator=None):
        self.upload(self.as_driver(operator), png_bytes(3, 3))
        return DriverDocument.query.order_by(DriverDocument.id.desc()).first()

    def path(self, document):
        return f"/admin/operators/{document.operator_id}/document/{document.id}"

    def test_staff_can_open_it_and_nothing_may_cache_it(self):
        document = self.uploaded()
        response = self.admin().get(self.path(document))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, documents.read_document(document))
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex")

    def test_a_stranger_cannot(self):
        document = self.uploaded()
        response = self.app.test_client().get(self.path(document))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_another_driver_cannot_reach_it_by_signing_in_as_themselves(self):
        document = self.uploaded()
        intruder = self.as_driver(self.other)
        self.assertEqual(intruder.get(self.path(document)).status_code, 302)
        # And their own page shows only their own documents, which is none.
        page = intruder.get("/driver/documents").get_data(as_text=True)
        self.assertNotIn("received", page)

    def test_the_document_is_nowhere_a_customer_could_reach(self):
        document = self.uploaded()
        self.driver.status = "approved"
        db.session.commit()
        public = self.app.test_client()
        # Not under static, and not linked from the driver's public profile.
        self.assertEqual(public.get(f"/static/{document.key}").status_code, 404)
        self.assertEqual(public.get(f"/static/uploads/{document.key}").status_code, 404)
        profile = public.get(f"/drivers/{self.driver.id}").get_data(as_text=True)
        self.assertNotIn(document.key, profile)
        self.assertNotIn("document", profile.lower().split("<footer")[0])
        # The file itself lives outside anything Flask serves.
        self.assertNotIn("static", self.app.config["DOCUMENT_ROOT"])

    def test_staff_see_what_is_on_file_before_they_approve(self):
        self.uploaded()
        page = self.admin().get("/admin/operators").get_data(as_text=True)
        self.assertIn(f"/admin/operators/{self.driver.id}/document/", page)
        self.assertIn("Photo ID not sent", page)


class TheJoiningFlow(DocumentCase):
    def test_a_new_driver_is_asked_for_them_straight_away(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["operator_id"] = self.driver.id
        response = client.get("/driver/status", follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertIn("Send your licence and photo identification", page)
        self.assertIn("/driver/documents", page)

    def test_the_application_page_stops_asking_once_both_arrive(self):
        client = self.as_driver()
        self.upload(client, kind="licence")
        self.upload(client, JPEG, name="id.jpg", kind="identity")
        page = client.get("/driver/status").get_data(as_text=True)
        self.assertIn("Licence and photo identification received", page)
        self.assertNotIn("Send your licence", page)

    def test_the_page_says_plainly_who_can_see_them(self):
        page = self.as_driver().get("/driver/documents").get_data(as_text=True)
        self.assertIn("Only GoGo Taxi staff can see them", page)
        self.assertIn("never shown on your public profile", page)


if __name__ == "__main__":
    unittest.main()
