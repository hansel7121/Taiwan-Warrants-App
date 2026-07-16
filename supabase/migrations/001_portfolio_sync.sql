-- Migration 001: make the portfolio table safe for two-way sync.
-- Run this ONCE in the Supabase SQL editor on an existing deployment created
-- from schema.sql. A fresh setup gets the same end state from schema.sql
-- directly and does not need this file.
--
-- Why: with two writers (Render + localhost) a hard DELETE of rows missing from
-- one writer's array silently destroys the other writer's new entries. A
-- tombstone column lets a delete be replicated as an update, and a non-null
-- updated_at gives reconcile a reliable cursor to pull "what changed since X".

alter table portfolio add column if not exists deleted_at timestamptz;

-- updated_at must never be null: it is the sync cursor, and a null row would be
-- invisible to every "updated_at > since" pull. Backfill first, then constrain.
update portfolio set updated_at = coalesce(updated_at, created_at, now()) where updated_at is null;
alter table portfolio alter column updated_at set default now();
alter table portfolio alter column updated_at set not null;

-- Reconcile pulls are always "this user's rows, newest first / since a cursor".
create index if not exists portfolio_user_updated_at_idx on portfolio (user_id, updated_at desc);

-- RLS: the existing "own rows" policy keys on auth.uid() = user_id only, so
-- tombstoned rows stay readable by their owner and no change is needed. Kept
-- explicit here so a re-run repairs a policy that was dropped or narrowed.
drop policy if exists "own rows" on portfolio;
create policy "own rows" on portfolio for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
