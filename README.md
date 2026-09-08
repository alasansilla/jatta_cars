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

**Staff area** (`/admin`)

- Dashboard: pending and confirmed bookings, cars out today, 30-day booked value
- Fleet management: add, edit, hide and delete vehicles
- Bookings: filter by status, confirm / complete / cancel
- Enquiries from the contact form

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

Business details, pick-up points, currency and booking limits live in
`config.py`. Change them there; everything on the site reads from that file.

Environment variables override the sensitive parts:

| Variable | Purpose |
| --- | --- |
| `JATTA_SECRET_KEY` | Session signing key. **Set this in production.** |
| `JATTA_DATABASE_URL` | Database URL. Defaults to SQLite in `instance/`. |
| `JATTA_ADMIN_USER` | Admin username for `seed.py` (default `admin`). |
| `JATTA_ADMIN_PASSWORD` | Admin password for `seed.py`. |
| `JATTA_PROMO` | Ribbon text on the home page panel. Empty string hides it. |

## Vehicle photos

Cars fall back to a flat illustration matching their category
(`app/static/img/car-*.svg`). To use a real photo, drop the file into
`app/static/img/` and put `img/your-file.jpg` in the vehicle's **Photo** field in
the staff area. Cut-outs on a white or transparent background look best; the
layout expects a roughly 16:10 landscape image.

## Layout

```
app/
  __init__.py      application factory, template filters, globals
  models.py        Vehicle, Booking, Enquiry, AdminUser
  forms.py         hand-rolled validation helpers
  public.py        customer-facing routes
  admin.py         staff routes
  templates/       Jinja templates (partials/ holds the shared pieces)
  static/          stylesheet and images
config.py          all configuration
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
