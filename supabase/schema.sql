-- Supabase schema for Taiwan-Warrants-Web.
-- Run this ONCE in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Creates the allowed_users allow-list, per-user portfolio and custom_stocks
-- tables, enables Row Level Security, and adds policies so each authenticated
-- user can only read/write their own rows.
-- This file is the current end state. An existing deployment created from an
-- older copy is brought up to date by the files in migrations/ instead.

create table allowed_users (email text primary key, note text, created_at timestamptz default now());
-- portfolio.deleted_at is a tombstone (null = live). Two-way sync between the
-- Render and localhost instances replicates a delete as an update; a hard
-- delete would let one writer erase the other's unseen entries. updated_at is
-- the sync cursor and must never be null.
create table portfolio (
  id text not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  payload jsonb not null,
  deleted_at timestamptz,
  created_at timestamptz default now(), updated_at timestamptz not null default now(),
  primary key (user_id, id)
);
create index portfolio_user_updated_at_idx on portfolio (user_id, updated_at desc);
create table custom_stocks (
  user_id uuid primary key references auth.users(id) on delete cascade,
  stocks jsonb not null default '[]', updated_at timestamptz default now()
);
alter table portfolio enable row level security;
alter table custom_stocks enable row level security;
alter table allowed_users enable row level security;
create policy "own rows" on portfolio for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own row" on custom_stocks for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
