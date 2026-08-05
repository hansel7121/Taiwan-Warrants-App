"""Connection Pool — spreads the shared Watchlist across every Broker Account.

Both users' Broker Accounts feed one pool over one Watchlist (docs/adr/0001),
so this module's job is: turn (accounts, watchlist) into a deterministic
code-to-connection assignment, then open one client per assigned connection and
fan every client's Ticks into a single callback.

Two pure functions cover the two moments packing happens: `assign` packs from
scratch at login, `reassign` folds an intraday Watchlist edit into a pool that
is already open and streaming (docs/adr/0002). `ConnectionPool` only does the
side-effecting part. That split is what keeps the packing rules testable
without a broker SDK or a network.

Deliberately absent: reconnect/backoff, Worker Status writes, and market-hours
gating. Those belong to the worker's resilience layer, so failures here
propagate untouched rather than being caught and retried.
"""
from dataclasses import dataclass, field

from services.broker import credentials


# Fill KGI's connections before Fubon's. Which broker carries which code does
# not affect correctness (a Tick is a Tick), so this exists only to make the
# assignment deterministic; same reason for the user_id tiebreak below.
_BROKER_ORDER = ("kgi", "fubon")


@dataclass(frozen=True)
class BrokerAccount:
    """One user's credentials for one broker, plus its Capacity Tier."""

    user_id: str
    broker: str
    symbols_per_connection: int
    connections: int

    @property
    def capacity(self):
        return self.symbols_per_connection * self.connections


@dataclass(frozen=True)
class ConnectionSlot:
    """The codes one connection of one Broker Account will carry."""

    broker: str
    user_id: str
    connection_index: int
    codes: tuple


@dataclass(frozen=True)
class Assignment:
    slots: list = field(default_factory=list)
    unassigned: list = field(default_factory=list)


def capacity(accounts):
    """Total live subscriptions the pool can hold."""
    return sum(a.capacity for a in accounts)


def assign(accounts, watchlist):
    """Pack `watchlist` into the accounts' connections, in order.

    Fills one account's connections before moving to the next, and yields a slot
    only for connections that actually get codes — an idle connection costs a
    login for nothing. Codes past total capacity come back in `unassigned`
    instead of being dropped: over-subscription is a real state the worker has
    to be able to report, not an error.
    """
    remaining = list(watchlist)
    slots = []

    for account in sorted(accounts, key=_account_order):
        for index in range(account.connections):
            if not remaining:
                break
            chunk, remaining = (
                remaining[: account.symbols_per_connection],
                remaining[account.symbols_per_connection :],
            )
            slots.append(
                ConnectionSlot(
                    broker=account.broker,
                    user_id=account.user_id,
                    connection_index=index,
                    codes=tuple(chunk),
                )
            )

    return Assignment(slots=slots, unassigned=remaining)


def live_status(assignment, worker_status_rows):
    """Map each assigned code to whether its connection reports 'connected'.

    Liveness is a property of the connection carrying a code, not of the
    code's own tick recency (issue #46) -- a quiet warrant on a healthy
    connection is live, a reconnecting one isn't. `worker_status_rows` is
    whatever `desired_state.list_all_worker_status()` returned; no status row
    means not live. Codes with no assigned slot are absent from the result
    (not False).
    """
    reported = {
        (row["user_id"], row["broker"]): row["status"] for row in worker_status_rows
    }
    return {
        code: reported.get((slot.user_id, slot.broker)) == "connected"
        for slot in assignment.slots
        for code in slot.codes
    }


@dataclass(frozen=True)
class SlotOp:
    """Codes to subscribe or unsubscribe on one already-open connection."""

    broker: str
    user_id: str
    connection_index: int
    codes: tuple


@dataclass(frozen=True)
class WatchlistDiff:
    """The minimal work an intraday Watchlist edit costs an open pool.

    `assignment` is the pool's new state once the ops are applied, and is what
    gets fed back in as `current` on the next edit.
    """

    subscribes: list = field(default_factory=list)
    unsubscribes: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    assignment: Assignment = field(default_factory=Assignment)


