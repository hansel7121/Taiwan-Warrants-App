-- Migration 011: live_prices.qty — traded quantity of a code's last print (#54).
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
alter table live_prices add column if not exists qty integer;
