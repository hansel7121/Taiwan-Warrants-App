# Live warrant rows show only websocket-derived prices, never the periodic-scan fallback

Unlike the Options Scanner's `is_live` pattern (which falls back to a settlement-price when no live quote exists), the Live-warrant sub-tab shows purely websocket-derived prices — a row displays the last tick actually received over the broker connection, or nothing at all if no tick has ever arrived for that code.

Behavior: once a code has received at least one live tick, that tick (price + timestamp) stays displayed even after its connection drops or Worker Status leaves `connected` — visually marked stale rather than cleared — since the last real observation is still more useful than blank. A code that has never received a tick (just added to the Watchlist, not yet subscribed) shows nothing, since there is no periodic-scan price to fall back to and no live tick yet to show.

This is a deliberate divergence from the Options Scanner's fallback pattern: the point of this sub-tab is to show genuinely live data, so silently substituting a stale periodic-scan price would undercut the reason the tab exists.
