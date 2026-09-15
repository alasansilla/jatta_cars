"""Machine-level configuration.

Everything here comes from the environment, so the same code runs against local
SQLite and hosted Postgres without edits. Business details, page copy and
booking rules are *not* here — they are edited in the staff area.

Nothing secret belongs in this file. `.env` is git-ignored; `.env.example`
lists the names without the values.
"""
import os
import json
from urllib.parse import quote, unquote

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _load_dotenv(path=None):
    """Read a local .env into os.environ, without adding a dependency.

    Real environment variables always win, so a value set by the host (Vercel,
    a shell export) is never overwritten by a stale local file.
    """
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

def _geoapify_key():
    key = os.environ.get('GEOAPIFY_API_KEY', '')
    if key or os.environ.get('JATTA_ENV', '').lower() in ('production', 'prod'):
        return key
    try:
        with open(os.path.join(BASE_DIR, 'instance', 'geoapify.json')) as handle:
            return json.load(handle).get('key', '')
    except (OSError, ValueError):
        return ''

GEOAPIFY_KEY = _geoapify_key()

DEV_SECRET = "dev-only-not-for-production"

# Shorter than this is worth attacking offline.
MINIMUM_SECRET_LENGTH = 32


def _postgres_driver():
    """Whichever psycopg is installed. psycopg 3 first — it has wheels for
    every Python we support; psycopg2 needs a compiler on some of them."""
    try:
        import psycopg  # noqa: F401

        return "psycopg"
    except ImportError:
        return "psycopg2"


