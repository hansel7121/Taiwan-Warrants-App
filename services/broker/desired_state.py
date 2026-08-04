"""Read/write the broker connect/disconnect control plane — see supabase/schema.sql.

Two tables, one per direction:

  broker_desired_state  user -> worker   what the user asked for
  worker_status         worker -> user   what the worker actually achieved

Nothing here writes worker_status: a status row is only meaningful if the worker
is its sole author, so the request routes never touch it. This module reads it
for display and leaves the writing to the worker (a later ticket).

Mirrors db_products.py / credentials.py: thin wrappers, no internal try/except —
callers decide how to handle failures.
"""
from services import db


DESIRED_TABLE = "broker_desired_state"
STATUS_TABLE = "worker_status"

BROKERS = ("kgi", "fubon")
DESIRED_STATES = ("connect", "disconnect")
# The four states a worker can report. Mirrors the CHECK constraint on
# worker_status.status; the UI colours each one.
WORKER_STATES = ("connected", "stopped", "reconnecting", "disconnected")


def set_desired_state(user_id, broker, state):
    """Record that `user_id` wants `broker` in `state`. Latest wins.

    Upserted on (user_id, broker) rather than appended, so a user toggling
    connect/disconnect repeatedly leaves one row holding only their final
    intent — the worker has nothing to replay.
    """
    if broker not in BROKERS:
        raise ValueError(f"unknown broker: {broker!r}")
    if state not in DESIRED_STATES:
        raise ValueError(f"unknown desired state: {state!r}")

    row = {
        "user_id": user_id,
        "broker": broker,
        "desired_state": state,
        "requested_at": db._now(),
    }
    db._run(
        lambda c: c.table(DESIRED_TABLE)
        .upsert(row, on_conflict="user_id,broker")
        .execute()
    )
    return row


def get_desired_state(user_id, broker):
    """This user's current intent for `broker`, or None if never requested."""
    r = db._run(
        lambda c: c.table(DESIRED_TABLE)
        .select("broker, desired_state, requested_at")
        .eq("user_id", user_id)
        .eq("broker", broker)
        .limit(1)
        .execute()
    )
    data = r.data or []
    return data[0] if data else None


def list_desired_states(user_id):
    r = db._run(
        lambda c: c.table(DESIRED_TABLE)
        .select("broker, desired_state, requested_at")
        .eq("user_id", user_id)
        .order("broker")
        .execute()
    )
    return r.data or []


def list_worker_status(user_id):
    """Worker-reported status rows for this user's broker accounts.

    Empty until a worker has actually run: absence of a row is itself the
    answer ("no worker has ever reported"), which is why a missing row is not
    synthesized into a fake 'disconnected' here — the UI distinguishes them.
    """
    r = db._run(
        lambda c: c.table(STATUS_TABLE)
        .select("broker, status, changed_at")
        .eq("user_id", user_id)
        .order("broker")
        .execute()
    )
    return r.data or []
