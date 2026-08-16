---
status: accepted
---

# Hetzner migration: Coolify + Docker, not systemd + Caddy

The Render → Hetzner migration (infra-only: host move + the `RENDER`-sentinel auth fix, see `project_hetzner_migration` memory) deploys via **Coolify + Docker + Traefik**, superseding the plain **systemd + Caddy** plan originally decided 2026-08-10.

**Why:** a 3-service pooled architecture (`web-api` + `broker-connector` + `redis`) was proposed and its *pooling* deferred (ADR-0001) — but Coolify itself (deploy UI, git-push-to-deploy, automatic HTTPS via Traefik, an auto-generated `sslip.io` domain equivalent to Render's `*.onrender.com`) still earns its setup cost for a single service, and keeps the door open if the app is later split or scaled without re-platforming. Plain systemd + Caddy is fewer moving parts but was reconsidered once Coolify was already being evaluated for the (deferred) pooled design.

**Consequences:** one Coolify service runs the whole Flask app (including Live Warrant once built, per ADR-0001) as a single container. `services/db.py`'s `.env` loader is unaffected — it only `setdefault`s missing vars, so Coolify's injected environment variables take precedence with no code change. Secrets (`SUPABASE_*`, later Fubon credentials) are set via Coolify's environment-variable UI, not a committed `.env`.
