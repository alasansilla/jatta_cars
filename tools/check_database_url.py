"""Check a database URL without printing it.

The Render deployment failed on a malformed connection string. Supabase shows
the password raw, and its generated passwords routinely contain `@`, `/`, `?`
and `#` — characters that mean something in a URL. A raw `@` makes the host look
like part of the password and the whole string fails to parse.

The application now percent-encodes the credentials itself, so a raw password
works. This tells you whether the value a host actually holds is sane, and
reports only its *shape* — never the value, so it is safe to run anywhere and to
paste the output into a chat.

    python tools/check_database_url.py
    JATTA_DATABASE_URL='...' python tools/check_database_url.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import unquote  # noqa: E402

from config import _normalise_credentials, _postgres_driver  # noqa: E402


def describe(url):
    findings, problems = [], []

    raw = url.strip()
    if raw != url:
        problems.append("has leading or trailing whitespace")
    if (raw.startswith('"') and raw.endswith('"')) or \
       (raw.startswith("'") and raw.endswith("'")):
        problems.append("is wrapped in quotes — paste it without them")
    raw = raw.strip('"').strip("'")

    if "://" not in raw:
        problems.append("has no scheme; it should start with postgresql://")
        return findings, problems

    scheme = raw.split("://", 1)[0]
    findings.append(f"scheme: {scheme}")
    if scheme not in ("postgres", "postgresql"):
        problems.append(f"unexpected scheme {scheme!r}")

    rest = raw.split("://", 1)[1]
    if "@" not in rest:
        problems.append("has no user:password@host section")
        return findings, problems

    userinfo, _, hostpart = rest.rpartition("@")
    user, colon, password = userinfo.partition(":")
    findings.append(f"user: {user[:9]}{'…' if len(user) > 9 else ''}")
    if not colon:
        problems.append("has no password")

    host = hostpart.split("/")[0]
    findings.append(f"host: {host}")
    port = host.rpartition(":")[2] if ":" in host else ""
    if port == "6543":
        findings.append("pooler: transaction (port 6543) — right for serverless")
    elif port == "5432":
        findings.append("pooler: session or direct (port 5432) — right for Render")
    elif port:
        problems.append(f"unusual port {port}")
    else:
        problems.append("no port given")

    if host.endswith(".pooler.supabase.com") and not user.startswith("postgres."):
        problems.append("pooler hosts need the user 'postgres.<project-ref>'")

    findings.append(f"password length: {len(password)}")
    findings.append(f"already percent-encoded: {'yes' if unquote(password) != password else 'no'}")
    risky = sorted(set(password) & set("@/?#[] "))
    if risky:
        findings.append(f"contains characters that need encoding: {' '.join(risky)}")
        findings.append("  (the app encodes these for you — no action needed)")

    if "/" not in hostpart:
        problems.append("no database name after the host; expected /postgres")

    return findings, problems


def main():
    url = os.environ.get("JATTA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("JATTA_DATABASE_URL is not set in this environment.")
        print("Run this where the app runs, or pass it inline for a one-off check.")
        return 2

    findings, problems = describe(url)
    print("Shape of the configured database URL (no values shown):")
    for line in findings:
        print(f"  {line}")

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")

    try:
        normalised = _normalise_credentials(url.strip().strip('"').strip("'"))
        from sqlalchemy.engine import make_url

        driver = _postgres_driver()
        probe = normalised.replace("postgresql://", f"postgresql+{driver}://", 1)
        probe = probe.replace("postgres://", f"postgresql+{driver}://", 1)
        parsed = make_url(probe)
        print(f"\nSQLAlchemy parses it: yes (host {parsed.host}, port {parsed.port})")
    except Exception as error:  # noqa: BLE001 — report the class, not the value
        print(f"\nSQLAlchemy CANNOT parse it: {type(error).__name__}")
        return 1

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
