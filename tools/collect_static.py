"""Copy bundled static files into public/ for the CDN to serve.

Vercel serves anything under `public/` straight from its CDN, and its Flask
guide is explicit that Flask's own static folder should not be used for this.
Copying `app/static` to `public/static` keeps every existing
`url_for('static', ...)` URL working while the bytes come from the CDN instead
of waking a function.

Uploads are skipped: those live in Supabase Storage, not in the bundle.
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "app", "static")
TARGET = os.path.join(ROOT, "public", "static")
SKIP = {"uploads"}


def main():
    if not os.path.isdir(SOURCE):
        print(f"No static directory at {SOURCE}", file=sys.stderr)
        return 1

    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    os.makedirs(TARGET, exist_ok=True)

    copied = 0
    for entry in sorted(os.listdir(SOURCE)):
        if entry in SKIP or entry.startswith("."):
            continue
        source = os.path.join(SOURCE, entry)
        destination = os.path.join(TARGET, entry)
        if os.path.isdir(source):
            shutil.copytree(source, destination)
            copied += sum(len(files) for _, _, files in os.walk(source))
        else:
            shutil.copy2(source, destination)
            copied += 1

    print(f"Copied {copied} static file(s) to public/static")
    return 0


if __name__ == "__main__":
    sys.exit(main())
