# CLI-DECOMPOSITION-DESIGN-001 — safe domain command extraction (2026-07)

Design (not implementation) for decomposing the **6,779-line `app/cli.py` (100
subcommands, 100 dispatch branches)** into domain-owned command modules **without
changing runtime behavior**, shrinking the import/parse blast radius of the frozen
`marketops-run-once` entry point. **Documentation only — no code, test, config,
migration, unit, `.env`, or database changed; EVO-X2 stays pinned at `3f742c9`.**
Grounded in code (`file:line`) and three independent read-only audits (command
inventory, safety/systemd/test contract, import-boundary verification).

## Verdict

Decomposition is **safe and worthwhile**, but its primary value is **not** eliminating
runtime import coupling — that is already gone: every one of the **113 `from
app.services` imports is lazy inside a handler**, and `build_parser()` (5595-6310) and
`main()` (6311-6779) contain **zero `app.*` imports**. The only module-top imports are
`app.adapters.kalshi.KalshiRestAdapter`, `app.models.ScannerRun`, and a
`logging.basicConfig` (`cli.py:42-45`). Therefore today a broken crypto/meme/tennis
*service* cannot break `marketops-run-once` (its import never fires); the **sole
cross-domain coupling that can brick the entry point is a `SyntaxError` anywhere in the
single 6,779-line file**, which currently prevents `python -m app.cli` from loading at
all. The decomposition's real wins are: (1) with **lazy per-command module import**,
`marketops-run-once` parses/imports only `registry` + `common` + `marketops.py` — a
syntax error in an unrelated domain module can no longer break it; (2) each ~150–600-line
module is independently reviewable, testable, and low-merge-conflict; (3) the pending
forecast PRs get a clean home. The risk is entirely **silently breaking a contract**
(command name, arg/default, exit code, provider gate, systemd `ExecStart`, or safety-audit
cleanliness) during the move — mitigated by a golden compatibility manifest test. **Not
implemented; all of it waits until after the 2026-07-23 checkpoint** because `cli.py` is
the frozen active runtime.

## CLI inventory

100 unique subcommands (no `aliases=`, no duplicates; each `add_parser` has exactly one
`if args.command ==` branch), across 17 domains:

| Domain | Count | Notable commands |
|---|---|---|
| crypto-horizon-readiness | 14 | cohort-create/arm/disarm/observe-once/run-job, 8 read-only reports incl. feasibility + candidate-readiness |
| edge-measurement | 12 | edge-precheck (writer), 11 read-only shadow/diagnostic reports |
| meme-news | 12 | meme-scan-once, meme-news-run-once (systemd), 10 read-only reports |
| tennis | 8 | watch-scan/tape-capture writers, live-source/probe validators, reports |
| crypto-discovery-risk | 8 | crypto-scan-once + crypto-risk-assess (**provider-gated**), 6 reports |
| watcher-signals | 6 | watch-once, **watch-loop** (systemd daemon), promote/process, reports |
| crypto-tape-lifecycle | 5 | tape-run-once/session (writers, zero-call), 3 reports |
| marketops | 5 | **marketops-run-once** (systemd), -loop, report/alerts/resolve-alert |
| baseline-scanning | 4 | scan, assess-resolution, enrich-details, **run-baseline** (systemd) |
| retention-aggregation | 4 | **prune-retention** + **aggregate-market-ticks** (systemd), 2 reports |
| polymarket | 4 | polymarket-scan-once + 3 reports |
| cross-venue | 4 | cross-venue-match-once + 3 reports |
| core/canon | 3 | agent-context, pipeline-status, db-stats |
| research-forecasting | 3 | collect-research, forecast, research-canary-report |
| calibration-outcomes | 3 | sync-outcomes, score-forecasts, calibration-report |
| backup-operations | 3 | **backup-db** (systemd), list-db-backups, verify-db-backup |
| frontier-champion-challenger | 2 | frontier-eval-report, champion-challenger-report |

