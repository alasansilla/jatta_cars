"""Application factory for the Jatta Cars site."""
import os
import re
import secrets
from datetime import date

from flask import Flask, render_template, request, session
from markupsafe import Markup, escape

from config import Config
from .models import db
from .settings import current_settings, fill_tokens


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    # SQLite needs the instance folder to exist before it can create the file.
    os.makedirs(os.path.join(app.root_path, "..", "instance"), exist_ok=True)
    # Uploaded images are served straight out of the static folder.
    app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)

    from .public import bp as public_bp
    from .admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    register_template_helpers(app)
    register_error_handlers(app)
    register_cli(app)

    return app


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
