# Live prices reach the browser via SSE, diverging from live-arb's polling precedent

This concerns only the worker-to-browser hop — the Live-price cache is still fed by the same KGI/Fubon broker websockets and Connection Pool as the rest of this design; that layer is unchanged.

`live-arb`'s ADR-0006 decided arb-signal delivery goes by polling, reasoning that every live-ish surface in the app already polls and push buys little at the cost of client-side connection-lifecycle complexity. For raw per-tick price display, we're diverging from that: Flask streams updates to the browser via Server-Sent Events (SSE) as soon as the Live-price cache updates, rather than the browser polling on an interval.

SSE specifically (not WebSocket, not Supabase Realtime): the browser only ever receives price updates on this channel, never sends anything back over it, so a one-way stream is sufficient — no need for WebSocket's bidirectionality or Flask's async/websocket support (`flask-sock` or a separate ASGI process). SSE also avoids the browser-side subscription/reconnect/auth complexity that 0006 cited when rejecting Supabase Realtime, since it works over plain HTTP with the browser's native `EventSource` reconnect handling.
