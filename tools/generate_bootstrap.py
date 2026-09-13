"""Generate supabase/bootstrap.sql from the models.

The schema in the SQL Editor and the schema the application expects have to be
the same thing. Writing the DDL by hand guarantees they drift — the first
version of this file silently omitted every index declared with `index=True`,
including the unique one on `bookings.reference`, because CreateTable does not
emit them.

So the file is generated, and a test regenerates it and fails if the committed
copy is stale.

    python tools/generate_bootstrap.py            # rewrite the file
    python tools/generate_bootstrap.py --check    # exit 1 if it is out of date
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app import create_app  # noqa: E402
from app.models import db  # noqa: E402
from migrations.runner import discover  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "supabase", "bootstrap.sql")

# Tables that must never be reachable through Supabase's auto-generated REST
# API. That is all of them: the application talks to Postgres directly.
PRIVATE_TABLES = [
    "vehicles", "bookings", "enquiries", "settings", "media_assets",
    "admin_users", "schema_migrations",
    # Marketplace tables. operators and commission_entries carry business and
    # personal data; operator_fares is public information but is still served
    # by the application rather than by PostgREST.
    "operators", "operator_fares", "commission_entries", "driver_states", "booking_reviews",
]

HEADER = """\
-- Jatta Cars — Supabase bootstrap
--
-- GENERATED FILE. Do not edit by hand: run
--     python tools/generate_bootstrap.py
-- The schema below comes from the SQLAlchemy models, so it cannot drift from
-- the application. `python tools/generate_bootstrap.py --check` fails if this
-- file is stale, and a test runs that check.
--
-- Paste into the Supabase SQL Editor and run. Safe to run again: every
-- statement is guarded, so re-running changes nothing and repairs drift.
--
-- What it does:
--   1. Creates the application tables and every index the models declare —
--      including the unique index on bookings.reference, which is what stops
--      two bookings sharing a reference and one customer seeing another's.
--   2. Adds the booking overlap constraint (migrations 0002 and 0004) and the
--      query indexes (0003).
--   3. Locks the tables away from the PostgREST roles.
--   4. Sets up the media bucket: public to read, server-only to write.
--   5. Records the migrations as applied.
--
-- It creates no users and stores no secrets. See DEPLOYMENT.md for the admin
-- account, which is created separately so no password passes through here.

begin;
"""

TAIL = """
-- ---------------------------------------------------------------------------
-- 3. Keep the application tables off the public REST API
-- ---------------------------------------------------------------------------

-- Supabase exposes every table in `public` through PostgREST using the `anon`
-- and `authenticated` roles. This application does not use that API at all —
-- Flask connects straight to Postgres — so those roles get nothing.
--
-- Row-level security is enabled with no policies, which denies every row to any
-- role that does not bypass RLS. The grants are revoked as well, so the tables
-- are unreachable twice over. RLS is deliberately not FORCEd: the owner role
-- the application connects as must keep working.

do $$
declare
  t text;
begin
  foreach t in array array[{private_tables}] loop
    if exists (select 1 from pg_tables where schemaname = 'public' and tablename = t) then
      execute format('alter table public.%I enable row level security', t);
      execute format('revoke all on table public.%I from anon, authenticated', t);
    end if;
  end loop;
end
$$;

-- New tables created later must not be exposed by accident either.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 4. Media bucket
-- ---------------------------------------------------------------------------

-- Public to read, because the pictures are on a public website and are served
-- from Supabase's CDN. Images only, and no larger than the 8 MB the application
-- accepts, so a leaked URL cannot be used to park arbitrary files.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'media', 'media', true, 8388608,
  array['image/png', 'image/jpeg', 'image/webp', 'image/gif']
)
on conflict (id) do update set
  public             = excluded.public,
  file_size_limit    = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- Anyone may read an object in this bucket.
drop policy if exists "media_public_read" on storage.objects;
create policy "media_public_read"
  on storage.objects for select
  to public
  using (bucket_id = 'media');

-- Nobody may write through the API. There is deliberately no insert, update or
-- delete policy: uploads go through the Flask server using the service role
-- key, which bypasses RLS. A leaked anon key cannot upload anything.
drop policy if exists "media_anon_insert" on storage.objects;
drop policy if exists "media_anon_update" on storage.objects;
drop policy if exists "media_anon_delete" on storage.objects;

