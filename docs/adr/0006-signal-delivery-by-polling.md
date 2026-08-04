# Arb Signals reach the browser by polling, not Supabase Realtime or SSE

The frontend fetches new Arb Signals from a `/list_signals`-style route on an interval while the relevant tab is open, and stops when it isn't. This is the pattern every other live-ish surface in the app already uses — the warrant scanner, the options scanner, and the Suggestions sub-tab all poll — so it needs no new client-side machinery, no connection lifecycle to manage, and no second code path for "what if the socket dropped".

Supabase Realtime (and a hand-rolled SSE endpoint) were both considered and rejected: they buy sub-poll-interval latency on the display hop, which is not where the latency that matters lives, at the cost of subscription/reconnect/auth complexity in the browser for a two-person internal tool.
