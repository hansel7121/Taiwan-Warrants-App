-- Migration 006: broker_desired_state + worker_status.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- Why: the UI and the Singapore worker are separate processes that never call
-- each other — the browser cannot reach the worker's box, and the worker is
-- offline whenever the user clicks. So the two halves communicate through these
-- two tables instead: the user writes what they *want* (broker_desired_state),
-- the worker writes what *is* (worker_status), and each side polls the other.
--
-- broker_desired_state is deliberately not a queue: the worker only ever needs
-- the newest intent, and a click-happy user toggling connect/disconnect five
-- times should not make the worker replay five transitions. One row per
-- (user_id, broker), upserted in place — latest wins.

create table if not exists broker_desired_state (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  desired_state text not null check (desired_state in ('connect', 'disconnect')),
  requested_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists broker_desired_state_user_idx on broker_desired_state (user_id);
alter table broker_desired_state enable row level security;
-- Per-user table, so it follows broker_credentials' "own rows" policy: defense
-- in depth, since the server itself reaches this table with the service-role
-- key, which bypasses RLS.
create policy "own rows" on broker_desired_state for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- worker_status is the worker's side of the conversation: one row per broker
-- account, overwritten on every transition. Server-only (RLS on, no policy,
-- like the md_* tables) because a user's own client must not be able to forge a
-- "connected" row — the status is only meaningful if the worker is its sole
-- author. The browser reads it through /broker/status, which is server-side and
-- scopes the query to the caller.
create table if not exists worker_status (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  status text not null check (status in ('connected', 'stopped', 'reconnecting', 'disconnected')),
  changed_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists worker_status_user_idx on worker_status (user_id);
alter table worker_status enable row level security;
grant all on worker_status to service_role;
