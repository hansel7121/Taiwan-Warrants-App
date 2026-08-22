"""Loader and comparator for the recorded arb fixtures.

The matchers emit ~35-key rows in which `None` and `NaN` mean different things —
several fields deliberately emit `None` (`opt_bid_per_share`, `warrant_iv`,
`price_diff_pct`) where a missing number would be `NaN` — so the comparison
keeps them distinct rather than normalising both to "empty". Values are compared
at `round(..., 4)`, the precision the app actually displays.
"""
import math

from tests import frame_codec as fc
from tests.conftest import FIXTURE_ROOT

ARB_FIXTURES = FIXTURE_ROOT / "arb"
FRAME_DIR = ARB_FIXTURES / "_frames"

# Identifies a row independently of the order it was emitted in, so a value
# regression and an ordering change fail as separate tests.
KEY_FIELDS = ("warrant_code", "option_contract", "tw_option_contract",
              "us_option_contract", "trade", "direction", "source", "id",
              "K1", "K2", "body_strike", "K", "dte")


def scenarios():
    """Every captured fixture directory name, sorted."""
    return sorted(d.name for d in ARB_FIXTURES.iterdir()
                  if d.is_dir() and (d / "expected.json").exists())


def load(name):
    """(frames, params, expected) for one scenario.

    `expected` is the row list, except for the straddle scenarios, where it is
    `{"rows": [...], "diag": str|None}` — the stage-diagnosis string is the only
    output of an empty straddle scan, so it is pinned alongside the rows.
    """
    d = ARB_FIXTURES / name
    params = fc.read_json(d / "params.json")
    blob_f = fc.read_json(FRAME_DIR / f"{params['frames']}.json")
    frames = {k: fc.load_frame(v) for k, v in blob_f.items()}
    blob = fc.read_json(d / "expected.json")
    if isinstance(blob, dict):
        return frames, params, {"rows": fc.load_rows(blob["rows"]), "diag": blob["diag"]}
    return frames, params, fc.load_rows(blob)


def expected_rows(expected):
    return expected["rows"] if isinstance(expected, dict) else expected


def row_key(row):
    return tuple(str(row.get(f)) for f in KEY_FIELDS)


def _is_nan(v):
    return isinstance(v, float) and math.isnan(v)


def assert_value(field, got, want, where):
    """One field: `None` and `NaN` stay distinct; floats compare at 4 dp."""
    if want is None or got is None:
        assert got is want, f"{where}.{field}: {got!r} != {want!r} (None mismatch)"
        return
    if _is_nan(want) or _is_nan(got):
        assert _is_nan(want) and _is_nan(got), f"{where}.{field}: {got!r} != {want!r}"
        return
    if isinstance(want, bool) or isinstance(got, bool):
        assert bool(got) == bool(want), f"{where}.{field}: {got!r} != {want!r}"
        return
    if isinstance(want, float) or isinstance(got, float):
        assert round(float(got), 4) == round(float(want), 4), \
            f"{where}.{field}: {got!r} != {want!r}"
        return
    assert got == want, f"{where}.{field}: {got!r} != {want!r}"


def assert_rows_match(got_rows, want_rows, name):
    """Same rows with the same values, compared by identity rather than position."""
    assert len(got_rows) == len(want_rows), \
        f"{name}: produced {len(got_rows)} rows, fixture has {len(want_rows)}"
    got = sorted(got_rows, key=row_key)
    want = sorted(want_rows, key=row_key)
    for i, (g, w) in enumerate(zip(got, want)):
        assert set(g) == set(w), (
            f"{name}[{i}] key set differs; "
            f"extra={sorted(set(g) - set(w))} missing={sorted(set(w) - set(g))}"
        )
        for field in w:
            assert_value(field, g[field], w[field], f"{name}[{i}]")


def assert_order_match(got_rows, want_rows, name):
    """Emission order, checked separately so it fails distinguishably from a value change."""
    assert [row_key(r) for r in got_rows] == [row_key(r) for r in want_rows], \
        f"{name}: rows are the same but emitted in a different order"


def run(name, frames, params):
    """Re-run the matcher a fixture was captured from."""
    from logic import arb_logic

    m = params["matcher"]
    if m == "same_type":
        return arb_logic._match_warrants_to_options(
            frames["warrant_df"], frames["opt_df"], params["opt_contract_size"],
            params["max_strike_diff_pct"], params["max_dte_diff"],
            positive_loose=params["positive_loose"])
    if m == "pcp":
        return arb_logic._match_warrants_pcp(
            frames["warrant_df"], frames["opt_df"], params["opt_contract_size"],
            params["max_strike_diff_pct"], params["max_dte_diff"],
            positive_loose=params["positive_loose"], r=params.get("r"),
            synthetic_underlying=params.get("synthetic_underlying", "warrant"))
    if m == "butterfly":
        return arb_logic._match_butterflies(
            frames["warrant_df"], frames["opt_df"], params["opt_contract_size"],
            positive_loose=params["positive_loose"])
    if m == "option_legs":
        return arb_logic._match_option_legs(
            frames["tw_df"], frames["us_df"], params["tw_contract_shares"],
            params["us_contract_shares"], params["max_strike_diff_pct"],
            params["max_dte_diff"], positive_loose=params["positive_loose"])
    if m == "straddle_legs":
        return arb_logic._straddle_legs(
            frames["warrant_df"], frames["opt_df"],
            loose=params["loose"], short_warrants=params["short_warrants"])
    if m == "straddle":
        return run_straddle(frames, params)
    raise AssertionError(f"unknown matcher {m!r}")


def run_straddle(frames, params):
    """`build_straddle_arb` with both fetches replaced by the fixture frames.

    Mirrors `scripts/capture_arb_fixtures.py::run_straddle` — the orchestrator
    fetches for itself, so end-to-end coverage of the pairing loop and the
    diagnosis counters means substituting the reads rather than the frames.
    """
    from logic import arb_logic, options_logic, warrant_logic
    from services import applog

    # _commodity_map is only a membership check here (CONTRACT is hardcoded to
    # 2000 inside build_straddle_arb), so stubbing it keeps the fixture run off
    # Supabase without changing a single output value.
    logged = []
    orig = (warrant_logic.read_warrant, options_logic.read_tw_option, applog.log,
            options_logic._commodity_map)
    warrant_logic.read_warrant = lambda *a, **k: (frames["warrant_df"], None, {})
    options_logic.read_tw_option = lambda *a, **k: (frames["opt_df"], None, {})
    options_logic._commodity_map = lambda: {"2330": {"exercise_ratio": 2000}}
    applog.log = lambda prefix, msg, level="INFO": logged.append(msg)
    try:
        df = arb_logic.build_straddle_arb(
            ["2330"], "All", params["max_strike_diff_pct"], params["max_dte_diff"],
            min_volume=0, min_iv_edge=params["min_iv_edge"], loose=params["loose"],
            short_warrants=params["short_warrants"],
            require_dte_cover=params["require_dte_cover"])
    finally:
        (warrant_logic.read_warrant, options_logic.read_tw_option, applog.log,
         options_logic._commodity_map) = orig
    diag = next((m for m in logged if m.startswith("No straddle rows.")), None)
    return {"rows": df.to_dict(orient="records"), "diag": diag}