Session ownership: every handler owns its session (`owns_session = session is None` +
lazy `from app.db import get_sessionmaker, run_migrations`) **except 6** — agent-context
(direct read-only engine), backup-db/list/verify (filesystem), disarm-cohort (systemd
only), candidate-readiness-history-report (reads JSONL). **No handler does network or
file I/O directly in `cli.py`** — all provider/HTTP/file effects live in the lazy service
modules. ~76/100 have a test reference (24 read-only reports have none; `scan`/`forecast`
substring-over-match). systemd-invoked (7 units): run-baseline, marketops-run-once,
watch-loop, meme-news-run-once --scheduled, aggregate-market-ticks --scheduled --hours 12,
prune-retention, backup-db (a root-scoped baseline.service also runs run-baseline).

## Import blast radius

At `python -m app.cli marketops-run-once` the import/execution order is:
1. **Module load** — imports `argparse/asyncio/logging/sys`, `KalshiRestAdapter`
   (`cli.py:42`), `ScannerRun` (`:43`); runs `logging.basicConfig(...)` (`:45`, an
   import-time root-logger side effect). All 100 handler *bodies* are defined but not
   executed. **A `SyntaxError`/`ImportError` here bricks every command.**
2. **`build_parser()`** (5595-6310) — pure argparse, **no `app.*` imports**; constructs
   all 100 subparsers. Cannot fail from a service issue.
3. **`main()` dispatch** (6311-6779) — pure branching, **no `app.*` imports**; `return 2`
   fallthrough; `__main__` → `sys.exit(main())`.
4. **Selected handler** — `marketops_run_once()` lazily imports
   `MarketOpsAutopilotService` + `app.db`. Only *this* chain's imports execute.

Blast-radius classes:
- **Parse/syntax (today: whole file)** — one bad line anywhere prevents module load. This
  is the coupling decomposition removes for the hot path (via §Registry lazy import).
- **Shared module-top imports** — `kalshi` + `models` + `basicConfig` are loaded for
  every command; keep them (or move `KalshiRestAdapter` lazy, since only scan/watcher use
  it) but centralize `basicConfig` in the entrypoint so it fires exactly once.
- **Runtime service import (already isolated)** — a crypto/meme/tennis service
  ImportError, missing optional dependency, config side-effect, or circular import can
  only affect its own command, because those imports are lazy. Decomposition preserves
  this and, with lazy module import, extends it to the parse layer too.

## Target architecture

`app/commands/` package; the current 100 handler functions + their dispatch/exit-code
adapters + their `add_parser` blocks move verbatim into domain modules. Validated against
the actual domain coupling (each module owns cohesive commands that share service imports;
no command needs a service another module owns at import time — all service imports stay
lazy).

| Module | Owns (domains) | ~cmds | Service imports (lazy) | Startup on `marketops-run-once`? |
|---|---|---|---|---|
| `registry.py` | name→module map (pure data) | — | none | **yes (data only)** |
| `common.py` | `_add_provider_gate_args`, session/format/exit helpers, `basicConfig` | 0 | none at import | **yes** |
| `core.py` | core/canon (agent-context, pipeline-status, db-stats) | 3 | canon/config/pipeline | no |
| `pipeline.py` | baseline-scanning + research-forecasting + calibration-outcomes | 10 | scanner/enrichment/resolution/research/forecasting/outcomes/calibration | no |
| `watcher.py` | watcher-signals | 6 | watcher/signal_workflow | no |
| `marketops.py` | marketops (5) | 5 | marketops | **yes** |
| `frontier.py` | frontier + champion/challenger | 2 | frontier_eval/champion_challenger | no |
| `edge.py` | edge-measurement | 12 | edge_*/forecast_anchor/trigger_timing/live_market_state | no |
| `forecasts.py` | **forecast-report commands from PR #1/#2** | 2 | forecast_scorability/forecast_reliability | no |
| `crypto.py` | crypto-discovery-risk + crypto-tape-lifecycle | 13 | crypto_scout/crypto_risk*/crypto_provider_*/crypto_tape/crypto_coverage/crypto_retrospect | no |
| `crypto_horizon.py` | crypto-horizon-readiness | 14 | crypto_horizon*/orchestrator/schedule/feasibility/readiness | no |
| `meme.py` | meme-news | 12 | meme_scout/meme_news/meme_mas/meme_shadow/domain_scout | no |
| `tennis.py` | tennis | 8 | tennis_* | no |
| `polymarket.py` | polymarket | 4 | polymarket/polymarket_coverage | no |
| `cross_venue.py` | cross-venue | 4 | cross_venue/xvenue_observation | no |
| `operations.py` | retention-aggregation + backup-operations | 7 | retention/tick_aggregation/backup/db_growth | no |

