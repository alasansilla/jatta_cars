# Deploying Jatta Cars

## Current local checkpoint — 13 September 2026

The local preview runs at `http://127.0.0.1:5001/` (port 5000 may be used by
macOS AirPlay). The marketplace home links to `/ride`, scheduled journeys and
rentals. `/operator/drive` provides a single active vehicle per operator with
availability, driver acceptance, trip stages and authenticated GPS sharing.
Migration 0006 adds driver state; run pending migrations on the hosting database
before deploying this code. The local migration is applied; remote migration
and a successful production deployment have not been verified.

Geoapify is connected locally. Its key is in `instance/geoapify.json`, an ignored
file with owner-only permissions. Do not commit or copy it into frontend assets.
For hosting, set `GEOAPIFY_API_KEY` as a secret environment variable; production
does not read the local credential file. This automatically selects Geoapify's
geocoding and driving-route endpoints; explicit `JATTA_GEOCODER_*` and
`JATTA_ROUTER_*` settings still take precedence. A real Kololi–Bakau lookup and
road route were verified on 13 September. Tests use independent credentials and
mocked provider data.

Real bookings still require approved operators, assigned active vehicles,
distance fares and drivers online. Do not seed invented operators into the live
database. Commission is recorded, not collected: online payment, payouts and
email delivery are not implemented. This checkpoint is not a completed Uber
replacement or a verified production launch.

The app is Flask talking directly to Postgres. Supabase provides the database
and the image storage; the host runs the Flask app. Local development keeps
working on SQLite with files on disk — nothing here changes that.

Nothing in this repository contains a credential. Everything below is set as an
environment variable on the host, or pasted into the Supabase dashboard by hand.

---

## The host

**Render** is the documented path. Vercel's config is still in the repository
and still works, but Vercel's Hobby plan is licensed for non-commercial use and
a car hire business taking bookings is commercial, so it would need Pro at about
$20/user/month.

| Host | Cost | Notes |
| --- | --- | --- |
| **Render Free** | £0 | For testing. Sleeps after 15 minutes idle and takes ~1 minute to wake, so a customer hitting a cold site waits. Ephemeral disk. 750 instance hours a month per workspace. |
| **Render Starter** | ~$7/month | No sleeping. What a live site needs. |
| **Vercel Pro** | ~$20/user/month | `vercel.json` and `wsgi.py` are still here if you prefer it. |
| **Cloudflare** | — | Workers will not run this WSGI app comfortably. Right choice as DNS and CDN **in front of** Render, not as the host. |

The application code is identical on all of them. The only difference that
matters is which Supabase pooler to use: a long-lived process like Render wants
the **session** pooler on port 5432, a serverless host like Vercel wants the
**transaction** pooler on 6543. The app works this out from the port.

**Nothing is deployed and nothing has been bought.**

---

## 1. Supabase: run the bootstrap SQL

Project **Jatta Cars** (`khisfholynmqvmrqlrij`, Ireland) already exists.

Open the SQL Editor and run [`supabase/bootstrap.sql`](supabase/bootstrap.sql).
It is idempotent — run it again any time. It:

- creates the tables (DDL generated from the same models the app uses)
- adds the booking overlap constraint and the indexes
- **enables row-level security and revokes `anon` / `authenticated` on every
  application table**, so Supabase's auto-generated REST API cannot reach
  customer names, emails or phone numbers
- creates the `media` bucket: public to read, images only, 8 MB cap, and **no
  write policy at all** — uploads only happen server-side with the service role
  key, which bypasses RLS
- records migrations 0001–0003 as applied

Verify with the queries at the bottom of the file: every application table
should report `rowsecurity = true`.

## 2. Collect the connection details

From **Project Settings → Database → Connection string**, take the pooler URI.
Which port depends on the host:

- **Serverless (Vercel): port 6543**, the transaction pooler. Each request is a
  short-lived function, and Supavisor owns the real connections. The app detects
  `:6543` and turns off its own connection pool and prepared statements, which
  transaction mode cannot carry.
- **A long-running process (Render, Fly, a VM): port 5432**, the session pooler.
  The app keeps a small pool of its own.

From **Project Settings → API**, take the project URL and the **service role**
key — not the anon key.

## 3. Set the environment variables

