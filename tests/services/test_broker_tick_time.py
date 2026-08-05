"""Tick timestamps: what each broker client stamps a price with.

`_tick_time` is a plain function in each client, so it is called directly — no
client instance, no login, no network. The only setup needed is stub SDK
modules: kgisuperpy and fubon_neo ship in the worker image only, and both client
modules import them at module scope, so importing either one on a dev machine
fails without the stubs (test_broker_worker.py pins the same boundary).

What these say is one thing: the datetime a Tick carries is tz-aware. It is
written to live_prices with .isoformat() (broker_worker._relay_tick), and a
naive value there would be read back as UTC and silently shift every stored
timestamp by the container's offset.
"""
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


def _install_sdk_stubs():
    fubon_sdk = types.ModuleType("fubon_neo.sdk")
    fubon_sdk.FubonSDK = object
    fubon = types.ModuleType("fubon_neo")
    fubon.sdk = fubon_sdk
    sys.modules.setdefault("kgisuperpy", types.ModuleType("kgisuperpy"))
    sys.modules.setdefault("fubon_neo", fubon)
    sys.modules.setdefault("fubon_neo.sdk", fubon_sdk)


_install_sdk_stubs()

from services.broker import fubon_client, kgi_client  # noqa: E402


TPE = ZoneInfo("Asia/Taipei")


# ── KGI: an exchange string, read as Taipei wall-clock time ──────────────

def test_a_parsed_kgi_timestamp_is_labelled_taipei():
    """TWSE prints in one timezone, so the naive string IS Taipei time."""
    ts = kgi_client._tick_time("2026-08-04 13:29:59.123456")

    assert ts.tzinfo == TPE
    assert ts.utcoffset().total_seconds() == 8 * 3600


def test_labelling_does_not_shift_the_wall_clock():
    """`.replace`, not `.astimezone`: the numbers KGI sent are kept as sent."""
    ts = kgi_client._tick_time("2026-08-04 13:29:59")

    assert (ts.year, ts.month, ts.day) == (2026, 8, 4)
    assert (ts.hour, ts.minute, ts.second) == (13, 29, 59)


def test_an_offset_already_on_the_string_is_left_alone():
    """Defensive: if KGI ever prints an offset, do not stamp a second one."""
    ts = kgi_client._tick_time("2026-08-04T13:29:59+00:00")

    assert ts.utcoffset().total_seconds() == 0
    assert ts == datetime(2026, 8, 4, 13, 29, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", [None, "not a timestamp", object()])
def test_the_arrival_time_fallback_is_taipei_aware(raw):
    """A format this cannot parse costs a late timestamp, never a dropped price
    — and the fallback carries an offset just like the parsed path."""
    ts = kgi_client._tick_time(raw)

    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 8 * 3600


# ── Fubon: an epoch value, which is already an absolute instant ──────────

def test_a_fugle_microsecond_epoch_becomes_utc():
    ts = fubon_client._tick_time(1_700_000_000_000_000)

    assert ts == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert ts.utcoffset().total_seconds() == 0


def test_fubon_sub_second_precision_survives():
    """Microseconds are the point of the unit — do not round them away."""
    ts = fubon_client._tick_time(1_700_000_000_123_456)

    assert ts.microsecond == 123456


def test_fubon_timestamps_do_not_depend_on_the_container_timezone():
    """The same instant whatever TZ is set: the epoch value is absolute, and
    the tz= argument is what stops fromtimestamp() reading it as local."""
    assert fubon_client._tick_time(1_700_000_000_000_000).timestamp() == 1_700_000_000
