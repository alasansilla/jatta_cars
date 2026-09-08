# Deploying Jatta Cars

The app is Flask talking directly to Postgres. Supabase provides the database
and the image storage; the host runs the Flask app. Local development keeps
working on SQLite with files on disk — nothing here changes that.

Nothing in this repository contains a credential. Everything below is set as an
environment variable on the host, or pasted into the Supabase dashboard by hand.

---

## Before anything: hosting is not settled

**Vercel's Hobby plan is for non-commercial use.** A car hire business taking
bookings is commercial, so Hobby is not a licence this site can run under. The
options:

| Option | Cost | Notes |
| --- | --- | --- |
| **Vercel Pro** | ~$20/user/month | What the setup below is written for. Zero-config Flask, deploys from GitHub. |
| **Render / Railway / Fly.io** | ~$5–7/month | Runs Flask as a normal long-lived process. Use the *session* pooler (port 5432) instead of the transaction pooler. |
| **Cloudflare** | — | Workers does not run a WSGI app like this comfortably. Sensible as DNS and CDN **in front of** one of the above, not as the host. |

The application code is the same either way; only two environment variables
differ. **Nothing is deployed and no plan has been bought.** Decide the host
first — the rest of this takes about fifteen minutes.

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

## 4. Deploy

The repository already contains what Vercel needs:

- `wsgi.py` — exposes `app`, one of Vercel's recognised entrypoints
- `vercel.json` — 30 s function timeout, excludes tests and the local database
  from the bundle, and runs `tools/collect_static.py`
- `.python-version` — 3.12
- `requirements.txt` — Flask, SQLAlchemy, `psycopg[binary]`

`tools/collect_static.py` copies `app/static` into `public/static` at build
time, so CSS, JavaScript and the car illustrations are served from the CDN
rather than by waking a function — Vercel's Flask guide is explicit that Flask's
own static folder should not be used for this. Existing `url_for('static', …)`
URLs are unchanged.

Connect the GitHub repository in Vercel, or `vercel deploy` from the CLI.

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

---

## Notes

**Migrations.** `python -m migrations` applies anything not yet run and records
it. `--status` lists them. Step 1's SQL and the migration runner produce the
same schema and agree on what has been applied, so either route works.

**Backups.** Supabase's free tier keeps daily backups for 7 days. The pictures
in Storage are not covered by a database backup — they are separate objects.

**Rotating the service role key.** Supabase Dashboard → Project Settings → API →
roll the key, then update `SUPABASE_SERVICE_ROLE_KEY` on the host and redeploy.
Nothing in the repository or the database stores it.

**If the site will not start**, the logs name the missing variable. That is the
fail-closed check in step 3, not a crash.