On Vercel: Project Settings → Environment Variables, scope Production. These are
server-side only; none is exposed to the browser, and none has a `NEXT_PUBLIC_`
style prefix. Locally, copy `.env.example` to `.env` (git-ignored).

| Variable | Required | Value |
| --- | --- | --- |
| `JATTA_SECRET_KEY` | **yes** | Long random string. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JATTA_ENV` | **yes** | `production` |
| `JATTA_DATABASE_URL` | **yes** | The pooler URI from step 2, password included |
| `JATTA_STORAGE` | **yes** | `supabase` |
| `SUPABASE_URL` | **yes** | `https://khisfholynmqvmrqlrij.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | **yes** | Service role key. Server-only — it bypasses RLS |
| `SUPABASE_STORAGE_BUCKET` | no | `media` (the default) |
| `JATTA_DB_SSLMODE` | no | `require` to insist on TLS |
| `JATTA_DB_POOL_SIZE` | no | `5`. Ignored on the transaction pooler |
| `JATTA_DB_POOL` | no | `none` to force the pool off on port 5432 |
| `JATTA_UPLOAD_CACHE_SECONDS` | no | `31536000`. Upload filenames are unique, so long caching is safe |

**The app fails closed.** With `JATTA_ENV=production` it refuses to start if the
secret key is the development default, if there is no `JATTA_DATABASE_URL`, or
if storage is not configured — and says which. A deployment that will not boot
is better than one that quietly takes bookings into a database that gets thrown
away.

## 4. Deploy to Render

`render.yaml` describes the service, so Render can create it from the
repository: **New → Blueprint**, pick the repo, and it reads the file. It
declares nothing secret — the four secrets are marked `sync: false`, which makes
Render prompt for them in the dashboard instead.

What it sets up:

- `gunicorn wsgi:app --bind 0.0.0.0:$PORT` — Render supplies `PORT` (10000 by
  default) and requires binding `0.0.0.0`; a hard-coded port never serves.
- `gthread` workers, 2 × 4. These requests spend their time waiting on Postgres,
  so threads buy more than processes on a small instance.
- `healthCheckPath: /healthz`, which runs `SELECT 1`. A deployment with the
  wrong `JATTA_DATABASE_URL` fails the check and is rolled back instead of going
  live and losing bookings. It answers even while the site is in draft, and
  never returns the connection string in an error.
- `autoDeploy: false` — deploy when you mean to, not on every push.
- Python 3.12, from `PYTHON_VERSION` and `.python-version`.

Then paste the four secrets from step 3 into the dashboard and deploy.

**On the free plan the disk is wiped on every restart and the instance sleeps
after 15 minutes.** That is survivable only because the database is Supabase and
the pictures are in Supabase Storage — nothing the site needs is kept on the
instance. Do not put it in front of customers on the free plan; the first
visitor after a quiet hour waits a minute for a loading page.

### If you use Vercel instead

`vercel.json` and `wsgi.py` are still here. Connect the repository in Vercel or
run `vercel deploy`. Its build runs `tools/collect_static.py`, which copies
`app/static` into `public/static` so the CDN serves it rather than a function —
Vercel's Flask guide is explicit that Flask's static folder should not be used
for this. Existing `url_for('static', …)` URLs are unchanged. Use the
transaction pooler (port 6543) there.

## 5. Create the staff account

Do **not** use `seed.py` for this: it takes the password from an environment
variable, where it would sit in a dashboard and a deployment log.

```bash
export JATTA_DATABASE_URL='…'      # the same URI as step 2
python tools/create_admin.py --suggest      # optional: a strong password
python tools/create_admin.py
```

It prompts twice, never echoes, never accepts the password as an argument, and
stores only a hash. Run it from your own machine — it needs no deployment.

Afterwards, sign in at `https://hajo.uk/admin/login` and change the password
under **Account** if you want to rotate it.

Until the domain is attached, the same page is at
`https://<service>.onrender.com/admin/login`.

## 6. Move the existing content across

Only if the local SQLite site has content worth keeping. It currently holds one
staff account and nothing else, so this may not be needed at all.

```bash
export JATTA_DATABASE_URL='…'
export JATTA_STORAGE=supabase SUPABASE_URL='…' SUPABASE_SERVICE_ROLE_KEY='…'

python tools/transfer_to_postgres.py            # dry run, shows the plan
python tools/transfer_to_postgres.py --commit
```

