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
