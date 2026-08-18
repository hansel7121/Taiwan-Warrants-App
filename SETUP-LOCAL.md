# Running a local redundancy instance

This guide sets up a **localhost copy** of the app that runs with **no login** and
keeps your portfolio in sync with the shared Supabase database. It also loads your
existing local `portfolio.json` (trade history / PnL) into Supabase the first time.

The model: **Supabase is the single source of truth.** The Render deploy and your
local copy are two doors into the *same* database — a trade added on either side
lands in Supabase, and the other side sees it on its next page load. Your local
`portfolio.json` is kept as a readable backup mirror so you can still view your
portfolio if Render is down.

> ⚠️ **Do the seed (Part B, steps 3–5) BEFORE you run the app locally for the first
> time.** Once local mode runs, it reads Supabase and *overwrites* `portfolio.json`
> with the database contents. If you launch the app before seeding, your local-only
> trades get wiped by the (empty or partial) Supabase copy. **Seed first, run second.**

---

## Part A — One-time coordination (with the Supabase project owner)

1. **Owner adds your email to the allow-list.** In the Supabase SQL editor:
   ```sql
   insert into allowed_users (email, note) values ('you@example.com', 'your name');
   ```
2. **Log into the live site once** (`https://warrant-scanner.onrender.com`) with that
   email via the magic link. This creates your row in Supabase `auth.users` — your
   portfolio rows must reference a real user, so this step is required even though
   local mode itself won't use login.
3. **Owner looks up your UUID:** Supabase dashboard → **Authentication → Users** →
   your email → copy the **User UID** (a UUID). They send it to you.
4. **Owner shares the Supabase secrets with you — securely** (password-manager share
   or another encrypted channel, **not** plain chat/email):
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `SUPABASE_JWT_SECRET` (if used)

   > 🔒 The `service_role` key grants full database access. Only share it with a
   > trusted co-owner of the project.

---

## Part B — Local setup (you run these)

1. **Get the code** and install dependencies (a virtualenv is recommended):
   ```bash
   pip install -r requirements.txt
   ```

2. **Create `.env`** from the template and fill it in:
   ```bash
   cp .env.example .env
   ```
   ```
   SUPABASE_URL=...
   SUPABASE_ANON_KEY=...
   SUPABASE_SERVICE_ROLE_KEY=...
   SUPABASE_JWT_SECRET=...

   APP_ENV=local
   LOCAL_USER_ID=<your UUID from Part A step 3>
   LOCAL_USER_EMAIL=you@example.com
   ```
   `APP_ENV=local` + `LOCAL_USER_ID` set is what enables no-login local mode. Leaving
   `APP_ENV` unset (or setting it to `production`) always requires real login, even
   if `LOCAL_USER_ID` is present.

3. **Put your existing `portfolio.json` in the repo root, then back it up:**
   ```bash
   cp portfolio.json portfolio.json.backup
   ```

4. **Preview the seed (dry run — writes nothing):**
   ```bash
   python scripts/migrate_portfolio_to_supabase.py
   ```
   It prints every trade it *would* upload under your `LOCAL_USER_ID`. Confirm the
   count and titles look right.

5. **Commit the seed for real:**
   ```bash
   python scripts/migrate_portfolio_to_supabase.py --commit
   ```
   This is **additive** — it only inserts/updates your rows and never deletes, so it
   safely *combines* your local `portfolio.json` with anything already in your
   Supabase account (e.g. trades you made on the live site).

6. **Now — and only now — run the app locally:**
   ```bash
   python wsgi.py     # or your usual local launch command
   ```
   It opens with no login and shows your combined portfolio. From here,
   `portfolio.json` is kept as a live backup mirror of Supabase.

---

## What you get

- Add a trade **locally** or on the **live site** → it lands in the shared Supabase;
  the other side sees it on the next page load (synced on load, not real-time across
  already-open tabs).
- `portfolio.json` stays as a local backup, so you can view your portfolio even when
  Render is down.

### Caveat (by design)

This setup handles a **Render** outage, not a **Supabase** outage. If Supabase itself
is unreachable, a trade you add locally is written to the `portfolio.json` backup but
is **not** auto-buffered and replayed to Supabase later — re-enter it once you're back
online. (The "full offline reconcile" behavior was intentionally left out of scope.)