It reads the local database and `app/static/uploads`, writes rows that are not
already there (matched on booking reference, setting key, filename), and uploads
the pictures to the bucket. The source is only ever read. Running it twice
copies nothing the second time. Staff accounts are deliberately not copied — use
step 5.

## 7. Point hajo.uk at it

1. Add `hajo.uk` and `www.hajo.uk` in the host's domain settings.
2. In Cloudflare DNS for `hajo.uk`, add the records it gives you.
   - On Render: `CNAME www → <service>.onrender.com`, and for the apex either an
     `ALIAS`/`CNAME` flattened at the root (Cloudflare does this) or the `A`
     record Render shows.
   - On Vercel: `CNAME www → cname.vercel-dns.com`, and an `A` record for the
     apex to the address Vercel shows.
   - Set those records to **DNS only** (grey cloud) until the certificate is
     issued; proxying first can stall validation. Turn the proxy on afterwards
     if you want Cloudflare's CDN and WAF.
3. Wait for the certificate, then check `https://hajo.uk` loads.
4. If Cloudflare's proxy is on, set SSL/TLS mode to **Full (strict)**. "Flexible"
   would put plain HTTP between Cloudflare and the host.

## 8. Check it

- `https://hajo.uk/` loads and the fleet page lists your cars.
- Sign in, upload a picture, and confirm the image URL is
  `https://khisfholynmqvmrqlrij.supabase.co/storage/v1/object/public/media/uploads/…`
  — that means storage is working, not the local disk.
- Make a test booking, then try the same car and dates again: the second attempt
  must be refused.
- Delete the test booking from the staff area.
- `python -m migrations --status` against the production URL: all applied.
- `curl https://hajo.uk/healthz` returns `{"status":"ok","database":"ok","storage":"supabase"}`.
  If `storage` says `local`, the `SUPABASE_*` variables did not reach the app.

---

## Notes

**Migrations.** `python -m migrations` applies anything not yet run and records
it. `--status` lists them. Step 1's SQL and the migration runner produce the
same schema and agree on what has been applied, so either route works.

### Marketplace schema

The marketplace added migration `0004`, which creates the operator, fare and
commission tables, widens bookings to carry a journey, and narrows the
overlap constraint so it applies to hires only — two taxi rides on one car in a
day are ordinary. On an existing database:

```bash
python -m migrations          # apply
python -m migrations --status # confirm
```

A brand-new Supabase project should instead run the regenerated
`supabase/bootstrap.sql`, which already contains the marketplace schema.

**Operator sign-ins are issued by hand.** Approving an operator does not let
them in; use **Issue sign-in** on the admin operators page, which generates a
password and shows it once. Pass it to them directly — the site sends no email.

**Backups. The free Supabase plan has no automated backups at all.** Supabase's
documentation says free projects should "regularly export their data using the
Supabase CLI `db dump` command and maintain off-site backups". Daily backups
start on Pro (7 days retained), Team is 14 and Enterprise up to 30;
point-in-time recovery is a paid add-on on Pro and above.

So on the free plan, taking a backup is a thing someone has to do:

```bash
supabase db dump --db-url "$JATTA_DATABASE_URL" -f jatta-$(date +%F).sql
```

Keep it somewhere that is not Supabase. Pictures in Storage are separate objects
and are not in a database dump either — copy the bucket as well if it matters.
Once there are real bookings in here, this is the first thing to sort out.

**Rotating the service role key.** Supabase Dashboard → Project Settings → API →
roll the key, then update `SUPABASE_SERVICE_ROLE_KEY` on the host and redeploy.
Nothing in the repository or the database stores it.

**If the site will not start**, the logs name the missing variable. That is the
fail-closed check in step 3, not a crash.


## The Render deployment that failed

The last deployment died on a malformed database URL. The cause was not Render:
Supabase shows the database password raw, and its generated passwords routinely
contain `@`, `/`, `?` and `#` — characters that mean something inside a URL. A
raw `@` makes the host look like part of the password, and the connection string
fails to parse before anything can connect.

The application now percent-encodes the user and password itself, splitting on
the *last* `@` so a password containing one is still read correctly. A password
that was already encoded is left alone rather than double-encoded. Both forms
work, so the value can be pasted straight from the Supabase dashboard.

