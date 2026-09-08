"""Application factory for the Jatta Cars site."""
import os
from datetime import date

from flask import Flask, render_template

from config import Config
from .models import db


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    # SQLite needs the instance folder to exist before it can create the file.
    os.makedirs(os.path.join(app.root_path, "..", "instance"), exist_ok=True)

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
        """Format a price with the configured currency symbol."""
        if value is None:
            return "—"
        return f"{app.config['CURRENCY']}{float(value):,.2f}"

    @app.template_filter("nice_date")
    def nice_date(value):
        if value is None:
            return "—"
        return value.strftime("%a %d %b %Y")

    @app.context_processor
    def inject_globals():
        return {
            "company_name": app.config["COMPANY_NAME"],
            "company_tagline": app.config["COMPANY_TAGLINE"],
            "company_email": app.config["COMPANY_EMAIL"],
            "company_phone": app.config["COMPANY_PHONE"],
            "company_address": app.config["COMPANY_ADDRESS"],
            "locations": app.config["LOCATIONS"],
            "promo_message": app.config["PROMO_MESSAGE"],
            "default_excess": app.config["DEFAULT_EXCESS"],
            "today": date.today().isoformat(),
            "current_year": date.today().year,
        }


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(error):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(error):
        return render_template("500.html"), 500


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create any missing database tables."""
        db.create_all()
        print("Database tables are ready.")
