"""CRUD for the shared tracked-product tables (warrant_stocks, tw_option_products,
us_option_products) — see supabase/schema.sql. Plain reads/writes, no
batch/pointer semantics: these are small, low-write tables, unlike the md_*
snapshot tables in db_market.py. Mirrors db.py: no internal try/except,
callers decide how to handle failures.
"""
from services import db


def list_warrant_stocks():
    r = db._run(lambda c: c.table("warrant_stocks").select("code, name").order("code").execute())
    return r.data or []


def add_warrant_stock(code, name=None):
    db._run(lambda c: c.table("warrant_stocks").upsert({"code": code, "name": name}).execute())


def upsert_warrant_stocks(rows):
    """Bulk upsert of [{code, name}] — one round trip instead of one per code.

    Used to seed the picker from the derived underlying list, which is ~600 rows
    and would otherwise be ~600 requests.
    """
    if not rows:
        return 0
    db._run(lambda c: c.table("warrant_stocks").upsert(list(rows)).execute())
    return len(rows)


def remove_warrant_stock(code):
    db._run(lambda c: c.table("warrant_stocks").delete().eq("code", code).execute())


def list_tw_option_products():
    r = db._run(lambda c: c.table("tw_option_products").select("*").order("code").execute())
    return r.data or []


def add_tw_option_product(code, commodity_ids, ticker, exercise_ratio, name=None):
    db._run(lambda c: c.table("tw_option_products").upsert({
        "code": code, "commodity_ids": commodity_ids, "ticker": ticker,
        "exercise_ratio": exercise_ratio, "name": name,
    }).execute())


def remove_tw_option_product(code):
    db._run(lambda c: c.table("tw_option_products").delete().eq("code", code).execute())


def list_us_option_products():
    r = db._run(lambda c: c.table("us_option_products").select("*").order("code").execute())
    return r.data or []


def add_us_option_product(code, adr_ticker, fx_ticker, adr_ratio, name=None):
    db._run(lambda c: c.table("us_option_products").upsert({
        "code": code, "adr_ticker": adr_ticker, "fx_ticker": fx_ticker,
        "adr_ratio": adr_ratio, "name": name,
    }).execute())


def remove_us_option_product(code):
    db._run(lambda c: c.table("us_option_products").delete().eq("code", code).execute())
