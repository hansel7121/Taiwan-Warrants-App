"""Read/write the broker connect/disconnect control plane — see supabase/schema.sql.

Two tables, one per direction:

  broker_desired_state  user -> worker   what the user asked for
  worker_status         worker -> user   what the worker actually achieved

A status row is only meaningful if the worker is its sole author, so the request
routes never touch it: `set_worker_status` exists here for proximity to the rest
of the control plane, but the worker process (broker_worker.py) is its only
caller.

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


def list_all_desired_states():
    """Every user's intent, for the worker's reconciliation poll.

    The worker serves all users from one process (docs/adr/0005: both users'
    accounts feed one pool), so unlike the per-user reads above it has no
    caller-scoped user_id to filter on.
    """
    r = db._run(
        lambda c: c.table(DESIRED_TABLE)
        .select("user_id, broker, desired_state, requested_at")
        .order("user_id")
        .execute()
    )
    return r.data or []


def set_worker_status(user_id, broker, status):
    """Record what the worker actually achieved for one Broker Account.

    Upserted on (user_id, broker) like the desired-state side: the UI only asks
    "what is it now", so a transition history would be a log nobody reads. The
    worker calls this on every transition, including the ones it passes through
    (connected -> reconnecting -> disconnected), so a poll landing mid-incident
    sees the honest intermediate state rather than a stale 'connected'.
    """
    if broker not in BROKERS:
        raise ValueError(f"unknown broker: {broker!r}")
    if status not in WORKER_STATES:
        raise ValueError(f"unknown worker status: {status!r}")

    row = {
        "user_id": user_id,
        "broker": broker,
        "status": status,
        "changed_at": db._now(),
    }
    db._run(
        lambda c: c.table(STATUS_TABLE)
        .upsert(row, on_conflict="user_id,broker")
        .execute()
    )
    return row


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
