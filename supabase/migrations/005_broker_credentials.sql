-- Migration 005: broker_credentials — encrypted KGI/Fubon login storage.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- Why: the Singapore worker logs in to KGI/Fubon unattended before market open
-- every trading day, so credentials must persist rather than be re-entered
-- (docs/adr/0002). encrypted_fields is a Fernet token over the credential dict,
-- keyed by the BROKER_CRED_KEY env var — the database never sees plaintext and
-- a database leak alone is not enough to log in as the user.
--
-- Also required, ONCE, by hand in the Supabase dashboard (Storage -> New
-- bucket): a bucket named "broker-certs", PRIVATE (not public), holding KGI's
-- manually generated cert and later Fubon's .pfx at {user_id}/{broker}/cert.ext.
-- A private bucket needs no storage policies: the server uses the service-role
-- key, which bypasses Storage RLS the same way it bypasses table RLS, and a
-- public bucket would serve certs to anyone with the URL.

create table if not exists broker_credentials (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  encrypted_fields text not null,
  -- Capacity tier, populated for every row whatever the broker, defaulted
  -- per-broker on write (kgi 30/2, fubon 200/5). KGI's tier varies by account
  -- and is entered by hand (the API exposes it nowhere), so its default is only
  -- a starting point a caller can override; Fubon's is fixed for every account
  -- and is mirrored here purely so readers get one uniform shape.
  symbols_per_connection int,
  connections int,
  cert_path text,
  created_at timestamptz default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists broker_credentials_user_idx on broker_credentials (user_id);
alter table broker_credentials enable row level security;
-- Per-user table, so it follows portfolio's "own rows" policy rather than the
-- md_* tables' service-role-only grant: defense in depth, since the server
-- itself reaches this table with the service-role key, which bypasses RLS.
create policy "own rows" on broker_credentials for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
