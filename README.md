# Jatta Cars

Self-drive car hire website for The Gambia, with a public booking flow and a
staff area for managing the fleet, bookings and enquiries. Flask + SQLite, no
build step.

## Taking the site private

The site is visible by default, which is what you want while building it locally.
Before it goes on a real domain with `[TBC]` wording still in it, switch it to a
draft from **Settings → Setup**: visitors then get a short holding page with your
contact details, while you carry on seeing the real site. The button tells you how
much placeholder wording is currently public.

## This is still a draft

Wording that has not been confirmed with the business is marked `[TBC]` so it
cannot be mistaken for a real claim. **Settings → Setup** in the staff area
lists every one of them, and the count is shown in the staff navigation. Work
through that list before showing the site to customers.

Nothing about insurance, mileage, fuel, deposits, extra drivers, breakdown
cover or cancellation has been filled in — those are business facts, not
defaults, so the draft asks the questions rather than answering them.

## What's in it

**Public site**

- Home page with a search panel (car type, pick-up point, dates)
- Fleet listing with filters — type, transmission, seats, price — and sorting
- Live availability: give dates and only genuinely free cars are shown
- Vehicle pages with specs, features and a price breakdown for the chosen dates
- Booking requests with a reference (`JC-XXXXXX`); customers can look a booking
  up again with the reference plus their email
- Contact form; messages are stored and read in the staff area
- About page
- A reviews section, off-limits until you have real quotes — the placeholders
  say so, and it can be switched off entirely under Settings

**Editing the site**

Sign in at `/admin/login` and a bar appears across the top of every public page.
Press **Edit this page** and the page itself becomes editable:

- Click any heading, paragraph or label and type over it
- Click a picture to replace it — the file uploads and swaps in place
- Click the round icons beside "Why choose us" to pick a different one
- Lists get an **× ** on each item and an **+ Add item** button
- **Save changes** writes everything at once and reloads; **Discard** throws it away

The fleet is built the same way. On the fleet page, **+ Add a car** creates one
and drops you on its page to fill in; a car starts hidden so a half-finished
entry is never public. On a car's page you can edit make, model, year, seats,
doors, luggage, rates and deposit in place, pick category, transmission and fuel
from a list, replace the photo, and hide or remove the car. A car with an open
booking cannot be removed.

Wording lives in `app/settings.py` as defaults, and edits are stored in the
database. Anything never edited falls back to the default, so a page cannot end
up blank.

**Staff area** (`/admin`)

- Dashboard: pending and confirmed bookings, cars out today, 30-day booked value
- Fleet management: add, edit, hide and delete vehicles, with photo upload
- Bookings: filter by status, confirm / complete / cancel
- Enquiries from the contact form
- Pictures: everything uploaded to the site, with deletion blocked while an image
  is still in use
- Settings: the things with no visible place on a page — currency, pick-up points,
  booking limits, insurance excess. Each group can be reset to its original wording.

**Booking rules that are actually enforced**

- A car cannot be double-booked. Overlaps are checked on the customer's request
  *and* again when staff confirm, in case the situation changed in between.
- Pick-up day is inclusive, return day exclusive — so one customer can return on
  the morning another collects.
- Whole weeks are charged at the weekly rate when that is cheaper than the daily
  rate; leftover days are charged daily.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m migrations   # create the schema
.venv/bin/python seed.py         # create the staff login
.venv/bin/python run.py
```

Locally that is SQLite in `instance/` and pictures on disk in
`app/static/uploads`. Point `JATTA_DATABASE_URL` at Postgres and set the
`SUPABASE_*` variables and the same code uses those instead — see
[DEPLOYMENT.md](DEPLOYMENT.md).

`seed.py` adds **no** vehicles — add your own under Fleet in the staff area.
For development you can load a European sample fleet with `seed.py --demo`;
`tools/clear_demo_fleet.py` takes it out again, refusing to touch any car that
has a booking, an uploaded photo or an edited description.

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests
```

Then open http://127.0.0.1:5000.

`seed.py` prints a generated admin password on first run. To choose your own:

```bash
JATTA_ADMIN_PASSWORD='something-long' .venv/bin/python seed.py
```