To check what a host actually holds, without printing it:

```bash
python tools/check_database_url.py
```

It reports only the *shape* — scheme, user prefix, host, port, password length,
whether it is already encoded, and whether SQLAlchemy can parse it. No part of
the value is printed, so the output is safe to paste into a chat. Nothing in the
application logs the connection string either; the health check reports the
exception class, never its message.


## Maps, geocoding and routing

Three separate things, configured separately, each optional.

### Choosing a provider

**Geocoding and routing are deliberately unset by default.** The public
Nominatim and OSRM demo servers forbid production use, and pointing at them
would both breach that and post customers' pickup addresses to a service nobody
chose. With them unset the site works: no map, addresses submitted as typed, and
a plain "the operator will confirm the fare" message.

Realistic options, cheapest first:

| Option | Notes |
| --- | --- |
| Leave unset | Free, honest, works today. Operators quote each journey by hand. |
| Self-host Nominatim + OSRM | No per-request cost; needs a server and a regional extract. Point the two URLs at it. |
| A commercial geocoding/routing API | Pick any that returns JSON. Put the key in the API-key variable, never in the URL you expose. |

Nothing here has been bought, and no account has been created.

### Tiles

`JATTA_MAP_TILE_URL` is fetched **by the browser**, so anything in it is public.
A tile provider that authenticates with a key in the URL cannot be kept secret
this way — put such a provider behind your own proxy and point this at the proxy.
The default is OpenStreetMap's public raster service, which needs no key but has
a usage policy: move to your own or a paid provider before this carries real
traffic, and keep the attribution.

### The variables

| Variable | Default | What it does |
| --- | --- | --- |
| `JATTA_MAP_ENABLED` | `1` | Set to `0` to drop maps entirely |
| `JATTA_MAP_TILE_URL` | OSM raster | `{z}/{x}/{y}` template; fetched by the browser |
| `JATTA_MAP_ATTRIBUTION` | OSM credit | Shown on the map; most providers require it |
| `JATTA_MAP_MAX_ZOOM` | `19` | |
| `JATTA_MAP_CENTRE_LAT` | `13.4432` | Where the map sits before a point is known |
| `JATTA_MAP_CENTRE_LNG` | `-15.3101` | |
| `JATTA_MAP_ZOOM` | `8` | |
| `JATTA_GEOCODER_URL` | *(unset)* | Address lookup. Placeholders: `{query}` `{limit}` `{country}` `{key}` |
| `JATTA_GEOCODER_API_KEY` | *(unset)* | Server-side only. Sent as `Authorization: Bearer` unless the URL uses `{key}` |
| `JATTA_GEOCODER_COUNTRY` | `gm` | Biases results, so "Kololi" finds the right one |
| `JATTA_ROUTER_URL` | *(unset)* | Distance and duration. Placeholders: `{lat1}` `{lon1}` `{lat2}` `{lon2}`, or `{coords}` for OSRM's `lon,lat;lon,lat` |
| `JATTA_ROUTER_API_KEY` | *(unset)* | Server-side only |
| `JATTA_ROUTING_TIMEOUT` | `8` | Seconds before giving up and falling back |
| `JATTA_ROUTING_USER_AGENT` | `jatta-cars` | Some providers reject requests without one |

Example shapes, with your own hosts:

```bash
# Nominatim-compatible geocoder
JATTA_GEOCODER_URL='https://geocode.example.org/search?q={query}&format=json&limit={limit}&countrycodes={country}'

# OSRM-compatible router
JATTA_ROUTER_URL='https://router.example.org/route/v1/driving/{coords}?overview=false'
```

The response parsing is deliberately tolerant: it reads Nominatim-style lists,
GeoJSON `FeatureCollection`s and `{"results": [...]}` envelopes for geocoding,
and OSRM-style `routes[0].distance` / `.duration` plus common variants for
routing. If a provider returns something unrecognisable the site falls back to a
manual quote rather than guessing.

### What is stored on a booking

The typed addresses are always kept, because they are what the customer actually
wrote. When a lookup succeeds, the coordinates, road distance, estimated
duration and the provider's host name are stored alongside them, so a fare can
be explained later and a change of provider is visible in the record.

The fare is always recomputed on the server from the stored distance. A distance
posted by the browser is ignored, so the form cannot be edited to talk the price
down.
