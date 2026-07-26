-- Migration 003: add md_warrant_universe.type.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- Why: Phase 6 of the fetch/refresh plan reads this snapshot back as a
-- fallback for warrant_logic._universe() when the in-process cache is cold.
-- Callers filter on the twstock "type" field (e.g. "權證" in v.type) to tell
-- warrant securities apart from their underlyings; without this column the
-- Supabase-sourced universe can't support that filter.

alter table md_warrant_universe add column if not exists type text;
