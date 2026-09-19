"""A returning visitor gets the new stylesheet the moment it changes.

The stylesheet used to live at /static/css/style.css whatever it contained.
Flask asked browsers to check it each time, but Cloudflare rewrote that into a
four-hour browser cache, so after a release a phone that had visited recently
kept the old CSS against the new HTML — the owner's own phone showed the home
page cut off on the right after the fix was already live.

Every static URL now carries a fingerprint of the file. New bytes, new URL,
nothing cached to get in the way; unchanged bytes keep their URL and may be
cached for a year.
"""
import hashlib
import os
import re
import unittest

from app import create_app
from app.models import db
from app.settings import save_settings
from config import Config


def fingerprint(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:12]


class StaticVersioningTests(unittest.TestCase):
    def setUp(self):
        class Local(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-" + "x" * 40
        self.app = create_app(Local)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings({"site_live": True})
        self.client = self.app.test_client()
        self.css_path = os.path.join(self.app.static_folder, "css", "style.css")
        self.scratch = os.path.join(self.app.static_folder, "css", "_versioning_test.css")

    def tearDown(self):
        if os.path.exists(self.scratch):
            os.remove(self.scratch)
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def stylesheet_url(self, path="/"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'<link rel="stylesheet" href="([^"]+style\.css[^"]*)"', page).group(1)

    def test_the_page_links_the_stylesheet_by_its_content(self):
        url = self.stylesheet_url()
        self.assertEqual(url, f"/static/css/style.css?v={fingerprint(self.css_path)}")

    def test_every_page_uses_the_same_fingerprinted_url(self):
        urls = {self.stylesheet_url(path) for path in ("/", "/fleet", "/about", "/contact",
                                                       "/driver/join", "/admin/login")}
        self.assertEqual(len(urls), 1, urls)

    def test_new_bytes_mean_a_new_url(self):
        with open(self.scratch, "w") as handle:
            handle.write("body { color: red; }")
        with self.app.test_request_context():
            from flask import url_for
            first = url_for("static", filename="css/_versioning_test.css")
            with open(self.scratch, "w") as handle:
                handle.write("body { color: blue; }  /* changed */")
            second = url_for("static", filename="css/_versioning_test.css")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(f"?v={fingerprint(self.scratch)}"))

    def test_the_current_file_may_be_cached_for_a_year(self):
        response = self.client.get(self.stylesheet_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_anything_else_is_checked_every_time(self):
        """An old page asking for an old fingerprint, or the bare address, must
        never be told to keep what it gets."""
        for url in ("/static/css/style.css", "/static/css/style.css?v=000000000000",
                    "/static/css/style.css?v="):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response.headers["Cache-Control"], "no-cache", url)

    def test_other_static_files_are_fingerprinted_too(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertRegex(page, r'/static/img/favicon\.svg\?v=[0-9a-f]{12}')

    def test_a_missing_or_escaping_file_is_not_fingerprinted_or_read(self):
        version = self.app.static_version
        self.assertIsNone(version("css/does-not-exist.css"))
        self.assertIsNone(version("../config.py"))
        self.assertIsNone(version("../../etc/passwd"))
        self.assertEqual(self.client.get("/static/../config.py").status_code, 404)


if __name__ == "__main__":
    unittest.main()
