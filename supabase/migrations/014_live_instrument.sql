-- Migration 014: instrument tagging for TW-option (TXO) live data (#55).
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
--
-- live_prices/live_depth/live_price_ticks were warrant-only until now. TXO
-- codes (kgisuperpy's api.FutQuote channel) start with a letter, every
-- warrant/stock code is all-digits (services/broker/kgi_client.py::
-- _is_option_code) -- the two code spaces are disjoint, so `code` alone stays
-- a safe key/on_conflict target with no migration needed there. `instrument`
-- is added purely so a row can be labelled/filtered by kind.
alter table live_prices add column if not exists instrument text not null default 'warrant';
alter table live_depth add column if not exists instrument text not null default 'warrant';
alter table live_price_ticks add column if not exists instrument text not null default 'warrant';
