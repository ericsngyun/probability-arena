# Deployment Audit — EVO-X2 (`mikolabs`)

Date: 2026-07-03 (UTC) · Auditor: deployment agent · Phase 1 (read-only; no mutations, no installs, no timers started)

## Host

| Item | Finding |
|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS, kernel `6.17.0-1020-oem` |
| Access | Tailscale SSH, host alias `evo-x2` (100.73.88.88) |
| User / home / shell | `miko_node_001` / `/home/miko_node_001` / `/bin/bash` |
| CPU / RAM | 32 cores, 92 GiB RAM (62 GiB available) |
| GPU stack | ROCm 6.3.1 under `/opt` (irrelevant to this deployment) |
| Disk | `/` 236 G total, **79 G free (65% used)** — ample for this workload (SQLite DB will be MBs) |

## Tooling

| Tool | Finding |
|---|---|
| Python | `python3` 3.12.3 (system), `pip` 24.0, `venv` module OK — **matches dev environment (3.12)**. No `uv`/`poetry`. |
| Git | 2.43.0 |
| Docker | 29.2.1 + Compose v5.1.0, daemon healthy, user has docker access |
| systemd user services | Available. `loginctl`: **Linger=yes** (user timers survive logout/reboot). User manager state: `degraded` — two pre-existing failed units unrelated to this project: `arena-daily.service` (Miko Agent Arena v0 — note: name similarity only, different project) and `syncthing.service`. Not touched. |

## Existing services (shared host — this is a loaded production box)

Docker stack (all long-running, weeks of uptime): `master-postgres` (postgres:16, awaas stack), `pgbouncer` (127.0.0.1:6432), `redis` (8.6), 2× qdrant, `langfuse` (127.0.0.1:3002), `caddy` (127.0.0.1:80/443), `grafana`/`prometheus`/`loki`/`alertmanager`/exporters, `miko-dashboard`, `pleadly-api`/`pleadly-agents`, `litellm-proxy`, `searxng`, `carrystar-agents`, `backup-agent`.

Host-level `postgresql`/`redis-server`/`caddy` systemd services: inactive (everything is containerized).

**Critical detail for DB choice:** the `master-postgres` and `redis` containers do **not** publish ports to the host. The only host-reachable Postgres path is pgbouncer at `127.0.0.1:6432`, whose `[databases]` routing section is **empty** (and `auth_type = any`) — i.e. there is no clean host→Postgres path today, and `master-postgres` belongs to the awaas stack's lifecycle, not ours.

## Project directories

- `~/projects`, `~/miko`, `~/probability-arena`: **absent** (clean slate).
- `/opt/miko/miko-node`: existing Miko node project (2.1 M) — not ours, untouched.
- `~/repos/`, `~/awaas/`, `~/atlas/`, `~/pleadly-cutover-staging/`: other projects, untouched.
- **`probability-arena` repo: not present anywhere on the host.** Remote `https://github.com/ericsngyun/probability-arena.git` (main @ `0cd62b0`) is reachable from EVO-X2 — pushed from the dev machine during this audit (local repo had a configured origin that had never been pushed).

## Env/secrets files (names only; no contents inspected or printed)

Numerous `.env` files exist for *other* projects (`~/awaas/*`, `~/atlas`, `~/repos/prax`, pleadly). None relate to probability-arena.

⚠️ **Flag:** `~/awaas/trading/.env` exists — an adjacent awaas project on this host has a trading component. It is entirely separate from probability-arena. This deployment must share **nothing** with it: no env files, no database, no services. The probability-arena repo itself contains no trading/order/wallet/execution/EV surface (verified by grep at MVP-004B/C/D; re-verified in Phase 2).

## Risks

1. **Shared production host.** Many live services; the deployment must be strictly additive: own directory, own venv, own SQLite file, user-level systemd only. No global pip installs, no system services, no Docker changes.
2. **No clean host→Postgres path.** Using `master-postgres` would require mutating another stack's pgbouncer routing or exec-ing into its container, and couples us to the awaas lifecycle. Rejected.
3. **Pre-existing degraded user units** (`arena-daily`, `syncthing`) — unrelated; left alone; noted so nobody attributes them to this deployment.
4. **Name adjacency:** `arena-daily` (Miko Agent Arena) vs `probability-arena-baseline` — distinct units; naming keeps them distinguishable.
5. **Outbound network:** Kalshi API reachability from EVO-X2 must be smoke-tested (Phase 2).

## Recommended deployment path (least-invasive)

**Host Python venv + systemd user timer** (the preferred path; Docker Compose rejected — it would add a 4th Postgres+Redis set to an already loaded host for no benefit):

- Repo → `~/projects/probability-arena` (fresh clone of `main` @ `0cd62b0`).
- Venv → `.venv` inside the repo; deps via `pip install -r requirements-dev.txt` (repo-documented method; includes pytest for on-host verification).
- **Database → SQLite** at `~/projects/probability-arena/data/probability_arena.db`. Rationale: the full test suite and all live smokes run on SQLite; data volume is tiny; zero coupling to the awaas Postgres; no credentials to manage. Migration to a dedicated Postgres later is a documented follow-up, not a blocker.
- **Redis → left at the default localhost URL, which is unreachable on this host — intentionally.** The baseline CLI pipeline never touches Redis; only the API's candidate-cache uses it, and the app degrades gracefully to a cache-miss path by design (tested).
- LLM/external flags stay disabled: `ENABLE_LLM_RESOLUTION=false`, `ENABLE_EXTERNAL_RESEARCH=false`, `ENABLE_LLM_FORECASTING=false`.
- systemd **user** units adapted from `infra/systemd/` (paths + `%h`, user-level hardening) → `~/.config/systemd/user/`, every 4 h, enabled only after dry-run + live smoke pass.
