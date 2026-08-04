"""The worker's local mirror of the md_* snapshots the web app already writes.

Per docs/adr/0003 the worker never fetches TAIFEX or CMoney itself: it is just
another reader of the tables the periodic scanner writes, refreshed on its own
poll interval. That keeps exactly one copy of the fetching logic in the repo and
guarantees the live path and the periodic scan disagree only about the warrant
price, which is the whole point of the comparison.

Two mirrors, both rebuilt by the same job:

  option side   the chain each Tick is checked against (arb_logic.OptionMirror)
  warrant side  the last known full warrant row per code, which a Tick carries
                only a price for (services/broker/tick_translate.py)
"""
from logic import arb_logic
from services import db_market


def load_option_mirror():
    """Build an OptionMirror from the current md_tw_options batch.

    Contract sizes come from the snapshot's own exercise_ratio rather than
    options_logic._commodity_map(): that map is populated by a TAIFEX fetch, and
    the worker must not be the second thing in the repo hitting TAIFEX.
    """
    df, as_of = db_market.read_snapshot("tw_options")
    if df is None or df.empty:
        return arb_logic.OptionMirror(), as_of
    # Only quotes with a live side can benchmark anything — the same filter
    # match_warrant_tw_option applies before matching, so the live path and the
    # periodic scan see the same chain.
    if "ask_live" in df.columns and "bid_live" in df.columns:
        df = df[df["ask_live"].fillna(False) | df["bid_live"].fillna(False)]
    return arb_logic.OptionMirror(df, contract_sizes(df)), as_of


def contract_sizes(opt_df):
    """underlying code -> shares per option contract, read off the snapshot."""
    if opt_df is None or opt_df.empty:
        return {}
    if "stock_code" not in opt_df.columns or "exercise_ratio" not in opt_df.columns:
        return {}
    sizes = {}
    for code, group in opt_df.groupby(opt_df["stock_code"].astype(str)):
        ratios = group["exercise_ratio"].dropna()
        if not ratios.empty:
            sizes[code] = float(ratios.iloc[0])
    return sizes


def load_warrant_rows():
    """warrant code -> last known full warrant row, for tick translation.

    Keyed on warrant_code because that is what a Tick carries; tick_translate
    rejects a merge whose codes disagree, so the key must be the same string.
    """
    df, as_of = db_market.read_snapshot("warrants")
    if df is None or df.empty or "warrant_code" not in df.columns:
        return {}, as_of
    rows = {}
    for record in df.to_dict(orient="records"):
        code = record.get("warrant_code")
        if code is not None:
            rows[str(code)] = record
    return rows, as_of
