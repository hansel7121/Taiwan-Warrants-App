-- Migration 008: watchlist — the shared list of codes the pool subscribes to.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- Why: the Live-warrant sub-tab needs somewhere durable to keep which warrant
-- codes are watched live. There is exactly ONE Watchlist, shared by every user
-- and consumed by one Connection Pool (docs/adr/0001) — so this table is NOT
-- per-user: added_by records who typed a code in, but it grants no ownership
-- and never filters a read. A code stays until someone removes it.
--
-- unique (code) is the whole concurrency story: two users adding the same code
-- at once collide on it, and the service upserts on that conflict rather than
-- writing a second row. Capacity is enforced in the service, not here — it
-- depends on the stored Capacity Tiers of every Broker Account, which no
-- constraint on this table can see (docs/adr/0002: an add past capacity is
-- rejected outright, never partially accepted and never evicting a code).
create table if not exists watchlist (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  -- set null, not cascade: a departing user must not silently unwatch codes
  -- somebody else is still relying on.
  added_by uuid references auth.users(id) on delete set null,
  added_at timestamptz not null default now(),
  unique (code)
);
-- No extra index: reads are "every row, ordered by code", which the unique
-- constraint's btree on code already serves, and the table is a handful of
-- rows anyway.
alter table watchlist enable row level security;
-- Neither existing pattern fits: broker_credentials' "own rows" policy keys on
-- auth.uid() = user_id, but this table is deliberately shared and has no owner
-- column to key on; the md_* tables' service-role-only grant is for
-- worker-written data no user edits, and users edit this one. So: any signed-in
-- user may read and edit the shared list. Defense in depth either way — the
-- server reaches this table with the service-role key, which bypasses RLS.
create policy "any authenticated user" on watchlist for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');
grant all on watchlist to service_role;
