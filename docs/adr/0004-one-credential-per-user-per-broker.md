# One Broker Account per user per broker for now, multiple deferred

`broker_credentials` carries a `unique(user_id, broker)` constraint (ported from `live-arb`'s schema): each user can store at most one KGI credential and one Fubon credential. A user with, say, two KGI accounts can't add both.

Kept deliberately, not revisited for this feature. Multiple credentials per user per broker is a real future need (flagged when the credential form was scoped), but nothing in the Live-warrant sub-tab or the current two-person usage pattern requires it yet, and relaxing the constraint later is a straightforward migration (drop the uniqueness, key rows by an added surrogate id) rather than a structural rework.

## Porting note

`services/broker/credentials.py::upload_cert`'s docstring on `live-arb` is stale — it says "the broker segment stays in the path even though only KGI needs a cert today," but the client code says the opposite: `kgi_client.py::login()` never references a cert path (KGI's cert setup is a manual one-time CLI step, done outside the app), while `fubon_client.py::from_stored()` requires one, downloaded from Supabase Storage. Fix the comment when this file is ported over.
