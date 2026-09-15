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

-- ---------------------------------------------------------------------------
-- 1. Tables and indexes (migration 0001)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS admin_users (
	id SERIAL NOT NULL,
	username VARCHAR(60) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS auth_events (
	id SERIAL NOT NULL,
	kind VARCHAR(30) NOT NULL,
	subject_hash VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_auth_events_kind_subject_time ON auth_events (kind, subject_hash, created_at);

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
CREATE INDEX IF NOT EXISTS ix_enquiries_is_read ON enquiries (is_read);

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

CREATE TABLE IF NOT EXISTS operators (
	id SERIAL NOT NULL,
	name VARCHAR(120) NOT NULL,
	slug VARCHAR(120) NOT NULL,
	contact_name VARCHAR(120),
	email VARCHAR(160),
	phone VARCHAR(40),
	phone_e164 VARCHAR(16),
	phone_verified_at TIMESTAMP WITHOUT TIME ZONE,
	password_hash VARCHAR(255),
	status VARCHAR(20) NOT NULL,
	commission_rate NUMERIC(5, 2),
	service_area VARCHAR(200),
	terms TEXT,
	notes TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	approved_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_operators_email ON operators (email);
CREATE UNIQUE INDEX IF NOT EXISTS ix_operators_phone_e164 ON operators (phone_e164);
CREATE UNIQUE INDEX IF NOT EXISTS ix_operators_slug ON operators (slug);
CREATE INDEX IF NOT EXISTS ix_operators_status ON operators (status);

CREATE TABLE IF NOT EXISTS phone_codes (
	id SERIAL NOT NULL,
	phone_e164 VARCHAR(16) NOT NULL,
	purpose VARCHAR(20) NOT NULL,
	code_hash VARCHAR(64) NOT NULL,
	session_hash VARCHAR(64) NOT NULL,
	attempts INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	closed_at TIMESTAMP WITHOUT TIME ZONE,
	outcome VARCHAR(20),
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_phone_codes_created_at ON phone_codes (created_at);
CREATE INDEX IF NOT EXISTS ix_phone_codes_phone_e164 ON phone_codes (phone_e164);

CREATE TABLE IF NOT EXISTS settings (
	key VARCHAR(80) NOT NULL,
	value TEXT NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS operator_fares (
	id SERIAL NOT NULL,
	operator_id INTEGER NOT NULL,
	kind VARCHAR(20) NOT NULL,
	title VARCHAR(160) NOT NULL,
	from_location VARCHAR(120) NOT NULL,
	to_location VARCHAR(120) NOT NULL,
	vehicle_class VARCHAR(60),
	seats INTEGER,
	pricing_model VARCHAR(20) NOT NULL,
	price NUMERIC(10, 2),
	base_price NUMERIC(10, 2),
	per_km NUMERIC(10, 2),
	minimum_price NUMERIC(10, 2),
	notes TEXT,
	is_active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
CREATE INDEX IF NOT EXISTS ix_operator_fares_kind ON operator_fares (kind);
CREATE INDEX IF NOT EXISTS ix_operator_fares_operator_id ON operator_fares (operator_id);

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
	operator_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
CREATE INDEX IF NOT EXISTS ix_vehicles_operator_id ON vehicles (operator_id);

CREATE TABLE IF NOT EXISTS bookings (
	id SERIAL NOT NULL,
	reference VARCHAR(12) NOT NULL,
	booking_type VARCHAR(20) NOT NULL,
	vehicle_id INTEGER,
	operator_id INTEGER,
	fare_id INTEGER,
	customer_name VARCHAR(120) NOT NULL,
	email VARCHAR(160) NOT NULL,
	phone VARCHAR(40),
	pickup_location VARCHAR(120) NOT NULL,
	dropoff_location VARCHAR(120) NOT NULL,
	start_date DATE NOT NULL,
	end_date DATE NOT NULL,
	pickup_at TIMESTAMP WITHOUT TIME ZONE,
	pickup_address VARCHAR(240),
	dropoff_address VARCHAR(240),
	passengers INTEGER,
	luggage_count INTEGER,
	flight_number VARCHAR(20),
	pickup_lat NUMERIC(9, 6),
	pickup_lng NUMERIC(9, 6),
	dropoff_lat NUMERIC(9, 6),
	dropoff_lng NUMERIC(9, 6),
	route_distance_m INTEGER,
	route_duration_s INTEGER,
	route_provider VARCHAR(80),
	route_meta TEXT,
	quote_basis VARCHAR(20),
	total_price NUMERIC(10, 2),
	deposit_amount NUMERIC(10, 2) NOT NULL,
	status VARCHAR(20) NOT NULL,
	notes TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITHOUT TIME ZONE,
	request_token VARCHAR(64),
	requested_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id),
	FOREIGN KEY(fare_id) REFERENCES operator_fares (id)
);
CREATE INDEX IF NOT EXISTS ix_bookings_booking_type ON bookings (booking_type);
CREATE INDEX IF NOT EXISTS ix_bookings_operator_id ON bookings (operator_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_bookings_reference ON bookings (reference);
CREATE UNIQUE INDEX IF NOT EXISTS ix_bookings_request_token ON bookings (request_token);
CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings (status);

CREATE TABLE IF NOT EXISTS booking_reviews (
	id SERIAL NOT NULL,
	booking_id INTEGER NOT NULL,
	rating INTEGER NOT NULL,
	comment TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT review_rating_range CHECK (rating >= 1 AND rating <= 5),
	UNIQUE (booking_id),
	FOREIGN KEY(booking_id) REFERENCES bookings (id)
);

CREATE TABLE IF NOT EXISTS commission_entries (
	id SERIAL NOT NULL,
	booking_id INTEGER NOT NULL,
	operator_id INTEGER,
	rate_percent NUMERIC(5, 2) NOT NULL,
	base_amount NUMERIC(10, 2) NOT NULL,
	amount NUMERIC(10, 2) NOT NULL,
	recorded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(booking_id) REFERENCES bookings (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_commission_entries_booking_id ON commission_entries (booking_id);
CREATE INDEX IF NOT EXISTS ix_commission_entries_operator_id ON commission_entries (operator_id);

CREATE TABLE IF NOT EXISTS driver_states (
	operator_id INTEGER NOT NULL,
	vehicle_id INTEGER,
	available BOOLEAN NOT NULL,
	active_booking_id INTEGER,
	lat FLOAT,
	lng FLOAT,
	updated_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (operator_id),
	FOREIGN KEY(operator_id) REFERENCES operators (id),
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id),
	UNIQUE (active_booking_id),
	FOREIGN KEY(active_booking_id) REFERENCES bookings (id)
);

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
  foreach t in array array['phone_codes', 'auth_events', 'vehicles', 'bookings', 'enquiries', 'settings', 'media_assets', 'admin_users', 'schema_migrations', 'operators', 'operator_fares', 'commission_entries', 'driver_states', 'booking_reviews'] loop
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
  ('0001', 'initial schema', now()),
  ('0002', 'no overlapping bookings for one vehicle (postgres)', now()),
  ('0003', 'booking lookup indexes', now()),
  ('0004', 'marketplace: operators, journeys and commission', now()),
  ('0005', 'route planning: coordinates, distance and distance-based fares', now()),
  ('0006', 'driver availability and dispatch', now()),
  ('0007', 'verified completed booking reviews', now()),
  ('0008', 'driver phone sign-in and one request per estimate', now()),
  ('0009', 'verified Gambian numbers in 9-digit form', now()),
  ('0010', 'public API lockdown and index parity', now())
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
