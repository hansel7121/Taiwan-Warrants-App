# Live `arb_signals` and periodic `arb_suggestions` stay fully separate — separate tables, separate UI

The live worker writes to `arb_signals`; the scheduler's periodic Direct-Arb scan keeps writing to `arb_suggestions`; nothing merges, dedups, or cross-references the two at the data layer, and the UI shows them as two distinct sections rather than one blended list. The same warrant/option pair can legitimately appear in both at once, and that is accepted as-is.

They look similar but are different features. Their cadences differ by orders of magnitude (tick-driven vs. a 15-minute grid), and so do their staleness semantics — a periodic suggestion is flipped stale on the next scan that no longer finds it, whereas a live signal's meaning is tied to the instant of the Tick that produced it. Sharing a table would force one lifecycle onto both, and dedup at the data layer would hide precisely the disagreement between the two paths that is worth seeing.
