-- Jatta Cars — Supabase bootstrap
--
-- Paste into the Supabase SQL Editor and run. Safe to run again: every
-- statement is guarded, so re-running changes nothing and fixes anything that
-- has drifted.
--
-- What this does, and why:
--
--   1. Creates the application tables. The DDL is generated from the same
--      SQLAlchemy models the app uses, so it cannot drift from the code.
--   2. Adds the booking exclusion constraint and the indexes from migrations
--      0002 and 0003.
--   3. Locks the tables away from Supabase's auto-generated REST API. The
--      Flask app talks to Postgres directly as the database owner; nothing
--      should be reachable as `anon` or `authenticated`, and this table holds
--      customers' names, emails and phone numbers.
--   4. Sets up the media bucket: public to read, writable only by the server.
--   5. Records these as applied migrations, so `python -m migrations --status`
--      against this database agrees with what is actually here.
--
-- It creates no users and stores no secrets. See DEPLOYMENT.md for the admin
-- account, which is created separately so no password passes through here.

begin;

-- ---------------------------------------------------------------------------
-- 1. Tables (migration 0001)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS admin_users (
	id SERIAL NOT NULL, 
	username VARCHAR(60) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS enquiries (
	id SERIAL NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	email VARCHAR(160) NOT NULL, 
	phone VARCHAR(40), 
	subject VARCHAR(160), 
	message TEXT NOT NULL, 
	is_read BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS media_assets (
	id SERIAL NOT NULL, 
	filename VARCHAR(200) NOT NULL, 
	original_name VARCHAR(200) NOT NULL, 
	alt_text VARCHAR(200), 
	size_bytes INTEGER NOT NULL, 
	uploaded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (filename)
);

CREATE TABLE IF NOT EXISTS settings (
	key VARCHAR(80) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS vehicles (
	id SERIAL NOT NULL, 
	make VARCHAR(60) NOT NULL, 
	model VARCHAR(60) NOT NULL, 
	year INTEGER NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	transmission VARCHAR(20) NOT NULL, 
	fuel VARCHAR(20) NOT NULL, 
	seats INTEGER NOT NULL, 
	doors INTEGER NOT NULL, 
	luggage INTEGER NOT NULL, 
	daily_rate NUMERIC(10, 2) NOT NULL, 
	weekly_rate NUMERIC(10, 2), 
	deposit NUMERIC(10, 2) NOT NULL, 
	image VARCHAR(120), 
	description TEXT, 
	features TEXT, 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS bookings (
	id SERIAL NOT NULL, 
	reference VARCHAR(12) NOT NULL, 
	vehicle_id INTEGER NOT NULL, 
	customer_name VARCHAR(120) NOT NULL, 
	email VARCHAR(160) NOT NULL, 
	phone VARCHAR(40), 
	pickup_location VARCHAR(120) NOT NULL, 
	dropoff_location VARCHAR(120) NOT NULL, 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	total_price NUMERIC(10, 2) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	notes TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id)
);

-- ---------------------------------------------------------------------------
-- 2. Booking integrity (migrations 0002 and 0003)
-- ---------------------------------------------------------------------------

-- Needed to mix an equality test with a range overlap in one exclusion
-- constraint.
create extension if not exists btree_gist;

-- Two people can pass the application's availability check at the same instant.
-- This is what actually stops the same car being let twice: the range matches
-- the app's rule exactly — pick-up day inclusive, return day exclusive — and
-- only pending and confirmed bookings hold a car.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'bookings_no_overlap') then
    alter table public.bookings
      add constraint bookings_no_overlap
      exclude using gist (
        vehicle_id with =,
        daterange(start_date, end_date, '[)') with &&
      )
      where (status in ('pending', 'confirmed'));
  end if;
end
$$;

create index if not exists ix_bookings_vehicle_dates
  on public.bookings (vehicle_id, start_date, end_date);
create index if not exists ix_bookings_status_start
  on public.bookings (status, start_date);
create index if not exists ix_vehicles_active
  on public.vehicles (is_active);

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
-- the app connects as must keep working.

do $$
declare
  t text;
begin
  foreach t in array array[
    'vehicles', 'bookings', 'enquiries', 'settings', 'media_assets',
    'admin_users', 'schema_migrations'
  ] loop
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

-- Existing sequences (the SERIAL primary keys).
revoke all on all sequences in schema public from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 4. Media bucket
-- ---------------------------------------------------------------------------

-- Public to read, because the pictures are on a public website and are served
-- straight from Supabase's CDN. Images only, and no larger than the 8 MB the
-- application accepts, so a stolen URL cannot be used to park arbitrary files.
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
-- key, which bypasses RLS. A leaked anon key therefore cannot upload anything.
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
  ('0001', 'initial schema', now()),
  ('0002', 'no overlapping bookings for one vehicle (postgres)', now()),
  ('0003', 'booking lookup indexes', now())
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
--   select conname from pg_constraint where conname = 'bookings_no_overlap';
--   -- one row
--
--   select id, public, file_size_limit from storage.buckets where id = 'media';
--   -- public = true, limit 8388608
