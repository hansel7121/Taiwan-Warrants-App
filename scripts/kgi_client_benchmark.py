"""Issue #47: kgisuperpy vs. pythonnet/QuoteCom tick-latency benchmark.

Opens KGIClient (kgi_client.py, kgisuperpy) and KGIPythonnetClient
(kgi_pythonnet_client.py, pythonnet/QuoteCom) simultaneously under the same
KGI account, subscribes both to the same warrant codes, and appends one CSV
row per tick received on either connection: source, code, price, exchange
timestamp, and local receipt time. Run during market hours, stop with
Ctrl+C, then compare exchange-to-receipt latency per source offline.

Not runnable until #47's own blockers land (see kgi_pythonnet_client.py's
docstring): a Mono runtime in the worker image and KGI's QuoteCom DLLs
vendored under vendor/kgi_quotecom/. Same shape as issue #48.

Env vars (kept out of the app's Supabase-backed credential store — this is a
developer-run comparison tool, not a self-service broker connection):
    KGI_PERSON_ID, KGI_PERSON_PWD        kgisuperpy credential
    KGI_QUOTECOM_TOKEN, KGI_QUOTECOM_SID,
    KGI_QUOTECOM_USER_ID, KGI_QUOTECOM_PASSWORD   QuoteCom credential
    KGI_BENCHMARK_CODES                  comma-separated warrant codes (default below)

Run with:
    TZ=Asia/Taipei python scripts/kgi_client_benchmark.py
"""
import csv
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.broker.kgi_client import KGIClient
from services.broker.kgi_pythonnet_client import KGIPythonnetClient


DEFAULT_CODES = ["065426"]  # actively-trading warrant used in the reference repo's own test

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", f"kgi_client_benchmark_{datetime.now().strftime('%Y%m%d')}.csv",
)

_log_lock = threading.Lock()


def _log_row(source, tick):
    receipt = datetime.now().isoformat(timespec="milliseconds")
    with _log_lock:
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [receipt, source, tick.code, tick.price, tick.qty, tick.ts.isoformat()]
            )
    print(f"{receipt} [{source}] {tick.code} price={tick.price} qty={tick.qty} exchange_ts={tick.ts.isoformat()}", flush=True)


def _make_on_tick(source):
    def _on_tick(tick):
        _log_row(source, tick)
    return _on_tick


def main():
    codes = os.environ.get("KGI_BENCHMARK_CODES", ",".join(DEFAULT_CODES)).split(",")

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["receipt_ts", "source", "code", "price", "qty", "exchange_ts"])

    kgisuperpy_client = KGIClient({
        "person_id": os.environ["KGI_PERSON_ID"],
        "person_pwd": os.environ["KGI_PERSON_PWD"],
    })
    pythonnet_client = KGIPythonnetClient({
        "token": os.environ["KGI_QUOTECOM_TOKEN"],
        "sid": os.environ["KGI_QUOTECOM_SID"],
        "user_id": os.environ["KGI_QUOTECOM_USER_ID"],
        "password": os.environ["KGI_QUOTECOM_PASSWORD"],
    })

    print(f"Logging into both clients, then subscribing to {codes}...", flush=True)
    kgisuperpy_client.login()
    pythonnet_client.login()

    kgisuperpy_client.subscribe(codes, _make_on_tick("kgisuperpy"))
    pythonnet_client.subscribe(codes, _make_on_tick("pythonnet"))

    print(f"Subscribed. Logging to {LOG_PATH}. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        kgisuperpy_client.unsubscribe(codes)
        pythonnet_client.unsubscribe(codes)
        kgisuperpy_client.logout()
        pythonnet_client.logout()


if __name__ == "__main__":
    main()
