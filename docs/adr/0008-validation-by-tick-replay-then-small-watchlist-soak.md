# Validation is an offline Tick-replay harness first, then a small-Watchlist soak — not a staged capital rollout

The worker only ever detects and writes Arb Signals; it never places an order. There is therefore no capital at risk in how fast it is rolled out, and the usual caution of a slow live-money ramp doesn't apply — the thing that can actually be wrong is the detection logic, and that is testable offline.

The primary gate is a dry-run harness that replays recorded or synthetic Ticks through the strategy-check functions. Those are the same `arb_logic` functions the periodic path already calls and already has tests for, so the harness only has to cover the new wiring — tick-to-check plumbing and the option-side mirror it compares against — and it catches bad detection before any live broker connection is involved.

The secondary step is operational rather than about correctness: run live against a Watchlist of one or two warrant codes for a soak period before opening it up to the full list, to shake out login, subscription, disconnect, and reconnect behaviour at a scale where a bug is obvious and cheap.