def _normalise_credentials(url):
    """Percent-encode the user and password in a database URL.

    Supabase generates passwords containing characters that mean something in a
    URL — `@`, `/`, `?`, `#`, `:` — and the dashboard shows the password raw. A
    raw `@` makes the host look like part of the password and the connection
    string fails to parse, which is the "malformed database URL" a deployment
    dies on.

    Splitting on the *last* `@` finds the real host separator even when the
    password contains one. Each part is then decoded and re-encoded, so a
    password that was already percent-encoded is left as it is rather than being
    double-encoded, and a raw one is fixed.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url

    userinfo, _, hostpart = rest.rpartition("@")
    user, colon, password = userinfo.partition(":")

    safe_user = quote(unquote(user), safe="")
    if colon:
        userinfo = f"{safe_user}:{quote(unquote(password), safe='')}"
    else:
        userinfo = safe_user

    return f"{scheme}://{userinfo}@{hostpart}"


def _database_uri():
    """Normalise whatever the host hands us into a SQLAlchemy URL.

    Supabase (and most providers) publish `postgresql://…`. SQLAlchemy needs an
    explicit driver, and the older `postgres://` scheme it no longer accepts at
    all, so both are rewritten here rather than in five different runbooks.
    """
    url = os.environ.get("JATTA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:///" + os.path.join(BASE_DIR, "instance", "jatta.db")

    url = url.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = _normalise_credentials(url)
        url = f"postgresql+{_postgres_driver()}://" + url[len("postgresql://"):]
    return url


def _engine_options(uri):
    """Connection-pool settings appropriate to where we are running.

    Supabase offers three ways in. The transaction pooler (port 6543) is the one
    for serverless: each invocation is short-lived and Supavisor owns the real
    connections. Holding a SQLAlchemy pool on top of it would just pin
    connections a frozen function is not using, so we use NullPool there and let
    the pooler pool.

    Transaction mode also cannot carry prepared statements across a checkout —
    Supabase's documentation is explicit about it. psycopg 3 prepares statements
    automatically after a few executions, so it is told not to; psycopg2 binds
    parameters client-side and needs nothing.
    """
    # Bound parameters carry phone numbers, addresses and booking details. They
    # are kept out of SQLAlchemy's log and error messages whatever the log level.
    if uri.startswith("sqlite"):
        return {"hide_parameters": True}

    options = {
        "hide_parameters": True,
        "pool_pre_ping": True,  # a pooled connection may have been closed under us
        "connect_args": {
            "connect_timeout": int(os.environ.get("JATTA_DB_CONNECT_TIMEOUT", "10")),
            "application_name": os.environ.get("JATTA_APP_NAME", "jatta-cars"),
        },
    }

    transaction_pooler = ":6543" in uri or os.environ.get("JATTA_DB_POOL") == "none"
    if transaction_pooler:
        from sqlalchemy.pool import NullPool

        options["poolclass"] = NullPool
        if "+psycopg" in uri and "+psycopg2" not in uri:
            # psycopg 3 only. Transaction pooling cannot carry a prepared
            # statement between checkouts, and Supavisor errors if you try.
            options["connect_args"]["prepare_threshold"] = None
    else:
        options["pool_size"] = int(os.environ.get("JATTA_DB_POOL_SIZE", "5"))
        options["max_overflow"] = int(os.environ.get("JATTA_DB_MAX_OVERFLOW", "5"))
        options["pool_recycle"] = int(os.environ.get("JATTA_DB_POOL_RECYCLE", "1800"))

    if os.environ.get("JATTA_DB_SSLMODE"):
        options["connect_args"]["sslmode"] = os.environ["JATTA_DB_SSLMODE"]

    return options


def _is_production_env():
    return os.environ.get("JATTA_ENV", "development").strip().lower() in ("production", "prod")


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


class Config:
    ENV_NAME = os.environ.get("JATTA_ENV", "development")

    SECRET_KEY = os.environ.get("JATTA_SECRET_KEY", DEV_SECRET)

    # --- Sessions -----------------------------------------------------------
    #
    # The cookie is signed, not encrypted, and holds who is signed in. HttpOnly
    # keeps page scripts away from it; SameSite=Lax stops other sites riding it
    # on a POST; Secure keeps it off plain HTTP wherever the site is served over
    # HTTPS. A driver who signs in with their phone stays signed in for a
    # fortnight, because signing in again costs them a text message.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _is_production_env()
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * _int_env("JATTA_DRIVER_SESSION_DAYS", 14)

    # How many proxies sit in front of the app and append to X-Forwarded-For.
    # Render and most platforms add exactly one. Leave 0 when the app is reached
    # directly: trusting a header nobody strips would let anyone pick the IP
    # address that sign-in rate limits count against.
    TRUSTED_PROXIES = _int_env("JATTA_TRUSTED_PROXIES", 0)

    # --- Search engines -----------------------------------------------------
    #
    # The one address canonical links and the sitemap use, whatever Host header
    # a request arrived with. See app/seo.py.
    PUBLIC_SITE_URL = os.environ.get("JATTA_PUBLIC_SITE_URL", "https://gogo-taxi.com").strip().rstrip("/")
    SITEMAP_CACHE_SECONDS = _int_env("JATTA_SITEMAP_CACHE_SECONDS", 600)

    # --- Driver sign-in by text message ------------------------------------
    #
    # See app/sms.py. Development defaults to the fake transport, which sends
    # nothing and writes codes to a git-ignored file under instance/. Production
    # has no default: until a real provider is configured, phone sign-in says it
    # is unavailable instead of pretending to send codes.
    SMS_BACKEND = os.environ.get(
        "JATTA_SMS_BACKEND", "").strip().lower()
    SMS_FAKE_OUTBOX = os.environ.get(
        "JATTA_SMS_FAKE_OUTBOX", os.path.join(BASE_DIR, "instance", "dev_sms_outbox.jsonl"))
    SMS_TIMEOUT = _int_env("JATTA_SMS_TIMEOUT", 10)
    SMS_WEBOTP_DOMAIN = os.environ.get("JATTA_SMS_WEBOTP_DOMAIN", "").strip()
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    TWILIO_FROM = os.environ.get("TWILIO_FROM", "").strip()

    SMS_CODE_TTL_SECONDS = _int_env("JATTA_SMS_CODE_TTL_SECONDS", 600)
    SMS_CODE_MAX_ATTEMPTS = _int_env("JATTA_SMS_CODE_MAX_ATTEMPTS", 5)
    SMS_RESEND_SECONDS = _int_env("JATTA_SMS_RESEND_SECONDS", 60)
    SMS_MAX_PER_NUMBER_HOUR = _int_env("JATTA_SMS_MAX_PER_NUMBER_HOUR", 5)
    SMS_MAX_PER_NUMBER_DAY = _int_env("JATTA_SMS_MAX_PER_NUMBER_DAY", 10)
    SMS_MAX_PER_IP_HOUR = _int_env("JATTA_SMS_MAX_PER_IP_HOUR", 10)
    SMS_MAX_PER_DAY = _int_env("JATTA_SMS_MAX_PER_DAY", 300)
    SMS_WRONG_CODES_PER_IP_HOUR = _int_env("JATTA_SMS_WRONG_CODES_PER_IP_HOUR", 30)
    # How long after proving a number a new driver has to type their name.
    PHONE_VERIFIED_SECONDS = _int_env("JATTA_PHONE_VERIFIED_SECONDS", 900)
    # Changing a sign-in number needs a sign-in at least this recent.
    RECENT_SIGN_IN_SECONDS = _int_env("JATTA_RECENT_SIGN_IN_SECONDS", 900)

    # Log at start-up when a production database schema is not current. The
    # schema itself is only ever changed by `python -m migrations`, which runs
    # before the web workers (see render.yaml).
    SCHEMA_REPORT_ON_START = _is_production_env()

    # --- On-demand rides -----------------------------------------------------
    # How long a chosen driver has to accept before the customer is asked to
    # choose again.
    DRIVER_ACCEPT_SECONDS = _int_env("JATTA_DRIVER_ACCEPT_SECONDS", 120)

    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(SQLALCHEMY_DATABASE_URI)

    # Image uploads.
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB per upload
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

    # Where uploaded pictures live. "local" writes into app/static/uploads,
    # which is fine on a normal server and useless on a serverless host where
    # the filesystem is read-only and thrown away. "supabase" is durable.
    STORAGE_BACKEND = os.environ.get("JATTA_STORAGE", "").strip().lower() or None
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "media")
    # Browsers and the CDN may hold an uploaded image this long. Filenames carry
    # a random suffix, so a changed picture is a new URL and never a stale one.
    UPLOAD_CACHE_SECONDS = int(os.environ.get("JATTA_UPLOAD_CACHE_SECONDS", "31536000"))

    # --- Maps and route planning --------------------------------------------
    #
    # Every endpoint is configurable, so the site is not tied to any one
    # provider and can move without a code change.
    #
    # Tiles default to OpenStreetMap's public raster service. It needs no key,
    # but it does have a usage policy: point JATTA_MAP_TILE_URL at your own or a
    # paid provider before this carries real traffic. A key embedded in a tile
    # URL is fetched by the browser and is therefore public by nature — put
    # keyed tile services behind your own proxy rather than in this variable.
    MAP_ENABLED = os.environ.get("JATTA_MAP_ENABLED", "1").strip().lower() \
        not in ("0", "false", "no", "off")
    MAP_TILE_URL = os.environ.get(
        "JATTA_MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png").strip()
    MAP_ATTRIBUTION = os.environ.get(
        "JATTA_MAP_ATTRIBUTION", "&copy; OpenStreetMap contributors").strip()
    MAP_MAX_ZOOM = int(os.environ.get("JATTA_MAP_MAX_ZOOM", "19"))
    # Roughly the middle of The Gambia; used only until a point is known.
    MAP_CENTRE_LAT = float(os.environ.get("JATTA_MAP_CENTRE_LAT", "13.4432"))
    MAP_CENTRE_LNG = float(os.environ.get("JATTA_MAP_CENTRE_LNG", "-15.3101"))
    MAP_ZOOM = int(os.environ.get("JATTA_MAP_ZOOM", "8"))

    # Geocoding and routing are deliberately UNSET by default.
    #
    # The public Nominatim and OSRM demo servers forbid this sort of use, so
    # defaulting to them would both breach someone's policy and post a
    # customer's pickup address to a third party nobody chose. Unset means the
    # site shows its manual-quote fallback, which is honest rather than broken.
    #
    # Both are URL templates. The geocoder gets {query}, and may use {limit} and
    # {country}; the router gets {lat1} {lon1} {lat2} {lon2}, or {coords} for the
    # OSRM-style "lon,lat;lon,lat" pair. A {key} placeholder is filled from the
    # matching API key variable, which never reaches the browser.
    GEOCODER_URL = os.environ.get("JATTA_GEOCODER_URL", (
        "https://api.geoapify.com/v1/geocode/search?text={query}&filter=countrycode:{country}&limit={limit}&apiKey={key}"
        if GEOAPIFY_KEY else "")).strip()
    GEOCODER_API_KEY = os.environ.get("JATTA_GEOCODER_API_KEY", GEOAPIFY_KEY)
    # Turns a point the traveller tapped on the map (or their own location)
    # into a readable address. Gets {lat} and {lon}; uses GEOCODER_API_KEY.
    REVERSE_GEOCODER_URL = os.environ.get("JATTA_REVERSE_GEOCODER_URL", (
        "https://api.geoapify.com/v1/geocode/reverse?lat={lat}&lon={lon}&format=json&apiKey={key}"
        if GEOAPIFY_KEY else "")).strip()
    GEOCODER_COUNTRY = os.environ.get("JATTA_GEOCODER_COUNTRY", "gm").strip()
    ROUTER_URL = os.environ.get("JATTA_ROUTER_URL", (
        "https://api.geoapify.com/v1/routing?waypoints={lat1},{lon1}|{lat2},{lon2}&mode=drive&apiKey={key}"
        if GEOAPIFY_KEY else "")).strip()
    ROUTER_API_KEY = os.environ.get("JATTA_ROUTER_API_KEY", GEOAPIFY_KEY)
    ROUTING_TIMEOUT = int(os.environ.get("JATTA_ROUTING_TIMEOUT", "8"))
    ROUTING_USER_AGENT = os.environ.get("JATTA_ROUTING_USER_AGENT", "jatta-cars")

    @property
    def is_production(self):
        return self.ENV_NAME.lower() in ("production", "prod")


def check_production_config(app):
    """Every way a production deployment can be misconfigured.

    Returns a list of problems. In production the app refuses to start if there
    are any — it fails closed. A half-configured deployment is worse than none:
    it takes real bookings from real customers and then loses them with the
    container, or signs staff sessions with a key that is on GitHub.
    """
    problems = []
    if app.config.get("ENV_NAME", "").lower() not in ("production", "prod"):
        return problems

    secret = app.config.get("SECRET_KEY")
    if not isinstance(secret, (str, bytes)) or not str(secret).strip():
        # An unset variable arrives as "" and whitespace is no better: Flask
        # would happily sign sessions with it and every deployment would share
        # the same empty key.
        problems.append(
            "JATTA_SECRET_KEY is empty. Set it to a long random value: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    elif str(secret).strip() == DEV_SECRET:
        problems.append(
            "JATTA_SECRET_KEY is still the development default. Set it to a long "
            "random value; anyone who knows the default can forge a staff session."
        )
    elif len(str(secret).strip()) < MINIMUM_SECRET_LENGTH:
        problems.append(
            f"JATTA_SECRET_KEY is only {len(str(secret).strip())} characters. Use at "
            f"least {MINIMUM_SECRET_LENGTH}; a short key can be brute-forced offline."
        )
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        problems.append(
            "JATTA_DATABASE_URL is not set, so this would run on SQLite. On a "
            "serverless host that file is discarded between invocations, so every "
            "booking taken would be lost."
        )

    backend = app.config.get("STORAGE_BACKEND") or (
        "supabase" if app.config.get("SUPABASE_URL") else "local")
    if backend != "supabase":
        problems.append(
            "Uploads would go to the local filesystem, which a serverless host "
            "throws away. Set JATTA_STORAGE=supabase and the SUPABASE_* variables."
        )
    elif not app.config.get("SUPABASE_SERVICE_ROLE_KEY"):
        problems.append(
            "SUPABASE_SERVICE_ROLE_KEY is missing, so no picture could be uploaded."
        )
    elif not app.config.get("SUPABASE_URL"):
        problems.append("SUPABASE_URL is missing.")

    # The fake text transport never delivers anything. In production that would
    # make driver sign-in silently impossible, so it is refused outright. An
    # unset transport is allowed: phone sign-in then reports itself unavailable,
    # which fails closed without taking the rest of the site down.
    if (app.config.get("SMS_BACKEND") or "").strip().lower() == "fake":
        problems.append(
            "JATTA_SMS_BACKEND=fake cannot run in production: it never sends a text. "
            "Configure a real SMS provider, or leave JATTA_SMS_BACKEND unset.")

    return problems
