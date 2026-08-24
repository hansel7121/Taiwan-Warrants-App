"""Seed warrant_stocks with every underlying that actually has listed warrants.

The picker was fed by a hand-curated table — two dozen codes against the ~600
securities that have warrants listed against them. This derives the full set
from the daily ISIN universe (no new data source: the scrape already holds both
the warrants and the securities) and upserts it.

Upsert, not replace: a code already in the table keeps its row, so anything
added by hand survives. Nothing is ever deleted here — removing a stock stays a
deliberate act through the UI.

    python scripts/seed_warrant_underlyings.py           # dry run, prints the diff
    python scripts/seed_warrant_underlyings.py --apply   # writes
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logic import warrant_logic  # noqa: E402
from services import db_products  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to Supabase")
    ap.add_argument("--limit", type=int, help="only the first N, for a cautious first run")
    args = ap.parse_args()

    derived = warrant_logic.warrant_underlyings()
    if args.limit:
        derived = derived[:args.limit]
    existing = {r["code"]: r.get("name") for r in db_products.list_warrant_stocks()}

    new = [r for r in derived if r["code"] not in existing]
    renamed = [r for r in derived
               if r["code"] in existing and existing[r["code"]] != r["name"]]
    orphan = sorted(set(existing) - {r["code"] for r in derived})

    print(f"derived from the universe : {len(derived)}")
    print(f"already in warrant_stocks : {len(existing)}")
    print(f"  new                     : {len(new)}")
    print(f"  name changed            : {len(renamed)}")
    # Left alone on purpose: an index (TXO) or a delisted underlying still worth
    # keeping is not something a derivation should quietly bin.
    print(f"  in table, not derived   : {len(orphan)}  {orphan if orphan else ''}")

    if new:
        print("\nfirst 10 new:")
        for r in new[:10]:
            print(f"  {r['code']}  {r['name']}")

    if not args.apply:
        print("\ndry run — pass --apply to write")
        return

    rows = new + renamed
    if not rows:
        print("\nnothing to write")
        return
    n = db_products.upsert_warrant_stocks(rows)
    print(f"\nupserted {n} rows")
    print(f"warrant_stocks now holds {len(db_products.list_warrant_stocks())}")


if __name__ == "__main__":
    main()
