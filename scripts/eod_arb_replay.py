"""End-of-day arb replay CLI: replays one day's TSMC tick log through Direct
Match + the static-arb LP (Python implementations, logic/eod_arb_replay.py),
plus the StatArb required-vol ranking when the day's spot file exists, and
stores the results in Supabase (services/db_eod_arb.py). Launched as a
subprocess by services/scheduler.py after the close, and by the EOD Replay
subtab's Re-run button; also runnable by hand.

    TZ=Asia/Taipei python scripts/eod_arb_replay.py                     # today's recording
    TZ=Asia/Taipei python scripts/eod_arb_replay.py --date 2026-10-07
    TZ=Asia/Taipei python scripts/eod_arb_replay.py --csv ticks.csv --spot-csv spot.csv --dry-run --out episodes.json
"""
import argparse
import json
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logic import eod_arb_replay  # noqa: E402
from services import live_tick_log  # noqa: E402


def _jsonable(v):
    """Plain-JSON copy: numpy scalars -> Python, NaN/inf -> None, dates -> ISO."""
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _date_from_name(path):
    m = re.search(r"(\d{8})", Path(path).name)
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def _log(msg):
    print(f"EOD: {msg}", flush=True)


def run(trade_date=None, csv_path=None, dry_run=False, run_lp=True, spot_path=None):
    """Replay one day and (unless dry_run) store it; returns the result summary dict."""
    if csv_path is None:
        trade_date = trade_date or datetime.now(live_tick_log.TW_TZ).date()
        csv_path = live_tick_log.existing_path_for(trade_date)
        spot_path = spot_path or live_tick_log.existing_path_for(trade_date, spot=True)
    elif trade_date is None:
        trade_date = _date_from_name(csv_path)

    db = None
    if not dry_run:
        from services import db_eod_arb as db

    if csv_path is None or not Path(csv_path).exists():
        _log(f"{trade_date}: no tick file — nothing to replay")
        if db and trade_date:
            db.mark_running(trade_date, None)
            db.finish_run(trade_date, {"status": "no_ticks", "n_ticks": 0})
        return {"trade_date": str(trade_date), "status": "no_ticks", "direct": [], "lp": [], "statarb": []}

    if trade_date is None:
        first = next(eod_arb_replay.iter_ticks(csv_path), None)
        trade_date = first["ts"].date() if first else datetime.now(live_tick_log.TW_TZ).date()

    spot_ticks = eod_arb_replay.iter_spot(spot_path) if spot_path and Path(spot_path).exists() else []
    _log(f"{trade_date}: replaying {csv_path} (lp={'on' if run_lp else 'off'}, "
         f"spot rows={len(spot_ticks):,}{'' if spot_ticks else ' — StatArb ranking skipped'})")
    if db:
        db.mark_running(trade_date, Path(csv_path).name)
    try:
        result = eod_arb_replay.replay(
            eod_arb_replay.iter_ticks(csv_path), trade_date, run_lp=run_lp, spot_ticks=spot_ticks,
            progress=lambda s: _log(f"{s['n_ticks']:,} ticks, {s['n_scans']:,} scans, "
                                    f"{s['n_lp_screens']:,} LP screens, {s['n_lp_solves']:,} full LP solves"))
        st = result["stats"]
        if st["n_ws"] == 0:
            # Only snapshot/seed rows — the exchange never ticked (a holiday the
            # weekday-only market gate let through), so every quote is stale.
            result["direct"], result["lp"], result["statarb"] = [], [], []
        direct = _jsonable(eod_arb_replay.direct_records(result["direct"], trade_date))
        lp = _jsonable(eod_arb_replay.lp_records(result["lp"], trade_date))
        statarb = _jsonable(eod_arb_replay.statarb_records(result["statarb"], trade_date))
        fields = {
            "status": "ok" if st["n_ws"] else "no_ticks",
            "n_ticks": st["n_ticks"], "n_changes": st["n_changes"], "n_scans": st["n_scans"],
            "n_lp_screens": st["n_lp_screens"], "n_lp_solves": st["n_lp_solves"], "n_direct": len(direct), "n_lp": len(lp),
            "first_tick_at": _jsonable(st["first_ts"]), "last_tick_at": _jsonable(st["last_ts"]),
            "runtime_s": st["runtime_s"], "error": None,
        }
        if db:
            db.replace_episodes(trade_date, direct, lp)
            try:
                db.replace_statarb(trade_date, statarb)
            except Exception as e:   # table missing until migration 028 is applied; don't lose the day's episodes
                _log(f"{trade_date}: StatArb top {len(statarb)} not stored ({type(e).__name__}: {e}) — "
                     f"apply supabase/migrations/028_eod_arb_statarb_top.sql")
            db.finish_run(trade_date, fields)
    except Exception as e:
        _log(f"{trade_date}: FAILED: {type(e).__name__}: {e}")
        if db:
            db.finish_run(trade_date, {"status": "error", "error": f"{type(e).__name__}: {e}"})
        raise

    _log(f"{trade_date}: {fields['n_ticks']:,} ticks -> {len(direct)} Direct + {len(lp)} LP "
         f"episodes, {st['n_statarb_scores']:,} StatArb scores in {fields['runtime_s']}s")
    return {"trade_date": trade_date.isoformat(), **fields, "direct": direct, "lp": lp, "statarb": statarb}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=date.fromisoformat, help="trading day (default: today, Taipei)")
    ap.add_argument("--csv", help="replay this tick file instead of the recorder's file for --date")
    ap.add_argument("--spot-csv", help="2330 spot file for --csv (default with --date: the recorder's spot file)")
    ap.add_argument("--dry-run", action="store_true", help="don't touch Supabase")
    ap.add_argument("--no-lp", action="store_true", help="Direct Match only")
    ap.add_argument("--out", help="also write the result (summary + episodes) as JSON here")
    args = ap.parse_args(argv)

    result = run(args.date, args.csv, dry_run=args.dry_run, run_lp=not args.no_lp, spot_path=args.spot_csv)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        _log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
