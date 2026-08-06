"""Read/write the shared Watchlist — see supabase/migrations/008_watchlist.sql.

One list, shared by every user, feeding one Connection Pool (docs/adr/0001), so
nothing here filters by user: `added_by` records who typed a code in and grants
no ownership. A code stays until someone removes it.

Adds are capacity-checked against the pool the stored Broker Accounts can
actually open, because an added code that does not fit would sit in the list
looking watched and never tick. The check is here rather than in the database:
capacity is the sum of every account's Capacity Tier, which no constraint on one
table can see. Per docs/adr/0002 an overflowing add is rejected WHOLE — no
partial accept, no evicting somebody else's code — and, like pool.WatchlistDiff,
the rejection comes back as data rather than an exception so the route can
answer 400 instead of catching.

Mirrors credentials.py / desired_state.py: thin wrappers, no internal
try/except — callers decide how to handle failures.

TXO codes (#55) need no changes here: a code is an opaque string end to end
in this module (and pool.py's packing) — which broker channel it routes to is
decided downstream, in kgi_client.py's `_is_option_code`.
"""
from dataclasses import dataclass, field

from services import db
from services.broker import credentials
from services.broker import pool


TABLE = "watchlist"


@dataclass(frozen=True)
class AddResult:
    """What an add attempt actually did. `rejected` non-empty means nothing was
    written at all — the batch is all-or-nothing (docs/adr/0002)."""

    added: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    capacity: int = 0
    watching: int = 0


def list_codes():
    """Every watched code, ordered. Shared list — scoped to nobody."""
    r = db._run(lambda c: c.table(TABLE).select("code").order("code").execute())
    return [row["code"] for row in (r.data or [])]


def add_codes(user_id, codes):
    """Add `codes` to the shared list, or reject the whole batch on overflow.

    Codes already watched are not counted against capacity and are not
    rewritten, so re-adding a code somebody else already watches is free. The
    insert upserts on `code` so two users adding the same code at the same
    moment collide in the database rather than producing a duplicate row.
    """
    wanted = _normalise(codes)
    current = list_codes()
    new = [code for code in wanted if code not in set(current)]

    limit = pool.capacity(pool.accounts_for(credentials.list_all_user_ids()))
    if len(current) + len(new) > limit:
        # Nothing is written: a partial accept would leave the user guessing
        # which of their codes took.
        return AddResult(added=[], rejected=wanted, capacity=limit,
                         watching=len(current))

    if new:
        rows = [{"code": code, "added_by": user_id, "added_at": db._now()}
                for code in new]
        db._run(lambda c: c.table(TABLE).upsert(rows, on_conflict="code").execute())

    return AddResult(added=new, rejected=[], capacity=limit,
                     watching=len(current) + len(new))


def remove_codes(codes):
    """Drop `codes` from the shared list; returns the ones that were on it.

    Removing a code nobody watches is a no-op rather than an error — two users
    can hit remove on the same row and both should get a clean answer.
    """
    wanted = _normalise(codes)
    if not wanted:
        return []
    r = db._run(
        lambda c: c.table(TABLE).delete().in_("code", wanted).execute()
    )
    return [row["code"] for row in (r.data or [])]


def _normalise(codes):
    """Strip and dedupe, order preserved. No case folding: warrant codes are
    numeric, and the code routes in app.py strip only (unlike broker names,
    which are lowercased because they are words)."""
    cleaned = [str(code).strip() for code in (codes or [])]
    return list(dict.fromkeys(code for code in cleaned if code))
