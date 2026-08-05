"""The worker's actual code -> connection placement, published for the web app.

broker_worker.py and app.py are separate Render services with no shared memory
(docs/adr/0006), so the web process cannot read the worker's `self._current`.
Recomputing it with `pool.assign` is not equivalent: intraday edits go through
`pool.reassign`, which keeps surviving codes on the connection they are already
streaming on, so a fresh `assign` drifts from reality after the first edit and
`is_live` names the wrong account.

Mirrors desired_state.py / watchlist.py: thin wrappers, no internal try/except —
callers decide how to handle failures.
"""
from services import db


TABLE = "live_assignment"


def publish(assignment):
    """Replace the whole table with `assignment`'s code -> connection mapping.

    One row per code, not per slot: the reader only needs which connection
    carries a code. Called after every apply_watchlist(), so this table mirrors
    the worker's self._current exactly.
    """
    rows = [
        {
            "code": code,
            "broker": slot.broker,
            "user_id": slot.user_id,
            "connection_index": slot.connection_index,
        }
        for slot in assignment.slots
        for code in slot.codes
    ]
    # Delete-then-insert rather than upsert: a code the worker dropped has to
    # leave the table, and the new mapping alone cannot say which rows those are.
    db._run(lambda c: c.table(TABLE).delete().neq("code", "").execute())
    if rows:
        db._run(lambda c: c.table(TABLE).insert(rows).execute())


def read_all():
    """{code: (broker, user_id)} for every code the worker currently carries."""
    r = db._run(lambda c: c.table(TABLE).select("code, broker, user_id").execute())
    return {row["code"]: (row["broker"], row["user_id"]) for row in (r.data or [])}
