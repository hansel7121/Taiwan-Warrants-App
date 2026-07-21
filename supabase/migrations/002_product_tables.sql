-- Migration 002: add the shared tracked-product tables (warrant_stocks,
-- tw_option_products, us_option_products) and retire custom_stocks.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment (both Render and localhost point at the same Supabase project).
-- A fresh setup gets the same end state from schema.sql directly and does not
-- need this file.
--
-- Why: the old custom_stocks table was a per-user jsonb blob keyed on
-- user_id, so a warrant added by one user was invisible to every other user.
-- The three tables here replace it with server-only, shared product lists
-- (no per-user scoping, no is_admin gate) — same RLS-enabled-no-policy
-- pattern as the md_* market-data tables: the service role bypasses RLS,
-- every anon/authenticated client is denied. Seeded below with the 24
-- warrant codes live in the app at migration time (the 22 defaults plus 2
-- that had been added via custom_stocks) and the option products currently
-- hard-coded in the app's option list.
--
-- The create/grant block is copied verbatim from schema.sql so this file is
-- self-sufficient and idempotent even if schema.sql was not already applied.

create table if not exists warrant_stocks (
  code text primary key,
  name text,
  created_at timestamptz default now()
);

create table if not exists tw_option_products (
  code text primary key,
  commodity_ids text[] not null,
  ticker text not null,
  exercise_ratio int not null,
  name text,
  created_at timestamptz default now()
);

create table if not exists us_option_products (
  code text primary key,
  adr_ticker text not null,
  fx_ticker text not null,
  adr_ratio numeric not null,
  name text,
  created_at timestamptz default now()
);

alter table warrant_stocks enable row level security;
alter table tw_option_products enable row level security;
alter table us_option_products enable row level security;

grant all on warrant_stocks, tw_option_products, us_option_products to service_role;

-- Seed warrant_stocks: 22 defaults + 2 user-added custom_stocks codes.
insert into warrant_stocks (code, name) values
  ('2330', '台積電 (TSMC)'),
  ('2317', '鴻海 (Foxconn)'),
  ('2454', '聯發科 (MediaTek)'),
  ('2382', '廣達'),
  ('3231', '緯創'),
  ('6669', '緯穎'),
  ('2376', '技嘉'),
  ('3017', '奇鋐'),
  ('3324', '雙鴻'),
  ('2308', '台達電'),
  ('3711', '日月光投控'),
  ('3034', '聯詠'),
  ('2379', '瑞昱'),
  ('3661', '世芯-KY'),
  ('3443', '創意'),
  ('2603', '長榮'),
  ('3008', '大立光'),
  ('2881', '富邦金'),
  ('2882', '國泰金'),
  ('3037', '欣興'),
  ('2303', '聯電 (UMC)'),
  ('2886', '兆豐金'),
  ('0050', '元大台灣50 (ETF)'),
  ('3105', '穩懋')
on conflict (code) do nothing;

-- Seed tw_option_products.
insert into tw_option_products (code, commodity_ids, ticker, exercise_ratio, name) values
  ('TXO',  ARRAY['TXO'],       '^TWII',    50,   '台指選擇權'),
  ('2330', ARRAY['CDA','CDO'], '2330.TW',  2000, '台積電選擇權'),
  ('2303', ARRAY['CCO'],       '2303.TW',  2000, '聯電選擇權 (UMC)'),
  ('2603', ARRAY['CZA','CZO'], '2603.TW',  2000, '長榮選擇權'),
  ('2881', ARRAY['CEO'],       '2881.TW',  2000, '富邦金選擇權'),
  ('2882', ARRAY['CKO'],       '2882.TW',  2000, '國泰金選擇權')
on conflict (code) do nothing;

-- Seed us_option_products.
insert into us_option_products (code, adr_ticker, fx_ticker, adr_ratio, name) values
  ('2303', 'UMC', 'TWD=X', 5,  'UMC (2303 聯電) US options'),
  ('2412', 'CHT', 'TWD=X', 10, 'CHT (2412 中華電) US options')
on conflict (code) do nothing;

-- custom_stocks is now fully superseded by warrant_stocks above.
drop policy if exists "own row" on custom_stocks;
drop table if exists custom_stocks;
