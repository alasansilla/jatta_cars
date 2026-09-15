"""What search engines may index, and the sitemap that tells them where it is.

The rule is one place: `indexable()` decides, per response, whether a page may
appear in search results. Everything else gets an `X-Robots-Tag: noindex`
header. Nothing is hidden with robots.txt, because a crawler that is refused a
page never sees its noindex and can still list the bare address; robots.txt
only keeps crawlers off the JSON lookups that spend map-provider quota.

A page is indexable only when all of these hold:

* it is one of the public pages below, fetched with GET, answered 200 as HTML;
* the site is published (the holding page and the staff preview are not);
* it contains no "[TBC]" wording still waiting for the owner to fill in;
* for a car, fare or driver page, the listing is not marked as a test —
  "TEST ONLY", "demo", "fictional" and the like in its name or description —
  and a driver's profile has something real to book.

The sitemap lists exactly the pages that pass, by asking the application for
each candidate rather than repeating the rules, so the two cannot disagree.
Canonical links and sitemap addresses use PUBLIC_SITE_URL, never the Host
header a request happened to arrive with.
"""
import re
import time
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Blueprint, Response, current_app, request

from .models import Operator, OperatorFare, Vehicle, db
from .settings import PLACEHOLDER_MARKER, current_settings

bp = Blueprint("seo", __name__)

# Landing pages, in the order the sitemap lists them.
PAGES = {
    "public.index": "/",
    "dispatch.ride": "/ride",
    "public.rides": "/rides",
    "public.fleet": "/fleet",
    "dispatch.drivers": "/drivers",
    "public.about": "/about",
    "public.contact": "/contact",
}
# Pages about one listing; whether each may be indexed depends on the listing.
LISTINGS = ("public.vehicle_detail", "public.ride_detail", "dispatch.driver_profile")
INDEXABLE = frozenset(PAGES) | frozenset(LISTINGS)

# Whole words only, so "tested" or "contest" in a real description do not count.
TEST_MARKER = re.compile(r"\b(test|testing|demo|dummy|fictional|fake)\b", re.IGNORECASE)

MAX_SITEMAP_URLS = 1000
_sitemap_cache = {}


def origin():
    return current_app.config.get("PUBLIC_SITE_URL", "https://gogo-taxi.com").rstrip("/")


def marked_as_test(*texts):
    return any(text and TEST_MARKER.search(str(text)) for text in texts)


def vehicle_is_test(vehicle):
    driver = vehicle.operator
    return marked_as_test(vehicle.make, vehicle.model, vehicle.description) or bool(
        driver and marked_as_test(driver.name, driver.contact_name))


def fare_is_test(fare):
    driver = fare.operator
    return marked_as_test(fare.title, fare.from_location, fare.to_location,
                          fare.vehicle_class, fare.notes) or bool(
        driver and marked_as_test(driver.name, driver.contact_name))


def driver_has_real_listing(driver):
    """A profile worth finding: a real car to rent or a real fare to book.

    A driver whose only cars are test cars is left out even if they have a fare,
    because the car a customer would ride in is not real yet.
    """
    if marked_as_test(driver.name, driver.contact_name):
        return False
    cars = [car for car in driver.vehicles if car.is_active]
    if cars and all(vehicle_is_test(car) for car in cars):
        return False
    return (any(car.is_bookable and not vehicle_is_test(car) for car in cars)
            or any(fare.is_bookable and not fare_is_test(fare) for fare in driver.fares))


def _listing_is_real(endpoint, args):
    # The view has just loaded this row, so the session returns it without a query.
    if endpoint == "public.vehicle_detail":
        vehicle = db.session.get(Vehicle, args.get("vehicle_id"))
        return bool(vehicle and vehicle.is_bookable and not vehicle_is_test(vehicle))
    if endpoint == "public.ride_detail":
        fare = db.session.get(OperatorFare, args.get("fare_id"))
        return bool(fare and fare.is_bookable and not fare_is_test(fare))
    if endpoint == "dispatch.driver_profile":
        driver = db.session.get(Operator, args.get("operator_id"))
        return bool(driver and driver.is_approved and driver_has_real_listing(driver))
    return True


def indexable(response):
    """Cheap checks first: most responses are ruled out without the database."""
    if request.endpoint not in INDEXABLE or request.method not in ("GET", "HEAD"):
        return False
    if response.status_code != 200 or response.mimetype != "text/html" or response.is_streamed:
        return False
    # Already read (and cached for this request) by the page's publish check.
    if not current_settings()["site_live"]:
        return False
    if PLACEHOLDER_MARKER.encode() in response.get_data():
        return False
    return _listing_is_real(request.endpoint, request.view_args or {})


@bp.after_app_request
def robots_header(response):
    # Stylesheets, scripts and car photos carry no page of their own to index.
    if request.endpoint == "static":
        return response
    if not indexable(response):
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@bp.app_context_processor
def canonical():
    endpoint = request.endpoint if request else None
    if endpoint not in INDEXABLE:
        return {"seo_canonical": None}
    # The path without its query string: /fleet?seats=4 is the page /fleet.
    return {"seo_canonical": origin() + request.path}


@bp.get("/robots.txt")
def robots():
    body = ("User-agent: *\n"
            "Disallow: /api/\n"
            "\n"
            f"Sitemap: {origin()}/sitemap.xml\n")
    return Response(body, mimetype="text/plain")


def _candidates():
    from .public import _published_fares, bookable_vehicles

    paths = list(PAGES.values())
    paths += [f"/fleet/{vehicle.id}" for vehicle in bookable_vehicles().order_by(Vehicle.id)]
    paths += [f"/rides/{fare.id}" for fare in sorted(_published_fares(), key=lambda fare: fare.id)]
    paths += [f"/drivers/{driver.id}" for driver in
              Operator.query.filter_by(status="approved").order_by(Operator.id)]
    return paths


def _indexable_paths():
    if not current_settings()["site_live"]:
        return []
    paths = _candidates()
    if len(paths) > MAX_SITEMAP_URLS:
        current_app.logger.warning("Sitemap limited to %s of %s pages", MAX_SITEMAP_URLS, len(paths))
        paths = paths[:MAX_SITEMAP_URLS]
    # Ask the application itself, as an anonymous visitor, so the sitemap applies
    # exactly the rules the pages do.
    client = current_app.test_client()
    return [path for path in paths if _answers_indexable(client, path)]


def _answers_indexable(client, path):
    response = client.get(path)
    return response.status_code == 200 and "X-Robots-Tag" not in response.headers


@bp.get("/sitemap.xml")
def sitemap():
    lifetime = current_app.config.get("SITEMAP_CACHE_SECONDS", 600)
    base = origin()
    key = (id(current_app._get_current_object()), base)
    cached = _sitemap_cache.get(key)
    if lifetime and cached and cached[0] > time.monotonic():
        body = cached[1]
    else:
        root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for path in _indexable_paths():
            SubElement(SubElement(root, "url"), "loc").text = base + path
        body = tostring(root, encoding="utf-8", xml_declaration=True)
        if lifetime:
            _sitemap_cache[key] = (time.monotonic() + lifetime, body)
    response = Response(body, mimetype="application/xml")
    response.headers["Cache-Control"] = "public, max-age=600" if lifetime else "no-store"
    return response
