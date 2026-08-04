"""The broker-agnostic surface the worker codes against (docs/adr/0003, 0005).

Everything above this layer — the Connection Pool, and later the arb-detection
worker — only ever sees `BrokerClient` and `Tick`, so adding a third broker is a
new module here rather than a change to the worker.

One `BrokerClient` instance is exactly one broker connection in the Capacity
Tier sense, for both brokers we support — KGI's own docs say each
`kgisuperpy.login()` is one connection (see kgi_client.py), and Fubon's limit
counts websocket clients (see fubon_client.py). So there is no separate
`BrokerConnection` type: a second connection is a second client instance, which
is also what lets the Connection Pool treat "which broker" as a detail.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from services.broker import credentials


class BrokerConnectionError(Exception):
    """Login or subscribe failed at the broker. Raised, never swallowed: the
    worker's resilience layer (issue #23) decides what a failure means."""


@dataclass(frozen=True)
class Tick:
    """One live trade print for one code.

    Deliberately a plain dataclass with no serialization: Ticks live only in
    worker memory and are never persisted or exposed (docs/adr/0003), so they
    need no schema, no id, and no round-trip format.
    """

    code: str
    price: float
    ts: datetime
    broker: str


class BrokerClient(ABC):
    """One connection to one broker, opened with one Broker Account's credential.

    Constructed from the dict `credentials.get_credential()` returns: the
    broker's own auth fields plus the account's Capacity Tier
    (`symbols_per_connection`, `connections`). The tier travels with the client
    so a caller can ask a client what it can hold without re-reading the row.
    """

    broker = None

    def __init__(self, credential):
        self._credential = credential
        self.symbols_per_connection = credential.get("symbols_per_connection")
        self.connections = credential.get("connections")

    @classmethod
    def from_stored(cls, user_id):
        """Build a client from the user's stored Broker Account."""
        return cls(credentials.get_credential(user_id, cls.broker))

    @abstractmethod
    def login(self):
        """Open the connection. Raises BrokerConnectionError on failure."""

    @abstractmethod
    def logout(self):
        """Close the connection. Safe to call when never logged in."""

    @abstractmethod
    def subscribe(self, codes, on_tick):
        """Subscribe to live trade prints for `codes`, calling `on_tick(Tick)`.

        `on_tick` is invoked on the SDK's own callback thread, so implementations
        do no work beyond building the Tick.
        """

    @abstractmethod
    def unsubscribe(self, codes):
        """Stop the subscriptions for `codes`."""

    @property
    @abstractmethod
    def is_connected(self):
        """Whether the underlying broker session is currently up."""
