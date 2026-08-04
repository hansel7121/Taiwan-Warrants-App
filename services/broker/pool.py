"""Connection Pool — spreads the shared Watchlist across every Broker Account.

Both users' Broker Accounts feed one pool over one Watchlist (docs/adr/0005),
so this module's job is: turn (accounts, watchlist) into a deterministic
code-to-connection assignment, then open one client per assigned connection and
fan every client's Ticks into a single callback.

`assign` is pure and holds all the interesting behaviour; `ConnectionPool` only
does the side-effecting part. That split is what keeps the packing rules
testable without a broker SDK or a network.

Deliberately absent: reconnect/backoff, Worker Status writes, and market-hours
gating. Those belong to the worker's resilience layer (docs/adr/0004, 0009), so
failures here propagate untouched rather than being caught and retried.
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
