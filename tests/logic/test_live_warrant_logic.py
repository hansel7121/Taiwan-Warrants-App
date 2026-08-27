"""Live Warrant tab decision logic (issue #69): connection assignment, the
2,100-subscription cap, the scan-vs-manual replace rule, and ladder padding.
"""
import pytest

from logic import live_warrant_logic as lwl


# ── assign_slot ──────────────────────────────────────────────────────────────

def test_assign_slot_fills_first_open_connection():
    assert lwl.assign_slot([0, 0]) == 0


def test_assign_slot_first_fit_skips_full_earlier_connections():
    assert lwl.assign_slot([300, 150, 0], max_per_conn=300) == 1


def test_assign_slot_opens_a_new_connection_when_all_full():
    assert lwl.assign_slot([300, 300], max_per_conn=300, max_connections=7) == 2


def test_assign_slot_no_connections_yet_opens_the_first():
    assert lwl.assign_slot([], max_connections=7) == 0


def test_assign_slot_raises_when_pool_fully_saturated():
    full = [300] * 7
    with pytest.raises(lwl.CapacityExceededError):
        lwl.assign_slot(full, max_per_conn=300, max_connections=7)


# ── check_capacity ───────────────────────────────────────────────────────────

def test_check_capacity_allows_change_within_cap():
    lwl.check_capacity(2000, 100, max_total=2100)  # does not raise


def test_check_capacity_allows_change_landing_exactly_on_cap():
    lwl.check_capacity(2000, 100, max_total=2100)


def test_check_capacity_rejects_change_over_cap():
    with pytest.raises(lwl.CapacityExceededError):
        lwl.check_capacity(2000, 101, max_total=2100)


def test_check_capacity_allows_negative_net_change():
    lwl.check_capacity(2100, -50, max_total=2100)  # a net removal never rejects


# ── scan_codes ───────────────────────────────────────────────────────────────

CHAIN = ["A1", "A2", "A3", "A4"]
VOLS = {"A1": 10, "A2": 400, "A3": 0, "A4": 70}


def test_scan_codes_ranks_by_volume_and_takes_top_n():
    assert lwl.scan_codes(CHAIN, VOLS, 2) == ["A2", "A4"]


def test_scan_codes_top_n_zero_takes_the_entire_chain():
    """The whole-chain stress case: every code, still ordered by volume."""
    assert sorted(lwl.scan_codes(CHAIN, VOLS, 0)) == sorted(CHAIN)
    assert lwl.scan_codes(CHAIN, VOLS, 0)[0] == "A2"


def test_scan_codes_top_n_none_is_also_the_entire_chain():
    assert sorted(lwl.scan_codes(CHAIN, VOLS, None)) == sorted(CHAIN)


def test_scan_codes_all_includes_codes_mis_had_no_volume_for():
    """MIS answers for a subset; the rest must still be subscribed, not dropped."""
    partial = {"A2": 400}
    assert sorted(lwl.scan_codes(CHAIN, partial, 0)) == sorted(CHAIN)


def test_scan_codes_falls_back_to_listing_order_when_all_volumes_are_zero():
    """Pre-open, or MIS down: rank by nothing, but never subscribe nothing."""
    zero = {c: 0 for c in CHAIN}
    assert lwl.scan_codes(CHAIN, zero, 2) == ["A1", "A2"]
    assert sorted(lwl.scan_codes(CHAIN, zero, 0)) == sorted(CHAIN)


def test_scan_codes_all_is_still_bounded_by_the_account_cap():
    """scan_codes itself does not cap — plan_scan_replace is what rejects."""
    big = [f"W{i}" for i in range(2200)]
    codes = lwl.scan_codes(big, {}, 0)
    assert len(codes) == 2200
    with pytest.raises(lwl.CapacityExceededError):
        lwl.plan_scan_replace([], "2330", codes, 0)


def test_scan_codes_tops_up_from_listing_order_when_mis_answered_for_too_few():
    """A throttled MIS batch must cost the ranking its confidence, not cost the
    scan its size — top_n codes were asked for, top_n codes come back."""
    partial = {"A2": 400}
    assert lwl.scan_codes(CHAIN, partial, 3) == ["A2", "A1", "A3"]


def test_scan_codes_top_up_never_exceeds_the_chain():
    assert lwl.scan_codes(["A1", "A2"], {"A2": 5}, 10) == ["A2", "A1"]


def test_scan_codes_ignores_volumes_for_codes_outside_the_chain():
    """MIS is keyed independently; a stray code must not be subscribed."""
    assert lwl.scan_codes(CHAIN, {"ZZ": 9999, "A2": 400}, 1) == ["A2"]


# ── scan_shrink_ratio / guard_chain_shrink ───────────────────────────────────

