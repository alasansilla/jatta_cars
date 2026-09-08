"""Application factory for the Jatta Cars site."""
import os
import re
import secrets
from datetime import date

from flask import Flask, render_template, request, session
from markupsafe import Markup, escape

from config import Config, check_production_config
from .models import db
from .settings import current_settings, fill_tokens
from .storage import media_url


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")

    # Only touch the filesystem when we are actually going to use it. A
    # serverless host gives us a read-only tree, and a mkdir there is an
    # immediate crash on a path that otherwise never needs writing.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        _ensure_dir(os.path.join(app.root_path, "..", "instance"))
    if (app.config.get("STORAGE_BACKEND") or
            ("supabase" if app.config.get("SUPABASE_URL") else "local")) == "local":
        _ensure_dir(app.config["UPLOAD_FOLDER"])

    # Fail closed. In production every one of these is a reason not to serve:
    # a guessable secret key lets anyone mint a staff session, SQLite loses the
    # bookings, and missing storage credentials lose the pictures. Better a
    # deployment that will not start than one that quietly drops customer data.
    problems = check_production_config(app)
    if problems:
        for problem in problems:
            app.logger.error("Configuration: %s", problem)
        raise RuntimeError(
            "Refusing to start: "
            + " ".join(f"({n}) {p}" for n, p in enumerate(problems, 1))
        )

    db.init_app(app)

    from .public import bp as public_bp
    from .admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    register_template_helpers(app)
    register_error_handlers(app)
    register_health(app)
    register_cli(app)

    return app


def register_health(app):
    """A health check the host can gate a deployment on.

    Registered on the application rather than the public blueprint so it is not
    caught by the draft-mode holding page: the host has to be able to tell a
    working deployment from a broken one whether or not the site is published.

    It touches the database on purpose. A deployment with the wrong
    JATTA_DATABASE_URL should fail its health check and be rolled back, not go
    live and start losing bookings.
    """
    from sqlalchemy import text

    @app.get("/healthz")
    def healthz():
        from flask import jsonify

        try:
            db.session.execute(text("SELECT 1"))
            db.session.commit()
        except Exception as error:  # noqa: BLE001 — report whatever went wrong
            db.session.rollback()
            app.logger.error("Health check failed: %s", error)
            return jsonify({
                "status": "unhealthy",
                "database": "unreachable",
                # The class name says enough to diagnose; the message could
                # carry the connection string, so it is not returned.
                "error": type(error).__name__,
            }), 503

        return jsonify({
            "status": "ok",
            "database": "ok",
            "storage": (app.config.get("STORAGE_BACKEND")
                        or ("supabase" if app.config.get("SUPABASE_URL") else "local")),
        }), 200


def _ensure_dir(path):
    """Create a directory, tolerating a read-only filesystem."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


def register_template_helpers(app):
    @app.template_filter("money")
    def money(value):
        """Format a price with the currency symbol set in the staff area."""
        if value is None:
            return "—"
        return f"{current_settings()['currency']}{float(value):,.2f}"

    @app.template_filter("nice_date")
    def nice_date(value):
        if value is None:
            return "—"
        return value.strftime("%a %d %b %Y")

    @app.template_filter("paragraphs")
    def paragraphs(text):
        """Render an editable textarea as paragraphs, splitting on blank lines."""
        if not text:
            return ""
        blocks = [block.strip() for block in re.split(r"\n\s*\n", str(text)) if block.strip()]
        return Markup("".join(f"<p>{escape(block)}</p>" for block in blocks))

    @app.template_filter("highlight")
    def highlight(text, phrase):
        """Colour the first occurrence of `phrase` inside a heading."""
        if not text:
            return ""
        if not phrase or phrase not in text:
            return escape(text)
        head, _, tail = str(text).partition(phrase)
        return Markup(f"{escape(head)}<em>{escape(phrase)}</em>{escape(tail)}")

    app.add_template_global(media_url, "media_url")

    @app.template_filter("approx")
    def approx(value):
        """Rough foreign-currency equivalent, when a rate has been entered.

        Nothing fetches a live rate: staff type one in and it is shown as an
        approximation, never as the price being charged.
        """
        settings = current_settings()
        if value is None:
            return ""
        parts = []
        for symbol, key in (("\u20ac", "fx_eur_rate"), ("\u00a3", "fx_gbp_rate")):
            rate = settings.get(key) or 0
            if rate > 0:
                parts.append(f"{symbol}{float(value) / float(rate):,.0f}")
        return "\u2248 " + " / ".join(parts) if parts else ""

    @app.template_filter("initial")
    def initial(text):
        """First letter of a name, ignoring the placeholder marker."""
        from .settings import PLACEHOLDER_MARKER

        cleaned = str(text or "").replace(PLACEHOLDER_MARKER, "").strip()
        for char in cleaned:
            if char.isalnum():
                return char.upper()
        return "\u2013"

    @app.template_filter("tokens")
    def tokens(text, **extra):
        return fill_tokens(text, current_settings(), **extra)

    @app.context_processor
    def inject_globals():
        settings = current_settings()
        return {
            "site": settings,
            # Staff signed in => the inline editor is offered, but only on the
            # public pages it can actually edit.
            "show_editbar": bool(session.get("admin_id"))
            and not (request.endpoint or "").startswith("admin."),
            "csrf_token": csrf_token(),
            # Used often enough across the templates to be worth the short names.
            "company_name": settings["company_name"],
            "company_tagline": settings["company_tagline"],
            "company_email": settings["company_email"],
            "company_phone": settings["company_phone"],
            "company_address": settings["company_address"],
            "locations": settings["locations"],
            "promo_message": settings["promo_message"],
            # True when at least one exchange rate has been entered.
            "approx_prices": bool(
                (settings.get("fx_eur_rate") or 0) > 0
                or (settings.get("fx_gbp_rate") or 0) > 0
            ),
            "today": date.today().isoformat(),
            "current_year": date.today().year,
        }


def csrf_token():
    """A per-session token, checked on every state-changing staff request."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(error):
        return render_template("404.html"), 404

    @app.errorhandler(413)
    def too_large(error):
        return render_template("413.html"), 413

    @app.errorhandler(500)
    def server_error(error):
        return render_template("500.html"), 500


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create any missing database tables."""
        db.create_all()
        print("Database tables are ready.")
