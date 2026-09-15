"""What Google may index: public pages yes; private, unfinished and test listings no."""
import unittest
from xml.etree import ElementTree

from sqlalchemy import event

from app import create_app
from app.models import Operator, OperatorFare, Vehicle, db
from app.settings import FIELDS, PLACEHOLDER_MARKER, save_settings
from config import Config

SITE = "https://gogo-taxi.com"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
FOREIGN_HOST = {"Host": "untrusted.example"}


def finished_wording():
    """Every setting that still says [TBC] by default, filled in."""
    return {key: "Filled in." for key, field in FIELDS.items()
            if PLACEHOLDER_MARKER in str(field.default or "")}


class SearchCase(unittest.TestCase):
    def setUp(self):
        class Local(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {}
            SECRET_KEY = "test-" + "x" * 40
            SITEMAP_CACHE_SECONDS = 0
            GEOCODER_URL = ""
            REVERSE_GEOCODER_URL = ""
            ROUTER_URL = ""
        self.app = create_app(Local)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        save_settings(dict(finished_wording(), site_live=True))
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def driver(self, name, status="approved", phone="+2203000001"):
        driver = Operator(name=name, slug=name.lower().replace(" ", "-"), status=status,
                          phone_e164=phone, service_area="Kololi")
        db.session.add(driver)
        db.session.commit()
        return driver

    def car(self, driver, model="Yaris", mode="rental", active=True, description="Clean and serviced."):
        car = Vehicle(make="Toyota", model=model, year=2020, daily_rate=2500, operator_id=driver.id,
                      is_active=active, service_mode=mode, description=description)
        db.session.add(car)
        db.session.commit()
        return car

    def fare(self, driver, title="Airport transfer", kind="transfer"):
        fare = OperatorFare(operator_id=driver.id, kind=kind, title=title, from_location="Banjul Airport",
                            to_location="Kololi", price=1500, is_active=True)
        db.session.add(fare)
        db.session.commit()
        return fare

    def sitemap_paths(self):
        response = self.client.get("/sitemap.xml", headers=FOREIGN_HOST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        root = ElementTree.fromstring(response.data)
        self.assertEqual(root.tag, SITEMAP_NS + "urlset")
        locations = [loc.text for loc in root.iter(SITEMAP_NS + "loc")]
        self.assertEqual(len(locations), len(set(locations)), "duplicate sitemap entries")
        for location in locations:
            self.assertTrue(location.startswith(SITE + "/"), location)
            self.assertNotIn("?", location)
        return [location[len(SITE):] for location in locations]

    def assertIndexable(self, path):
        response = self.client.get(path, headers=FOREIGN_HOST)
        self.assertEqual(response.status_code, 200, path)
        self.assertNotIn("X-Robots-Tag", response.headers, path)
        return response

    def assertNoindex(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        self.assertLess(response.status_code, 500, path)
        self.assertEqual(response.headers.get("X-Robots-Tag"), "noindex", path)
        if response.status_code != 200:
            self.assertNotIn(b'rel="canonical"', response.data, path)
        elif b'rel="canonical"' in response.data:
            # A listing kept out of search may still name itself, never another page.
            self.assertIn(f'<link rel="canonical" href="{SITE}{path.split("?")[0]}">'.encode(), response.data)
        return response


class RobotsAndSitemap(SearchCase):
    def test_robots_points_at_the_sitemap_and_blocks_only_the_paid_lookups(self):
        response = self.client.get("/robots.txt", headers=FOREIGN_HOST)
        self.assertEqual(response.mimetype, "text/plain")
        body = response.get_data(as_text=True)
        self.assertIn("User-agent: *", body)
        self.assertIn(f"Sitemap: {SITE}/sitemap.xml", body)
        disallowed = [line.split(":", 1)[1].strip() for line in body.splitlines()
                      if line.startswith("Disallow:")]
        # Private pages are not blocked here: a crawler must be able to fetch
        # them to see their noindex.
        self.assertEqual(disallowed, ["/api/"])

    def test_sitemap_lists_landing_pages_on_the_fixed_domain(self):
        paths = self.sitemap_paths()
        for path in ["/", "/ride", "/rides", "/fleet", "/drivers", "/about", "/contact"]:
            self.assertIn(path, paths)
        for private in ["/admin", "/operator", "/driver/", "/booking", "/ride/track", "/api/"]:
            self.assertFalse([p for p in paths if p.startswith(private)], private)
        self.assertNotIn(b"untrusted.example", self.client.get("/sitemap.xml", headers=FOREIGN_HOST).data)

    def test_every_sitemap_entry_is_itself_indexable(self):
        driver = self.driver("Awa Jallow")
        self.car(driver)
        self.fare(driver)
        paths = self.sitemap_paths()
        self.assertGreater(len(paths), 7)
        for path in paths:
            self.assertIndexable(path)

    def test_the_site_url_comes_from_configuration(self):
        self.app.config["PUBLIC_SITE_URL"] = "https://www.example.gm/"
        self.assertIn(b"Sitemap: https://www.example.gm/sitemap.xml", self.client.get("/robots.txt").data)
        self.assertIn(b"<loc>https://www.example.gm/fleet</loc>", self.client.get("/sitemap.xml").data)
        self.assertIn(b'<link rel="canonical" href="https://www.example.gm/fleet">',
                      self.client.get("/fleet").data)


class PublicPages(SearchCase):
    def test_canonical_ignores_host_and_query_string(self):
        for path, canonical in [("/fleet?seats=4&sort=price_desc", "/fleet"),
                                ("/drivers?q=kololi", "/drivers"), ("/ride?driver=7", "/ride"), ("/", "/")]:
            page = self.assertIndexable(path).get_data(as_text=True)
            self.assertIn(f'<link rel="canonical" href="{SITE}{canonical}">', page)
            self.assertIn(f'<meta property="og:url" content="{SITE}{canonical}">', page)
            self.assertEqual(page.count('rel="canonical"'), 1)

    def test_titles_and_descriptions_say_what_each_page_offers(self):
        expected = {
            "/": ("Rides, airport transfers and car rental in The Gambia", "Rides, airport transfers and car rental"),
            "/ride": ("Request a ride in The Gambia", "compare available drivers by price, profile and reviews"),
            "/rides": ("airport transfers in The Gambia", "Book an airport transfer or a scheduled ride in The Gambia"),
            "/fleet": ("Car rental in The Gambia", "Rent a car in The Gambia"),
            "/drivers": ("Choose a driver in The Gambia", "approved drivers in The Gambia"),
        }
        for path, (title, description) in expected.items():
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn(title, page.split("<title>", 1)[1].split("</title>", 1)[0], path)
            meta = page.split('<meta name="description" content="', 1)[1].split('"', 1)[0]
            self.assertIn(description, meta, path)
            self.assertNotIn("Self-drive car hire", meta, path)

    def test_no_invented_business_facts_in_search_metadata(self):
        head = self.client.get("/").get_data(as_text=True).split("</head>", 1)[0]
        for invented in ("application/ld+json", "telephone", "openingHours", "streetAddress", "ratingValue"):
            self.assertNotIn(invented, head)

    def test_holding_page_and_staff_preview_are_not_indexed(self):
        driver = self.driver("Awa Jallow")
        self.car(driver)
        save_settings({"site_live": False})
        for path in ["/", "/fleet", "/drivers", "/ride", "/rides"]:
            held = self.assertNoindex(path)
            self.assertNotIn(b'rel="canonical"', held.data, path)
            self.assertNotIn(b"og:url", held.data, path)
        self.assertEqual(self.sitemap_paths(), [])
        self.assertIn(b"Sitemap:", self.client.get("/robots.txt").data)
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
        self.assertNoindex("/fleet")

    def test_pages_with_unfinished_wording_are_left_out(self):
        save_settings({"about_section1_body": f"{PLACEHOLDER_MARKER} write a short introduction"})
        self.assertNoindex("/about")
        self.assertNotIn("/about", self.sitemap_paths())
        self.assertIndexable("/contact")


class PrivatePages(SearchCase):
    def test_private_workflows_answer_noindex_without_being_blocked(self):
        for path in ["/admin/login", "/admin/", "/operator/login", "/operator/drive", "/driver/join",
                     "/driver/sign-in", "/driver/code", "/driver/status", "/booking", "/booking/ABC123",
                     "/booking/ABC123/review", "/ride/track/ABC123", "/ride/status/ABC123",
                     "/api/geocode?q=kololi", "/healthz", "/no-such-page", "/operators"]:
            self.assertNoindex(path)

    def test_a_post_or_error_on_a_public_address_is_not_indexed(self):
        self.assertNoindex("/fleet/999")
        response = self.client.post("/contact", data={"name": "A"})
        self.assertEqual(response.headers.get("X-Robots-Tag"), "noindex")

    def test_static_files_health_and_robots_need_no_extra_database_work(self):
        statements = []
        listener = lambda *args: statements.append(args[2])  # noqa: E731
        event.listen(db.engine, "before_cursor_execute", listener)
        try:
            static = self.client.get("/static/css/style.css")
            self.assertNotIn("X-Robots-Tag", static.headers)
            self.assertEqual(statements, [])
            self.client.get("/robots.txt")
            self.assertEqual(statements, [])
            self.client.get("/healthz")
            self.assertEqual(len(statements), 1, statements)   # its own SELECT 1
        finally:
            event.remove(db.engine, "before_cursor_execute", listener)


class Listings(SearchCase):
    def test_real_cars_fares_and_drivers_are_discoverable(self):
        driver = self.driver("Awa Jallow")
        car = self.car(driver, description="Tested and serviced before every rental. No contest.")
        fare = self.fare(driver)
        paths = self.sitemap_paths()
        for path in [f"/fleet/{car.id}", f"/rides/{fare.id}", f"/drivers/{driver.id}"]:
            self.assertIn(path, paths)
            page = self.assertIndexable(path).get_data(as_text=True)
            self.assertIn(f'<link rel="canonical" href="{SITE}{path}">', page)
        self.assertIn("Toyota Yaris for rent in The Gambia", self.client.get(f"/fleet/{car.id}").get_data(as_text=True))

    def test_test_only_car_is_not_promoted(self):
        owner = self.driver("Alasan Silla")
        test_car = self.car(owner, model="Corolla — TEST ONLY", mode="both")
        self.assertNoindex(f"/fleet/{test_car.id}")
        # The driver's only car is a test car, so the profile has nothing real yet.
        self.assertNoindex(f"/drivers/{owner.id}")
        paths = self.sitemap_paths()
        self.assertNotIn(f"/fleet/{test_car.id}", paths)
        self.assertNotIn(f"/drivers/{owner.id}", paths)
        # The listing pages themselves stay indexable.
        self.assertIn("/fleet", paths)

        real = self.car(owner, model="Hilux")
        self.assertIndexable(f"/drivers/{owner.id}")
        self.assertIn(f"/fleet/{real.id}", self.sitemap_paths())
        self.assertNoindex(f"/fleet/{test_car.id}")

    def test_other_test_markings(self):
        driver = self.driver("Awa Jallow")
        for description in ["Demo listing", "fictional car for testing", "dummy"]:
            car = self.car(driver, model="Vitz", description=description)
            self.assertNoindex(f"/fleet/{car.id}")
        demo_fare = self.fare(driver, title="Demo airport transfer")
        self.assertNoindex(f"/rides/{demo_fare.id}")
        test_driver = self.driver("Test Driver", phone="+2203000002")
        self.car(test_driver)
        self.assertNoindex(f"/drivers/{test_driver.id}")

    def test_hidden_unapproved_and_taxi_only_listings_stay_out(self):
        pending = self.driver("Pending Driver", status="pending", phone="+2203000003")
        pending_car = self.car(pending)
        driver = self.driver("Awa Jallow")
        hidden = self.car(driver, model="Hidden", active=False)
        taxi = self.car(driver, model="Taxi", mode="taxi")
        paths = self.sitemap_paths()
        for path in [f"/fleet/{pending_car.id}", f"/drivers/{pending.id}", f"/fleet/{hidden.id}", f"/fleet/{taxi.id}"]:
            self.assertNotIn(path, paths)
            self.assertNoindex(path)


if __name__ == "__main__":
    unittest.main()