def _scan_rows(codes, underlying="2330"):
    return [{"code": c, "source": "scan", "underlying": underlying} for c in codes]


def test_scan_shrink_ratio_counts_only_this_underlyings_scan_rows():
    existing = _scan_rows(["A1", "A2", "A3", "A4"]) + [
        {"code": "M1", "source": "manual", "underlying": None},
        {"code": "B1", "source": "scan", "underlying": "2317"},
    ]
    assert lwl.scan_shrink_ratio(existing, "2330", ["A1", "A2", "A3"]) == 0.25


def test_scan_shrink_ratio_zero_when_nothing_tracked_yet():
    assert lwl.scan_shrink_ratio([], "2330", ["A1"]) == 0.0


def test_guard_allows_a_whole_chain_rescan_that_barely_shrinks():
    existing = _scan_rows([f"A{i}" for i in range(10)])
    kept = [f"A{i}" for i in range(9)]  # 10% shrink, under the 20% limit
    assert lwl.guard_chain_shrink(existing, "2330", kept, 0) == pytest.approx(0.1)


def test_guard_rejects_a_whole_chain_rescan_that_collapses():
    """The truncated-catalog case: half the chain missing is not a delisting."""
    existing = _scan_rows([f"A{i}" for i in range(10)])
    with pytest.raises(lwl.ChainShrinkError):
        lwl.guard_chain_shrink(existing, "2330", ["A0", "A1", "A2", "A3", "A4"], 0)


def test_guard_force_applies_the_shrink_anyway():
    existing = _scan_rows([f"A{i}" for i in range(10)])
    assert lwl.guard_chain_shrink(existing, "2330", ["A0"], 0, force=True) == 0.0


def test_guard_does_not_gate_a_ranked_top_n_scan():
    """A top-N scan is meant to drop codes that fell out of the ranking."""
    existing = _scan_rows([f"A{i}" for i in range(10)])
    assert lwl.guard_chain_shrink(existing, "2330", ["A0"], 5) == pytest.approx(0.9)


def test_guard_gates_a_top_n_scan_when_the_catalog_came_back_incomplete():
    """An incomplete catalog means the codes that 'fell out' may just be the
    ones the catalog failed to list, so no shrink at all is allowed."""
    existing = _scan_rows([f"A{i}" for i in range(10)])
    with pytest.raises(lwl.ChainShrinkError):
        lwl.guard_chain_shrink(existing, "2330", [f"A{i}" for i in range(9)], 5,
                               catalog_complete=False)


def test_guard_allows_an_incomplete_catalog_that_drops_nothing():
    existing = _scan_rows(["A1", "A2"])
    assert lwl.guard_chain_shrink(existing, "2330", ["A1", "A2", "A3"], 0,
                                  catalog_complete=False) == 0.0


# ── scan_replace ─────────────────────────────────────────────────────────────

def _row(code, source, underlying=None):
    return {"code": code, "source": source, "underlying": underlying}


def test_scan_replace_adds_new_codes_not_already_tracked():
    existing = []
    to_add, to_remove = lwl.scan_replace(existing, "2330", ["A1", "A2"])
    assert to_add == ["A1", "A2"]
    assert to_remove == []


def test_scan_replace_drops_stale_scan_rows_for_same_underlying():
    existing = [_row("A1", "scan", "2330"), _row("A2", "scan", "2330")]
    to_add, to_remove = lwl.scan_replace(existing, "2330", ["A2", "A3"])
    assert to_add == ["A3"]
    assert to_remove == ["A1"]


def test_scan_replace_never_touches_manual_rows():
    existing = [_row("M1", "manual"), _row("A1", "scan", "2330")]
    to_add, to_remove = lwl.scan_replace(existing, "2330", [])
    assert to_add == []
    assert to_remove == ["A1"]
    assert "M1" not in to_remove


def test_scan_replace_never_touches_other_underlyings_scan_rows():
    existing = [_row("B1", "scan", "2317"), _row("A1", "scan", "2330")]
    to_add, to_remove = lwl.scan_replace(existing, "2330", [])
    assert to_remove == ["A1"]
    assert "B1" not in to_remove


def test_scan_replace_already_tracked_code_is_not_re_added():
    existing = [_row("A1", "scan", "2330")]
    to_add, _ = lwl.scan_replace(existing, "2330", ["A1"])
    assert to_add == []


def test_scan_replace_rerunning_same_ranking_is_a_no_op():
    existing = [_row("A1", "scan", "2330"), _row("A2", "scan", "2330")]
    to_add, to_remove = lwl.scan_replace(existing, "2330", ["A1", "A2"])
    assert to_add == []
    assert to_remove == []


# ── plan_scan_replace ────────────────────────────────────────────────────────

