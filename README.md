# Jatta Cars

Car hire website with a public booking flow and a staff area for managing the
fleet, bookings and enquiries. Flask + SQLite, no build step.

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

**Editing the site**

Sign in at `/admin/login` and a bar appears across the top of every public page.
Press **Edit this page** and the page itself becomes editable:

- Click any heading, paragraph or label and type over it
- Click a picture to replace it — the file uploads and swaps in place
- Click the round icons beside "Why choose us" to pick a different one
- Lists get an **× ** on each item and an **+ Add item** button
- **Save changes** writes everything at once and reloads; **Discard** throws it away

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
.venv/bin/python seed.py      # creates the database and a starting fleet
.venv/bin/python run.py
```

Then open http://127.0.0.1:5000.

`seed.py` prints a generated admin password on first run. To choose your own:

```bash
JATTA_ADMIN_PASSWORD='something-long' .venv/bin/python seed.py
```

Running `seed.py` again is safe — existing vehicles and the admin account are
left alone.

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
config.py          secrets, database URL, upload limits
seed.py            database setup and starting fleet
run.py             development server
```

## Not built yet

- **No email is sent.** Booking confirmations and contact form messages are
  stored in the database and shown in the staff area only. Wire up SMTP (or a
  service like Postmark) when you want them delivered.
- **No online payment.** Bookings are requests; payment and the deposit happen
  at the desk.
- One shared staff login rather than per-user accounts.
- No revision history on edits — saving overwrites. "Reset to defaults" in
  Settings restores the original wording for a group.
- The inline editor covers text, pictures, icons and lists. Structured vehicle
  data (seats, doors, transmission, category) is still edited on the vehicle form,
  because those drive the search filters and need validating.
