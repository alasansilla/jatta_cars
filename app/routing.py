"""Geocoding and route planning, without committing to a provider.

Both services are URL templates supplied by configuration, so moving from one
provider to another is an environment change rather than a code change. Nothing
here knows what a "Mapbox" or a "Google" is.

Two rules shape this module:

* **Every call happens on the server.** The browser talks to this application,
  and this application talks to the provider. An API key therefore never reaches
  a page, and a provider never sees the visitor's IP address.
* **Unavailable is a normal answer.** Geocoding and routing are unset by
  default, and a provider can time out. Callers get `None`, the customer gets a
  clear "we will confirm the fare" message, and no distance or price is ever
  guessed at.

Query text is a customer's home or hotel address, so it is never written to a
log — only whether a lookup succeeded.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from flask import current_app


class RoutingUnavailable(RuntimeError):
    """The provider is not configured, not reachable, or gave nothing usable."""


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lng: float

    def as_dict(self):
        return {"label": self.label, "lat": self.lat, "lng": self.lng}


@dataclass(frozen=True)
class Route:
    distance_m: int
    duration_s: int
    provider: str
    geometry: list = field(default_factory=list)

    @property
    def distance_km(self):
        return round(self.distance_m / 1000, 1)

    @property
    def duration_minutes(self):
        return int(round(self.duration_s / 60))

    def as_dict(self):
        return {
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "distance_km": self.distance_km,
            "duration_minutes": self.duration_minutes,
            "provider": self.provider,
            "geometry": self.geometry,
        }


def _config(key, default=None):
    return current_app.config.get(key, default)


def geocoding_available():
    return bool(_config("MAP_ENABLED") and _config("GEOCODER_URL"))


def routing_available():
    return bool(_config("MAP_ENABLED") and _config("ROUTER_URL"))


def _provider_name(url):
    """A host name, for recording which service produced a number."""
    try:
        return urllib.parse.urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"


def _fetch(url, api_key, key_in_url=False):
    """GET and parse JSON. Raises RoutingUnavailable on anything unusable."""
    headers = {
        "User-Agent": _config("ROUTING_USER_AGENT", "jatta-cars"),
        "Accept": "application/json",
    }
    # A key goes in a header unless the template asked for it inline. Either
    # way it is added here, on the server, and never rendered into a page.
    if api_key and not key_in_url:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_config("ROUTING_TIMEOUT", 8)) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise RoutingUnavailable(f"provider returned {error.code}") from error
    except urllib.error.URLError as error:
        raise RoutingUnavailable(f"provider unreachable: {error.reason}") from error
    except (ValueError, TimeoutError) as error:
        raise RoutingUnavailable(f"provider gave an unreadable answer: {error}") from error


def _fill(template, values):
    """Substitute {placeholders}, URL-encoding each value."""
    filled = template
    for name, value in values.items():
        token = "{" + name + "}"
        if token in filled:
            filled = filled.replace(token, urllib.parse.quote(str(value), safe=""))
    return filled


def _as_places(payload, limit):
    """Read a geocoding response without assuming one provider's shape.

    Handles the Nominatim-style list, the GeoJSON FeatureCollection that most
    others return, and a plain {results: [...]} envelope.
    """
    candidates = payload
    if isinstance(payload, dict):
        for key in ("features", "results", "items", "data"):
            if isinstance(payload.get(key), list):
                candidates = payload[key]
                break
        else:
            candidates = []
    if not isinstance(candidates, list):
        return []

    places = []
    for entry in candidates[:limit]:
        if not isinstance(entry, dict):
            continue
        label = (entry.get("display_name") or entry.get("name")
                 or entry.get("label") or entry.get("formatted")
                 or (entry.get("properties") or {}).get("formatted")
                 or (entry.get("properties") or {}).get("label")
                 or (entry.get("properties") or {}).get("name"))

        lat = lng = None
        if entry.get("lat") is not None and (entry.get("lon") is not None
                                             or entry.get("lng") is not None):
            lat = entry.get("lat")
            lng = entry.get("lon", entry.get("lng"))
        else:
            geometry = entry.get("geometry") or {}
            coords = geometry.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lng, lat = coords[0], coords[1]  # GeoJSON is lon, lat
            else:
                centre = entry.get("center") or entry.get("centre") or {}
                if isinstance(centre, dict):
                    lat, lng = centre.get("lat"), centre.get("lng", centre.get("lon"))
                elif isinstance(centre, (list, tuple)) and len(centre) >= 2:
                    lng, lat = centre[0], centre[1]

        try:
            place = Place(label=str(label or "Unnamed place"),
                          lat=float(lat), lng=float(lng))
        except (TypeError, ValueError):
            continue
        if math.isfinite(place.lat) and math.isfinite(place.lng) and abs(place.lat) <= 90 and abs(place.lng) <= 180:
            places.append(place)
    return places


def geocode(query, limit=5):
    """Turn typed text into candidate places. Returns [] when unavailable."""
    query = (query or "").strip()
    if not query or not geocoding_available():
        return []

    url = _fill(_config("GEOCODER_URL"), {
        "query": query,
        "q": query,
        "limit": limit,
        "country": _config("GEOCODER_COUNTRY", ""),
        "key": _config("GEOCODER_API_KEY", ""),
    })
    payload = _fetch(url, _config("GEOCODER_API_KEY"), "{key}" in _config("GEOCODER_URL"))
    return _as_places(payload, limit)


def _as_route(payload, provider):
    """Read a routing response. OSRM's shape, or anything close to it."""
    candidate = None
    if isinstance(payload, dict):
        for key in ("routes", "trips", "paths", "features"):
            entries = payload.get(key)
            if isinstance(entries, list) and entries:
                candidate = entries[0]
                break
        if candidate is None and "distance" in payload:
            candidate = payload
    if not isinstance(candidate, dict):
        raise RoutingUnavailable("no route in the provider's answer")

    summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else {}
    properties = candidate.get("properties") if isinstance(candidate.get("properties"), dict) else {}
    source = {**properties, **summary, **candidate}

    distance = source.get("distance") or source.get("distanceMeters") or source.get("length")
    duration = (source.get("duration") or source.get("time")
                or source.get("durationSeconds") or source.get("travelTime"))

    try:
        distance_m = int(round(float(distance)))
        duration_s = int(round(float(duration)))
    except (TypeError, ValueError, OverflowError):
        raise RoutingUnavailable("the provider gave no distance or duration")

    if distance_m <= 0 or duration_s < 0:
        raise RoutingUnavailable("the provider gave an impossible distance")

    geometry = candidate.get("geometry") or {}
    points = []
    if isinstance(geometry, dict):
        coordinates = geometry.get("coordinates", [])
        if geometry.get("type") == "MultiLineString":
            coordinates = [point for line in coordinates for point in line]
        if geometry.get("type") in ("LineString", "MultiLineString"):
            for point in coordinates[:20000]:
                try:
                    lng, lat = float(point[0]), float(point[1])
                    if not (math.isfinite(lat) and math.isfinite(lng) and abs(lat) <= 90 and abs(lng) <= 180):
                        raise ValueError()
                    points.append([lat, lng])
                except (TypeError, ValueError, IndexError):
                    raise RoutingUnavailable("invalid route geometry")
    return Route(distance_m=distance_m, duration_s=duration_s, provider=provider, geometry=points)


