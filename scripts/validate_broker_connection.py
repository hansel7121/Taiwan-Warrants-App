#!/usr/bin/env python3
"""Phase 2's validation gate (issue #22): prove a broker client really connects.

Compiling is not evidence — the criterion is a live login plus at least one real
Tick for a subscribed code, which is also the only way the open questions in
kgi_client.py / fubon_client.py (what one "connection" is, KGI's real Capacity
Tier, its tick timestamp format) get settled.

Run it against a REAL account, from the worker environment where the broker SDK
and its native libraries are installed, during Taiwan market hours — outside
them a live account connects fine and simply never prints a trade, which reads
as a timeout here.

Run:  python scripts/validate_broker_connection.py kgi <user_id> 2330 [--timeout 30]

Needs the same env as the worker: SUPABASE_* plus BROKER_CRED_KEY, since the
credential is read and decrypted from broker_credentials.
"""
import argparse
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.broker import pool  # noqa: E402


def main():
    args = _parse_args()

    client = pool.client_class(args.broker).from_stored(args.user_id)
    first_tick = []
    got_tick = threading.Event()

    def on_tick(tick):
        # Ticks arrive on the SDK's thread; hand the first one to the main
        # thread and stop caring about the rest.
        if not got_tick.is_set():
            first_tick.append(tick)
            got_tick.set()

    print(f"logging in to {args.broker} as user {args.user_id} ...")
    client.login()
    print(f"connected={client.is_connected}; subscribing to {args.code} ...")
    client.subscribe([args.code], on_tick)

    try:
        if not got_tick.wait(args.timeout):
            print(f"FAIL: no tick for {args.code} within {args.timeout}s")
            return 1
        print(f"OK: {first_tick[0]}")
        return 0
    finally:
        client.unsubscribe([args.code])
        client.logout()
        print("logged out")


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("broker", choices=["kgi", "fubon"])
    p.add_argument("user_id", help="the Broker Account's owner (auth.users id)")
    p.add_argument("code", help="warrant or stock code to subscribe, e.g. 2330")
    p.add_argument("--timeout", type=float, default=30.0)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main())
