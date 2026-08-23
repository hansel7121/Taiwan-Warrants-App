"""The arb matchers must keep producing exactly what they produce today.

`test_arb_logic_no_matches.py` covers only the empty-scan error contract, so
before Phase 3's IV batching or any Rust port, every matcher's real numeric
output is pinned against fixtures recorded from live chains by
`scripts/capture_arb_fixtures.py`. Values are compared at `round(..., 4)`;
`None` and `NaN` stay distinct; emission order is a separate test so an ordering
change is distinguishable from a value change.
"""
import pytest

from tests.logic import arb_golden

SCENARIOS = arb_golden.scenarios()


@pytest.mark.parametrize("name", SCENARIOS)
def test_matcher_output_matches_fixture(name):
    frames, params, expected = arb_golden.load(name)
    got = arb_golden.run(name, frames, params)
    arb_golden.assert_rows_match(got, expected, name)


@pytest.mark.parametrize("name", SCENARIOS)
def test_matcher_emission_order_unchanged(name):
    frames, params, expected = arb_golden.load(name)
    got = arb_golden.run(name, frames, params)
    arb_golden.assert_order_match(got, expected, name)


def test_the_corpus_actually_covers_something():
    """A corpus of empty results would pass every test above while proving nothing."""
    total = sum(len(arb_golden.load(n)[2]) for n in SCENARIOS)
    assert len(SCENARIOS) >= 12
    assert total > 1000, f"only {total} recorded rows across {len(SCENARIOS)} scenarios"


def test_warrant_leg_iv_uses_hardcoded_r_002():
    """`_match_warrants_to_options` prices the warrant leg at r=0.02 while the
    option leg uses `options_logic.R`. That inconsistency is load-bearing for
    every recorded row — this test exists so a future reader sees it is pinned
    deliberately rather than "fixing" it and silently moving every warrant IV."""
    import inspect

    from logic import arb_logic

    src = inspect.getsource(arb_logic._match_warrants_to_options)
    assert "0.02" in src, "the warrant leg's hardcoded r=0.02 disappeared"
    assert "options_logic.R" in src, "the option leg's options_logic.R disappeared"

    frames, params, expected = arb_golden.load("direct_strict")
    ivs = [r["warrant_iv"] for r in expected if r.get("warrant_iv") is not None]
    assert ivs, "direct_strict recorded no warrant IVs to pin"
