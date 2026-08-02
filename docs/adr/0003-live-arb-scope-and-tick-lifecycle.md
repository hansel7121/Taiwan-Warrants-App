# Live arb-detection scope is warrant-containing strategies only; raw ticks never leave worker memory

The worker runs live-Tick-driven arb detection only for strategies with a warrant leg — initially the three Direct-Match-tab strategies already unified behind `arb_logic.match_warrant_tw_option`'s `strategy` parameter (`same_type`, `pcp`, `butterfly`), dispatched from a registered list so adding Warrant-vs-US-Option later is additive rather than a rewrite. TW-Option-vs-US-Option is permanently excluded from this path: it has no warrant leg, so a live warrant Tick can't produce a fresher signal for it than the existing periodic scan.

Separately: raw Ticks are never persisted or exposed via any route or shared store (no Redis, no `live_ticks` table). They exist only transiently in worker memory, compared against a periodically-refreshed local mirror of option-side data. Only the Arb Signal a check produces is written to the database, for the web app to read.