def route(origin, destination):
    """Distance and duration between two points, or None when unavailable.

    `origin` and `destination` are (lat, lng) pairs.
    """
    if not routing_available():
        return None

    template = _config("ROUTER_URL")
    lat1, lon1 = float(origin[0]), float(origin[1])
    lat2, lon2 = float(destination[0]), float(destination[1])

    url = _fill(template, {
        "lat1": lat1, "lon1": lon1, "lng1": lon1,
        "lat2": lat2, "lon2": lon2, "lng2": lon2,
        # OSRM-style "lon,lat;lon,lat". Built here so the template stays short.
        "coords": f"{lon1},{lat1};{lon2},{lat2}",
        "key": _config("ROUTER_API_KEY", ""),
    })
    payload = _fetch(url, _config("ROUTER_API_KEY"), "{key}" in template)
    return _as_route(payload, _provider_name(template))


def map_settings():
    """What the browser needs to draw a map. Contains no secret."""
    return {
        "enabled": bool(_config("MAP_ENABLED")),
        "tile_url": _config("MAP_TILE_URL", ""),
        "attribution": _config("MAP_ATTRIBUTION", ""),
        "max_zoom": _config("MAP_MAX_ZOOM", 19),
        "centre": [_config("MAP_CENTRE_LAT", 0.0), _config("MAP_CENTRE_LNG", 0.0)],
        "zoom": _config("MAP_ZOOM", 8),
        "geocoding": geocoding_available(),
        "routing": routing_available(),
    }