-- ---------------------------------------------------------------------------
-- 5. Record these as applied migrations
-- ---------------------------------------------------------------------------

create table if not exists public.schema_migrations (
  version     varchar(20)  not null primary key,
  name        varchar(200) not null,
  applied_at  timestamptz  not null
);

insert into public.schema_migrations (version, name, applied_at) values
{migration_rows}
on conflict (version) do nothing;

alter table public.schema_migrations enable row level security;
revoke all on table public.schema_migrations from anon, authenticated;

commit;

-- ---------------------------------------------------------------------------
-- Check it worked
-- ---------------------------------------------------------------------------
--
--   select tablename, rowsecurity from pg_tables
--    where schemaname = 'public' order by tablename;
--   -- every application table should show rowsecurity = true
--
--   select indexname from pg_indexes where schemaname = 'public' order by 1;
--   -- must include ix_bookings_reference (unique), ix_bookings_status,
--   -- ix_enquiries_is_read
--
--   select conname, pg_get_constraintdef(oid) from pg_constraint
--    where conname = 'bookings_no_overlap';
--   -- one row, and its WHERE clause must mention booking_type = 'rental' 
--
--   select id, public, file_size_limit from storage.buckets where id = 'media';
--   -- public = true, limit 8388608
"""

CONSTRAINTS = """
-- ---------------------------------------------------------------------------
-- 2. Booking integrity (migrations 0002, 0003 and 0004)
-- ---------------------------------------------------------------------------

-- Needed to mix an equality test with a range overlap in one exclusion
-- constraint.
create extension if not exists btree_gist;

-- Two people can pass the application's availability check at the same instant.
-- This is what actually stops one car being let twice: the range matches the
-- application's rule exactly — pick-up day inclusive, return day exclusive —
-- and only pending and confirmed bookings hold a car.
--
-- Only a hire holds a car for a range of days (migration 0004). Two taxi rides
-- on the same car on the same day are ordinary, so journeys are excluded or the
-- constraint would refuse a perfectly normal day's work.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'bookings_no_overlap') then
    alter table public.bookings
      add constraint bookings_no_overlap
      exclude using gist (
        vehicle_id with =,
        daterange(start_date, end_date, '[)') with &&
      )
      where (status in ('pending', 'confirmed') and booking_type = 'rental');
  end if;
end
$$;

create index if not exists ix_bookings_vehicle_dates
  on public.bookings (vehicle_id, start_date, end_date);
create index if not exists ix_bookings_status_start
  on public.bookings (status, start_date);
create index if not exists ix_vehicles_active
  on public.vehicles (is_active);
"""


def render():
    app = create_app()
    with app.app_context():
        dialect = postgresql.dialect()
        parts = [HEADER, "\n-- ---------------------------------------------------------------------------"
                 "\n-- 1. Tables and indexes (migration 0001)"
                 "\n-- ---------------------------------------------------------------------------\n"]

        for table in db.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
            ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            parts.append("\n" + ddl + ";\n")

            # CreateTable does not emit indexes declared with index=True. Missing
            # them cost bookings.reference its uniqueness the first time round.
            for index in sorted(table.indexes, key=lambda i: i.name or ""):
                statement = str(CreateIndex(index).compile(dialect=dialect)).strip()
                statement = statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                statement = statement.replace(
                    "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
                parts.append(statement + ";\n")

        parts.append(CONSTRAINTS)

        migration_rows = ",\n".join(
            f"  ('{step.VERSION}', '{step.NAME.replace(chr(39), chr(39) * 2)}', now())"
            for step in discover()
        )
        parts.append(TAIL.format(
            private_tables=", ".join(f"'{name}'" for name in PRIVATE_TABLES),
            migration_rows=migration_rows,
        ))
        return "\n".join(line.rstrip() for line in "".join(parts).splitlines()) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed file is out of date")
    args = parser.parse_args()

    generated = render()
    current = open(TARGET, encoding="utf-8").read() if os.path.exists(TARGET) else None

    if args.check:
        if current == generated:
            print("supabase/bootstrap.sql is up to date.")
            return 0
        print("supabase/bootstrap.sql is STALE. Run: python tools/generate_bootstrap.py",
              file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w", encoding="utf-8") as handle:
        handle.write(generated)
    print(f"Wrote {os.path.relpath(TARGET, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
