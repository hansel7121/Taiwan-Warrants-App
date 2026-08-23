# Recorded arb fixtures

These pin what `logic/arb_logic.py`'s matchers produce **today**, so the IV
batching and the Rust port can be proved to change nothing. They were recorded
from live CMoney / TAIFEX / yfinance chains by `scripts/capture_arb_fixtures.py`
on unmodified code; `provenance.json` in each directory carries the git SHA, the
IV engine in effect, and the input sizes.

## Layout

```
_frames/{base}__{mutation}.json   input frames, shared by every scenario using them
{scenario}/params.json            the matcher and its arguments (+ which frames)
{scenario}/expected.json          the matcher's output, before any sort_values
{scenario}/provenance.json        where this came from
```

Frames are JSON with an explicit dtype map (`tests/frame_codec.py`), not CSV or
Parquet: the option frames carry `None` in object-dtype columns while the warrant
frames carry `NaN` in float columns, `to_json` renders both as `null`, and a port
that silently swaps one for the other passes every value-level check while
breaking `.fillna`, `> 0` comparisons and the Supabase writer. Parquet would
preserve that too, but only by adding pyarrow to the deploy's requirements for
the sake of test data.

`expected.json` records the **private matcher's** return value, before the
orchestrator's `sort_values`, so the fixture pins the matcher and not pandas'
sort.

A scenario is deleted only when the feature it covers is deleted — the
`straddle_*` set went when the Straddle Vol Arb tab did. That is not the same as
regenerating one, which is what the next section is about.

## Regenerating them

**Don't, to make a failing test pass.** That is the one action that quietly
destroys the safety net these exist to provide: the test goes green and the
regression ships. If a fixture genuinely must change — a deliberate behaviour
change, not a port — it changes in **its own commit**, with the reason written
in the commit message, never in the same PR as the change it is meant to guard.

Note that the fixtures pin current behaviour *including current quirks*. The
`r=0.02` hardcoded for the warrant leg in `_match_warrants_to_options` while the
option leg uses `options_logic.R` is one of them, and it is deliberate: see
`test_warrant_leg_iv_uses_hardcoded_r_002`.

```bash
# Re-record everything from live chains (market hours; needs network)
TZ=Asia/Taipei python scripts/capture_arb_fixtures.py

# Re-derive only expected.json from the committed frames — how you confirm a
# refactor is a no-op offline. Any diff under a scenario directory is a
# behaviour change.
TZ=Asia/Taipei python scripts/capture_arb_fixtures.py --offline
```

Frames and `expected.json` must be captured **together**: re-recording frames
against a moved market while keeping old expectations invalidates the corpus.