def test_plan_scan_replace_rejects_when_net_growth_exceeds_cap():
    existing = [_row(f"OLD{i}", "scan", "2330") for i in range(5)]
    new_codes = [f"NEW{i}" for i in range(50)]
    with pytest.raises(lwl.CapacityExceededError):
        lwl.plan_scan_replace(existing, "2330", new_codes, current_total=2090, max_total=2100)


def test_plan_scan_replace_allows_when_removals_offset_additions():
    existing = [_row(f"OLD{i}", "scan", "2330") for i in range(50)]
    new_codes = [f"NEW{i}" for i in range(50)]
    to_add, to_remove = lwl.plan_scan_replace(existing, "2330", new_codes,
                                               current_total=2100, max_total=2100)
    assert len(to_add) == 50
    assert len(to_remove) == 50


# ── plan_manual_add ──────────────────────────────────────────────────────────

def test_plan_manual_add_true_for_a_new_code_under_cap():
    assert lwl.plan_manual_add({"A1"}, "A2", current_total=10, max_total=2100) is True


def test_plan_manual_add_false_for_an_already_tracked_code():
    assert lwl.plan_manual_add({"A1"}, "A1", current_total=10, max_total=2100) is False


def test_plan_manual_add_rejects_when_at_cap():
    with pytest.raises(lwl.CapacityExceededError):
        lwl.plan_manual_add({"A1"}, "A2", current_total=2100, max_total=2100)


# ── ladder_rows ──────────────────────────────────────────────────────────────

def test_ladder_rows_pads_to_fixed_levels_with_none():
    rows = lwl.ladder_rows(bids=[{"price": 10, "size": 5}], asks=[], levels=5)
    assert len(rows) == 5
    assert rows[0] == {"level": 1, "bid_size": 5, "bid": 10, "ask": None, "ask_size": None}
    assert rows[1] == {"level": 2, "bid_size": None, "bid": None, "ask": None, "ask_size": None}


def test_ladder_rows_truncates_extra_depth_beyond_levels():
    bids = [{"price": p, "size": 1} for p in range(10)]
    rows = lwl.ladder_rows(bids=bids, asks=[], levels=5)
    assert len(rows) == 5
    assert rows[-1]["bid"] == 4


def test_ladder_rows_empty_book_is_all_none():
    rows = lwl.ladder_rows(bids=[], asks=[], levels=5)
    assert all(r["bid"] is None and r["ask"] is None for r in rows)


# ── best_level ───────────────────────────────────────────────────────────────

def test_best_level_reads_only_level_one():
    bids = [{"price": 10, "size": 5}, {"price": 9, "size": 99}]
    asks = [{"price": 11, "size": 3}, {"price": 12, "size": 99}]
    assert lwl.best_level(bids, asks) == {"bid": 10, "bid_size": 5, "ask": 11, "ask_size": 3}


def test_best_level_empty_side_is_none():
    assert lwl.best_level([], []) == {"bid": None, "bid_size": None, "ask": None, "ask_size": None}


# ── best_level_changed ──────────────────────────────────────────────────────

def test_best_level_changed_true_on_first_tick():
    assert lwl.best_level_changed(None, [{"price": 10, "size": 5}], []) is True


def test_best_level_changed_true_when_best_price_moves():
    old = {"bids": [{"price": 10, "size": 5}], "asks": []}
    assert lwl.best_level_changed(old, [{"price": 10.5, "size": 5}], []) is True


def test_best_level_changed_true_when_best_size_moves():
    old = {"bids": [{"price": 10, "size": 5}], "asks": []}
    assert lwl.best_level_changed(old, [{"price": 10, "size": 6}], []) is True


def test_best_level_changed_false_when_only_deep_levels_move():
    old = {"bids": [{"price": 10, "size": 5}, {"price": 9, "size": 1}], "asks": []}
    new_bids = [{"price": 10, "size": 5}, {"price": 8.5, "size": 40}]
    assert lwl.best_level_changed(old, new_bids, []) is False


def test_best_level_changed_false_when_nothing_moves():
    old = {"bids": [{"price": 10, "size": 5}], "asks": [{"price": 11, "size": 2}]}
    assert lwl.best_level_changed(
        old, [{"price": 10, "size": 5}], [{"price": 11, "size": 2}]) is False


# ── parse_warrant_type ───────────────────────────────────────────────────────

def test_parse_warrant_type_call():
    assert lwl.parse_warrant_type("台積電元大11購01") == "Call"


def test_parse_warrant_type_put():
    assert lwl.parse_warrant_type("啟碁台新5A售02") == "Put"


def test_parse_warrant_type_unknown_when_neither_char_present():
    assert lwl.parse_warrant_type("some other name") is None


def test_parse_warrant_type_none_when_name_missing():
    assert lwl.parse_warrant_type(None) is None
    assert lwl.parse_warrant_type("") is None
