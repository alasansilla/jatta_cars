"""Create or re-password a staff account, without the password touching disk.

The password is typed at a prompt, never passed as an argument and never put in
an environment variable, so it does not end up in shell history, in `ps`, or in
a deployment log. Only the hash is stored.

    python tools/create_admin.py                 # against JATTA_DATABASE_URL
    python tools/create_admin.py --username awa

Point it at production by exporting JATTA_DATABASE_URL first. It prints which
database it is talking to (without the password) and asks before changing an
existing account.
"""
import argparse
import getpass
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.models import AdminUser, db  # noqa: E402

MINIMUM = 12


def describe(uri):
    """The database, with any credentials taken out."""
    scheme, _, rest = uri.partition("://")
    if "@" not in rest:
        return uri
    return f"{scheme}://…@{rest.rpartition('@')[2]}"


def suggest():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("JATTA_ADMIN_USER", "admin"))
    parser.add_argument("--suggest", action="store_true",
                        help="print a strong password and exit, so you can save it first")
    args = parser.parse_args()

    if args.suggest:
        print(suggest())
        return 0

    app = create_app()
    with app.app_context():
        print(f"Database: {describe(app.config['SQLALCHEMY_DATABASE_URI'])}")

        user = AdminUser.query.filter_by(username=args.username).first()
        if user:
            print(f"'{args.username}' already exists.")
            if input("Set a new password for it? [y/N] ").strip().lower() != "y":
                print("Nothing changed.")
                return 0
        else:
            print(f"Creating '{args.username}'.")

        password = getpass.getpass("New password: ")
        if len(password) < MINIMUM:
            print(f"Too short — use at least {MINIMUM} characters.", file=sys.stderr)
            return 1
        if password != getpass.getpass("Again: "):
            print("They did not match.", file=sys.stderr)
            return 1

        if user is None:
            user = AdminUser(username=args.username)
            db.session.add(user)
        user.set_password(password)
        db.session.commit()

        print(f"Done. Sign in at /admin/login as '{args.username}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