Running `seed.py` again is safe — existing vehicles and the admin account are
left alone. Change the password afterwards under **Account** in the staff area;
`seed.py` will not reset it.

## Configuration

Almost nothing needs configuring in code. Business details, pick-up points,
currency, booking limits and every piece of page copy are edited in the browser
and stored in the database; `app/settings.py` only holds the starting values.

`config.py` is what is left: secrets, the database URL and the upload limit.
Environment variables override them:

| Variable | Purpose |
| --- | --- |
| `JATTA_SECRET_KEY` | Session signing key. **Set this in production.** |
| `JATTA_DATABASE_URL` | Database URL. Defaults to SQLite in `instance/`. |
| `JATTA_ADMIN_USER` | Admin username for `seed.py` (default `admin`). |
| `JATTA_ADMIN_PASSWORD` | Admin password for `seed.py`. |

Prices are shown in dalasi. To also show an approximate euro or pound figure,
enter the rate under **Settings → Booking rules**; leave it at 0 and no
conversion appears. Nothing fetches a live rate — you set it, and it is labelled
as approximate.

Uploads are capped at 8 MB and limited to JPG, PNG, WebP and GIF.

## Vehicle photos

Cars fall back to a flat illustration matching their category
(`app/static/img/car-*.svg`). To use a real photo, either click the car's picture
while editing a page, or upload it on the vehicle's edit form. Cut-outs on a
white or transparent background look best; the layout expects a roughly 16:10
landscape image.

Uploads land in `app/static/uploads/`, which is deliberately kept out of git —
it is site data, not source. Back it up along with `instance/jatta.db`.

## Layout

```
app/
  __init__.py      application factory, template filters, globals
  models.py        Vehicle, Booking, Enquiry, AdminUser, Setting, MediaAsset
  settings.py      the editable-settings schema and its defaults
  media.py         image uploads and where each one is used
  forms.py         hand-rolled validation helpers
  public.py        customer-facing routes
  admin.py         staff routes, including the inline-editor API
  templates/       Jinja templates (partials/ holds the shared pieces)
  static/
    css/style.css  the whole stylesheet
    js/editor.js   the inline page editor
    uploads/       uploaded pictures (git-ignored)
config.py          environment-driven config: database, storage, secrets
wsgi.py            WSGI entrypoint (Vercel, gunicorn)
seed.py            staff login; --demo adds a sample fleet
run.py             development server
migrations/        versioned schema steps; `python -m migrations`
supabase/          bootstrap.sql for the Supabase SQL Editor
tools/             collect_static, create_admin, transfer_to_postgres,
                   clear_demo_fleet
tests/             unittest suite (no pytest needed)
render.yaml        Render blueprint (the documented hosting path)
vercel.json        Vercel config, kept as an alternative
DEPLOYMENT.md      hosting, environment variables, going live
```

## Database and storage

The schema is created by versioned steps in `migrations/`, run with
`python -m migrations` (`--status` to list them). They work identically on
SQLite and Postgres.

One thing only Postgres can do: migration 0002 adds an exclusion constraint so
two people cannot book the same car for overlapping dates even if their requests
arrive at the same instant. The application checks availability first, but that
is a read followed by a write; the constraint is what actually settles it. On
SQLite the step is skipped and the application check stands alone.

Pictures go through a storage abstraction (`app/storage.py`): the local disk in
development, a Supabase Storage bucket in production. The same
`uploads/<filename>` key works under both, so moving hosts does not rewrite the
database.

## Not built yet

- **No email is sent.** Booking confirmations and contact form messages are
  stored in the database and shown in the staff area only. Nothing on the site
  promises an email, because none goes out — staff contact the customer. Wire up
  SMTP (or a service like Postmark) when you want that automated.
- **No online payment.** Bookings are requests; money changes hands when the car
  is collected. Cash, mobile money and card are preferences for later, not
  capabilities this site has — do not advertise them as if they were.
- One shared staff login rather than per-user accounts. It can be renamed only by
  editing the database; the password is changed under **Account**.
- No revision history on edits — saving overwrites. "Reset to defaults" in
  Settings restores the original wording for a group.
- The inline editor covers text, pictures, icons and lists. Structured vehicle
  data (seats, doors, transmission, category) is still edited on the vehicle form,
  because those drive the search filters and need validating.
