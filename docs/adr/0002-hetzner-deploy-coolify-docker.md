---
status: accepted
---

# Hetzner migration: Coolify + Docker, not systemd + Caddy

The Render → Hetzner migration (infra-only: host move + the `RENDER`-sentinel auth fix, see `project_hetzner_migration` memory) deploys via **Coolify + Docker + Traefik**, superseding the plain **systemd + Caddy** plan originally decided 2026-08-10.

**Why:** a larger multi-service pooled architecture was proposed and deferred (ADR-0001) — but Coolify itself (deploy UI, git-push-to-deploy, automatic HTTPS via Traefik, an auto-generated `sslip.io` domain equivalent to Render's `*.onrender.com`) still earns its setup cost for a single service, and keeps the door open if the app is later split or scaled without re-platforming. Plain systemd + Caddy is fewer moving parts but was reconsidered once Coolify was already being evaluated for the (deferred) pooled design.

**Consequences:** one Coolify service runs the whole Flask app (including Live Warrant once built, per ADR-0001) as a single container. `services/db.py`'s `.env` loader is unaffected — it only `setdefault`s missing vars, so Coolify's injected environment variables take precedence with no code change. Secrets (`SUPABASE_*`, later Fubon credentials) are set via Coolify's environment-variable UI, not a committed `.env`.

**Region — revised 2026-08-19, supersedes Singapore:** issue #68 originally specified a Singapore region host to preserve the low broker latency Render's worker had. The app is instead deployed on a Hetzner **Cost-Optimized CX23 in Helsinki**, confirmed as the permanent host, not a temporary dev/testing stand-in. Cost-Optimized has no Singapore option, and this is a deliberate tradeoff: broker latency from Helsinki to Taiwan will be materially worse than a Singapore host once Live Warrant does real order placement from this process. If/when that latency becomes a real problem, the fix is a separate Singapore host for the broker connector specifically (a scoped exception to ADR-0001's single-process model), not necessarily re-migrating the whole app.