def reassign(accounts, current, watchlist):
    """Fold a Watchlist edit into an already-open pool, touching as little as possible.

    Watchlist edits take effect intraday (docs/adr/0002), and both brokers can
    subscribe/unsubscribe on a live connection without relogin — so the cost of
    an edit should be proportional to the edit, not to the Watchlist. Rerunning
    `assign` would satisfy the packing rules but reshuffle unrelated codes
    across connections, dropping and re-opening subscriptions nobody touched;
    that is why this walks the current slots instead of calling `assign`, even
    though it repeats its ordering. Codes that survive the edit keep the exact
    connection they are already streaming on and generate no op at all.

    Overflow is a rejection, not an overflow bucket: an add that would push the
    Watchlist past total capacity comes back in `rejected` for the UI to
    surface, rather than landing in `unassigned` (which would look subscribed
    but never tick) or evicting somebody else's code (docs/adr/0002). Removals
    are applied before adds are placed, so freeing a code in the same edit that
    adds one leaves room for it.

    Codes stranded by a shrunk Capacity Tier or a removed Broker Account are
    treated as adds and re-placed on a surviving connection; their old slot
    still gets an unsubscribe, so a caller holding that connection can act on it
    before tearing it down.
    """
    ordered = sorted(accounts, key=_account_order)
    live = {
        (account.broker, account.user_id, index)
        for account in ordered
        for index in range(account.connections)
    }

    wanted = list(dict.fromkeys(watchlist))
    wanted_set = set(wanted)

    kept = {}
    unsubscribes = []
    staying = set()

    for slot in current.slots:
        key = (slot.broker, slot.user_id, slot.connection_index)
        stay, leave = [], []
        for code in slot.codes:
            if key in live and code in wanted_set and code not in staying:
                stay.append(code)
                staying.add(code)
            else:
                leave.append(code)
        if key in live:
            kept[key] = stay
        if leave:
            unsubscribes.append(
                SlotOp(
                    broker=slot.broker,
                    user_id=slot.user_id,
                    connection_index=slot.connection_index,
                    codes=tuple(leave),
                )
            )

    room = capacity(accounts) - len(staying)
    added = [code for code in wanted if code not in staying]
    pending, rejected = added[:room], added[room:]

    subscribes = []
    slots = []
    for account in ordered:
        for index in range(account.connections):
            codes = list(kept.get((account.broker, account.user_id, index), ()))
            free = max(account.symbols_per_connection - len(codes), 0)
            chunk, pending = pending[:free], pending[free:]
            if chunk:
                subscribes.append(
                    SlotOp(
                        broker=account.broker,
                        user_id=account.user_id,
                        connection_index=index,
                        codes=tuple(chunk),
                    )
                )
                codes.extend(chunk)
            if codes:
                slots.append(
                    ConnectionSlot(
                        broker=account.broker,
                        user_id=account.user_id,
                        connection_index=index,
                        codes=tuple(codes),
                    )
                )

    return WatchlistDiff(
        subscribes=subscribes,
        unsubscribes=unsubscribes,
        rejected=rejected,
        assignment=Assignment(slots=slots, unassigned=[]),
    )


class ConnectionPool:
    """Opens one broker client per assigned connection and merges their Ticks."""

    def __init__(self, accounts, watchlist, on_tick):
        self.assignment = assign(accounts, watchlist)
        self.on_tick = on_tick
        self.clients = []

    @property
    def unassigned(self):
        """Watchlist codes that exceeded the pool's capacity."""
        return self.assignment.unassigned

    def open(self):
        """Log in and subscribe every slot. Broker failures propagate."""
        for slot in self.assignment.slots:
            client = client_class(slot.broker).from_stored(slot.user_id)
            client.login()
            client.subscribe(list(slot.codes), self.on_tick)
            self.clients.append(client)

    def close(self):
        for client in self.clients:
            client.logout()
        self.clients = []


def _account_order(account):
    broker = account.broker
    rank = _BROKER_ORDER.index(broker) if broker in _BROKER_ORDER else len(_BROKER_ORDER)
    return (rank, account.user_id)


def client_class(broker):
    """Imported lazily so the broker SDKs are only needed when a pool is opened.

    `assign` runs in tests and in the worker's planning step without either SDK
    installed, and neither SDK is present in the web app's environment at all.
    """
    if broker == "kgi":
        from services.broker.kgi_client import KGIClient

        return KGIClient
    if broker == "fubon":
        from services.broker.fubon_client import FubonClient

        return FubonClient
    raise ValueError(f"unknown broker: {broker}")


def accounts_for(user_ids):
    """Build BrokerAccounts from every stored Broker Account of `user_ids`."""
    return [
        BrokerAccount(
            user_id=user_id,
            broker=row["broker"],
            symbols_per_connection=row["symbols_per_connection"],
            connections=row["connections"],
        )
        for user_id in user_ids
        for row in credentials.list_credentials(user_id)
    ]