Each module exposes two pure functions: `add_arguments(subparsers)` (argparse only, no
service imports) and `run(command, args) -> int` (the verbatim dispatch body incl.
`asyncio.run(...)` and the exact exit-code mapping). Service imports stay **inside** the
handler bodies exactly as today. `marketops-run-once` therefore imports only `registry`
(data) + `common` + `marketops.py` at parse+dispatch time — no unrelated domain module is
imported or parsed.

## Registry decision

**Chosen: explicit static registry with lazy per-command module import.** `registry.py`
holds a pure-data map `COMMANDS = {"marketops-run-once": "app.commands.marketops", …}`
(100 entries, no imports). `main()`:
- reads `sys.argv`; if the first token is a **known** command → import **only** that one
  module → `add_arguments` for just that subparser → `parse_args` → `run(cmd, args)`;
- for `--help` / no-command / unknown / ambiguous → import **all** modules, build the full
  parser → identical help text and identical argparse error (`required=True`, `return 2`).

Feasible because the top-level parser has **no global options** (only
`add_subparsers(dest="command", required=True)`), so a single-subcommand parser parses a
known command identically. This is the design that actually shrinks the hot-path blast
radius. **Explicit construction requirement:** the lazy single-subcommand parser must be
built with byte-identical top-level parser kwargs (`prog="python -m app.cli"`, plus
whatever `formatter_class`/`allow_abbrev`/`description` the current `build_parser` uses) so
`usage:`/error strings never drift from the full-parser path; the golden-manifest diff
(C1/C2) validates this, and a single shared `_make_top_parser()` factory should build both
the full and the single-subcommand parsers to guarantee it.

Alternatives compared and **rejected**:
- *Import-all-at-build_parser* (each module's `add_arguments` imported when the parser is
  built): simplest, preserves help trivially, but **re-expands the parse blast radius** (a
  syntax error in any module breaks `build_parser` → breaks `marketops-run-once`). Still a
  large improvement over the monolith (small modules + CI import-smoke test); **acceptable
  as an intermediate** if the lazy single-command parser proves surprising, but not the
  target.
- *importlib/plugin filesystem auto-discovery*: **rejected** — non-deterministic ordering,
  hidden failure modes, harder to golden-test; the objective demands explicit, deterministic
  registration. Prefer explicit over magic.

## Compatibility contract

A machine-readable **golden CLI manifest** generated from `build_parser()` *before* any
command moves, then re-generated and diffed by a test after every phase. Per command:
name; positional args; each option's `dest`, flag strings, `action`, `type`, `default`,
`choices`, `required`, mutual-exclusion; the normalized `--help` text; and, for a
representative **safe read-only** invocation, the exit code and output *shape* (JSON keys
/ text line skeleton). Generation walks `parser._subparsers` (no command execution for the
parser-level manifest). Byte-equivalence targets (from the safety audit):
- **Provider gate** — `_add_provider_gate_args`'s five flags (`--provider-plan`,
  `--allow-provider`/`--deny-provider`/`--confirm-paid-provider` `action=append default=[]`,
  `--yes`) on exactly `crypto-scan-once` + `crypto-risk-assess`; the **asymmetric returns**
  (scan: `0/1` on policy presence, blocked+`--yes`→`1`, dispatch pass-through `:6415`;
  risk: `0` on plan/blocked, count-mapped dispatch `:6438`); zero provider calls before the
  gate passes; `--yes` never authorizes a paid provider; `--deny` overrides.
