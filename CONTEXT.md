# Taiwan Warrants App

A local/deployed Flask app for scanning Taiwan warrants and options, computing IV/greeks, and surfacing cross-market arbitrage.

## Language

**Tracked Warrant** (Live Warrant tab):
A warrant code the app is watching on the Fubon live order-book feed. There is exactly one Fubon broker session for the whole app, so the tracked list is shared/global — every user of the Live Warrant tab sees the same set of codes and the same book, not a per-user list.
_Avoid_: Subscribed warrant (that's the transport-level fact — a code has a Fubon subscription; "tracked" is the domain fact — it's on the list a user chose to watch).

**Liquidity Scan** (Live Warrant tab):
The action of ranking one underlying's warrants by traded volume and tracking the top N. Scoped to its underlying: re-running it for an underlying replaces only that underlying's previously scan-tracked warrants, leaving other underlyings' scan-tracked warrants and any manually-tracked warrants untouched.
_Avoid_: Subscribe top liquid (implementation-level name from the original script; "Liquidity Scan" is the user-facing/domain name).

**Tracked Warrant provenance**:
Every Tracked Warrant has a source: `scan` (added by a Liquidity Scan, tagged with the underlying that produced it — prunable by a later scan of that same underlying) or `manual` (added by the plain Add-code box — permanently protected from any scan's replace, never tagged with an underlying). A code that is both manually added and later appears in a scan's top-N stays `manual` and is never converted to `scan`.
