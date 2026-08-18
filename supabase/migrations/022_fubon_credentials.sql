-- Migration 022: fubon_credentials — encrypted Fubon login storage, keyed by
-- label so more than one account (e.g. a different login tested from a
-- different IP) can be stored later without a schema change.
--
-- Why: scripts/fubon_quote_viewer.py currently reads FUBON_ID/FUBON_PASSWORD/
-- FUBON_CERT_PATH/FUBON_CERT_PASSWORD from a local .env, which only works on
-- one machine. encrypted_fields is a Fernet token over {fubon_id,
-- fubon_password, cert_password}, keyed by the BROKER_CRED_KEY env var — the
-- database never sees plaintext, and a database leak alone is not enough to
-- log in as the account.
--
-- Also required, ONCE, by hand in the Supabase dashboard (Storage -> New
-- bucket): a bucket named "broker-certs", PRIVATE (not public), holding the
-- .p12 cert at fubon/{label}/cert.p12. A private bucket needs no storage
-- policies: the server uses the service-role key, which bypasses Storage RLS
-- the same way it bypasses table RLS, and a public bucket would serve certs
-- to anyone with the URL.

create table if not exists fubon_credentials (
  label text primary key,
  encrypted_fields text not null,
  cert_path text,
  created_at timestamptz default now(),
  updated_at timestamptz not null default now()
);

alter table fubon_credentials enable row level security;
-- Server-only secret store: enabled, no policy, same pattern as md_* /
-- cmoney_key. The service-role key bypasses RLS; anon/authenticated stay
-- blocked entirely, which is what we want for credentials.
grant all on fubon_credentials to service_role;