- **Dry-run/confirm** — literal `persisted=…`/`external_calls=0` strings; cohort-create
  explicit-token `-1`-on-reject→exit 1; observe-once dry-run zero-calls; arm/disarm
  `confirmation_required`.
- **Env-gate refusals** — `ENABLE_REALTIME_WATCHER`/`ENABLE_TICK_AGGREGATION_TIMER`/
  `ENABLE_MEME_NEWS_SCOUT` print their skip message and `return 0` (systemd-facing no-op).
- **Exit codes** — per-branch mapping preserved verbatim (pass-through vs `≥0`-sentinel vs
  status-set vs always-0 vs `return 2` fallthrough; `crypto-horizon-run-job` returns
  `last_exit_code`).
- **Import-time** — `logging.basicConfig(level=INFO, format=…)` fires exactly once.

The manifest contains **no secrets** (agent-context already redacts the DB URL; the
manifest records structure, not values).

## Migration sequence

Low-risk read-only first; marketops + crypto-runtime last; **one domain per commit/PR**;
each phase independently revertible via `git revert` of a single commit.

| Phase | Moves | Files | ~LOC out of cli.py | Tests | EVO dark-validate? | Revertible |
|---|---|---|---|---|---|---|
| **CLI-DECOMP-BASELINE-001** | nothing (golden manifest + compat harness only) | +test, +manifest | 0 | manifest generator + diff test | no | yes |
| **CLI-DECOMP-REGISTRY-001** | scaffold `commands/` (registry, common, lazy main) — commands still resolve to cli.py functions | +registry/common, edit cli.py main | ~small | registry resolves all 100; lazy-import isolation test | no | yes |
| **CLI-DECOMP-OPS-001** | operations + core read-only (db-stats, db-growth-report, tick-aggregation-report, list/verify backup, agent-context, pipeline-status) | +core.py/operations.py | ~600 | manifest diff, per-cmd golden | no | yes |
| **CLI-DECOMP-REPORTS-001** | all read-only report domains (edge, frontier, meme reports, polymarket/cross-venue reports, crypto reports, crypto-horizon reports) | +edge/frontier/meme/polymarket/cross_venue/crypto*/crypto_horizon (reports only) | ~2500 | manifest diff | no | yes (per domain) |
| **CLI-DECOMP-FORECASTS-001** | forecast-scorability + forecast-reliability (**after PR #1/#2 merge**) | +forecasts.py | ~200 | manifest diff | no | yes |
| **CLI-DECOMP-CRYPTO-RUNTIME-001** | crypto-scan-once + crypto-risk-assess (provider-gated), crypto-tape writers, crypto-horizon cohort/observe/arm/disarm/run-job | crypto.py/crypto_horizon.py (writers) | ~1200 | provider-gate golden, dry-run/confirm golden | **yes** | yes (per command) |
| **CLI-DECOMP-MARKETOPS-001** | marketops (5), baseline/scan, watcher, retention/aggregation, sync/score | marketops/pipeline/watcher/operations (writers) | ~1500 | full suite + subprocess import test | **yes + one natural cycle after each** | yes (per command) |
| **CLI-DECOMP-CLEANUP-001** | delete emptied handler bodies; `cli.py` → thin shim (registry + main) | edit cli.py | remainder | no-duplicate-dispatch test | verify import graph | yes |

## Safety-sensitive commands

Must keep validation/confirmation **byte-equivalent** (from the safety contract):
- **Provider-gated / paid-provider-confirm**: `crypto-scan-once`, `crypto-risk-assess`
  (the two using `_add_provider_gate_args`; fail-closed print-then-return, asymmetric exit
  codes, `--yes` never authorizes paid, `--deny` overrides — all verbatim; the gate helper
  in ONE shared `common.py`, never duplicated).
- **Cohort create / arm / disarm**: `crypto-horizon-cohort-create` (`--confirm`),
  `crypto-horizon-arm-cohort`/`disarm-cohort` (`--confirm`; **install/remove user-systemd
  units** — writes-shared-runtime). Preserve `confirmation_required` + status→exit mapping.
- **Horizon observe (live provider)**: `crypto-horizon-observe-once`,
  `crypto-horizon-run-job` (internal one-shot worker; returns `last_exit_code`).
- **Retention (destructive DELETE)**: `prune-retention` (never touches intelligence/
  calibration; dry-run computes only). **Backup**: `backup-db` (non-SQLite → `return 1`),
  list/verify.
- **Outcome-sync / score-create**: `sync-outcomes`, `score-forecasts`, `run-baseline`,
  `marketops-run-once/-loop`.
- **Env-gated systemd no-ops**: watch-loop, aggregate-market-ticks `--scheduled`,
  meme-news-run-once `--scheduled`, marketops-loop.
No forbidden financial capability exists in `cli.py`; the decomposition introduces none.

## Testing architecture

1–20 (from the objective) realized as: a **golden CLI manifest diff** (inventory, no
lost/duplicated command, help/arg/default/exit-code/text/JSON parity, systemd ExecStart
parse-ability); a **provider-policy golden** (plan output + asymmetric exit codes) and
**dry-run/confirm golden**; a **subprocess import-isolation test** asserting `python -c
"import app.commands.marketops"` (and a real `python -m app.cli marketops-run-once
--help`) does **not** import `app.commands.crypto`/`meme`/`tennis`/… (uses
`sys.modules` inspection); **no import-time DB/network/filesystem** (monkeypatch
`socket`/`create_engine`/`open` during `import app.commands.*`); the existing **AST safety
audit** (`frontier_eval.safety_audit()` rglobs `app/**/*.py` → new modules auto-covered;
`files_scanned>30` stays satisfied — splitting *increases* the count) plus the AGENTS.md
text grep; **frozen golden snapshots** per command. **Flaky-output normalization**:
timestamps→`<TS>`, absolute paths→`<PATH>`, durations/`*_ms`→`<DUR>`, DB URL→`<DB>`,
run/cohort ids→`<ID>` via a shared normalizer used by both the generator and the diff.

## Rollout plan

After the freeze lifts (post-2026-07-23): Mac full-suite green → CLI-manifest comparison
vs the pre-decomposition golden → subprocess import-isolation test → **one domain per
commit/PR** → dark-deploy to EVO-X2 (git pull only; no flag/schema change) → **for each
high-risk phase (crypto-runtime, marketops) observe one natural MarketOps cycle** and
confirm `readiness.jsonl` + `marketops_runs` health before the next → rollback = revert
the single commit. **Do not bundle** the CLI decomposition with WAL, transaction
refactoring, lock telemetry, provider/discovery/model/flag/schema changes (each is its
own milestone from `docs/SQLITE_WRITER_TOPOLOGY_2026_07.md`).

## Interaction with the forecast PRs

PR #1 (`FORECAST-SCORABILITY-AUDIT-001`, `forecast-scorability-audit-report`) and PR #2
(`FORECAST-RELIABILITY-DECOMP-001`, `forecast-reliability-decomposition-report`) both add
CLI commands to the monolithic `app/cli.py`. **Sequence: merge PR #1 then PR #2 first**
(after the checkpoint), so their two commands become part of the **pre-decomposition CLI
compatibility baseline** (captured in CLI-DECOMP-BASELINE-001's golden manifest). Only
*then* does CLI-DECOMP-REGISTRY-001 begin. The two forecast-report commands migrate
**together** into `app/commands/forecasts.py` at CLI-DECOMP-FORECASTS-001. This ordering
avoids any duplicate/conflict resolution: the decomposition never rewrites lines the
forecast PRs also touch, because it starts after they are integrated. **Neither PR is
modified by this milestone.**

## Risk register

| ID | Risk | Sev | Mitigation / rollback |
|---|---|---|---|
| C1 | Parser incompatibility (help/usage/error text drift) | high | golden manifest diff gates every phase; revert commit |
| C2 | Changed default / arg `type`/`action` | critical | manifest captures every default/type/action; per-cmd golden |
| C3 | Hidden command lost or duplicated | high | manifest asserts exactly 100 names, 1:1 dispatch; no-duplicate test |
| C4 | Import cycle among command modules | medium | modules import only `common`+lazy services (never each other); import-smoke test |
| C5 | Lazy-import regression (a service import accidentally hoisted to module top) | high | import-isolation test: `marketops-run-once` must not import crypto/meme/tennis modules |
| C6 | Provider-gate bypass / drift | critical | gate helper in one `common.py`; provider-policy golden; asymmetric exit codes asserted |
| C7 | Incorrect session ownership after move | high | preserve `owns_session`+`run_migrations` block verbatim; write-behavior golden |
| C8 | Changed exit code | high | per-branch exit mapping golden; the `≥0`-sentinel and `last_exit_code` cases explicitly tested |
| C9 | systemd ExecStart breakage | critical | the 7 ExecStart command+flag strings asserted parse-identical; systemd untouched |
| C10 | Partial migration leaves duplicate dispatch | medium | registry is the single dispatch source; no-duplicate test; CLEANUP phase verifies |
| C11 | Stale/over-matching test coverage | low | add per-cmd golden for the 24 untested reports before moving them |
| C12 | Oversized PR / merge conflict with pending work | medium | one domain per PR; decomposition starts only after PR #1/#2 merge |
| C13 | `basicConfig` fires twice / wrong order | medium | centralize in the entrypoint; assert single call, identical format |

## First implementation slice

**CLI-DECOMP-REGISTRY-001** (after 2026-07-23) — establishes the pattern and moves only
low-risk read-only commands; touches no marketops, no crypto discovery/horizon runtime, no
config/models/migrations; independently revertible.

- **Files added**: `app/commands/__init__.py`, `app/commands/registry.py` (static
  name→module map), `app/commands/common.py` (`_add_provider_gate_args` moved here +
  session/format/exit helpers + centralized `logging.basicConfig`),
  `app/commands/operations.py` and `app/commands/core.py`,
  `tests/test_cli_manifest_001.py` (golden manifest generator + diff + import-isolation).
- **Files edited**: `app/cli.py` — `main()` gains the lazy registry dispatch; the registry
  resolves every not-yet-moved command back to its existing `cli.py` handler (zero behavior
  change), and resolves the migrated read-only commands to the new modules.
- **Commands moved (all read-only, no writers, no provider gate, no systemd runtime):**
  `db-stats`, `db-growth-report`, `tick-aggregation-report`, `list-db-backups`,
  `verify-db-backup` (operations.py); `agent-context`, `pipeline-status` (core.py).
- **Not touched**: any writer, marketops, crypto scan/risk/horizon, provider gate, config,
  models, migrations, systemd, `.env`.
- **Acceptance**: full suite green; golden manifest identical to the pre-decomposition
  baseline; import-isolation test proves `marketops-run-once` imports neither the moved
  modules' services nor any unrelated domain; AST audit + text grep clean; `git revert`
  restores the monolith exactly.

Nothing above is implemented in this milestone; every phase waits until after the
2026-07-23 candidate-readiness checkpoint, since `app/cli.py` is the frozen active runtime.
