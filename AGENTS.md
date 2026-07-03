# AGENTS.md — operating framework for coding/ops agents

**Read this before changing anything.** Then run `python -m app.cli agent-context`.

## Project purpose

Probability Arena is a **read-only market intelligence and calibration system** for Kalshi prediction markets. It scans, gates, enriches, assesses, researches, forecasts, and — critically — **scores its own forecasts against settled outcomes**. The strategy is deliberate: prove forecasting edge with calibration data *before* any EV or trading capability is even designed.

## Current phase

Through **MVP-004F / OPS-004** (see `docs/ROADMAP.md`): the full read-only loop runs scheduled on EVO-X2, a real-time watcher emits informational signals, promoted signals trigger intelligence refreshes, and two narrow baseball canaries (external research, evidence-aware forecasting) exist behind default-off flags. **No EV, no trading of any kind exists — anywhere.**

## Agent roles

- **Coding agent** — implements a specified milestone in this repo. Must follow this file, `docs/TESTING_POLICY.md`, and `docs/SAFETY_BOUNDARIES.md`.
- **Ops/deployment agent** — deploys to EVO-X2 per `docs/EVO_X2_RUNBOOK.md`. Least-invasive changes only; never mutates other projects on that shared host.
- **Review agent** — checks correctness first, then architecture fit, then the safety greps below.

## Required first steps (every session)

1. `python -m app.cli agent-context` — phase, flags, DB state, boundaries.
2. Read `docs/PROJECT_CANON.md` (architecture) and `docs/SAFETY_BOUNDARIES.md` (hard limits).
3. `git log --oneline -10` — the commit messages are the milestone history.
4. `.venv/bin/python -m pytest -q` — confirm green before touching anything.
5. If the task touches deployment: read `docs/EVO_X2_RUNBOOK.md` and check what commit EVO-X2 is actually on before assuming.

## Allowed capabilities

Everything currently in the repo (see `docs/CAPABILITY_MATRIX.md`): read-only scanning, gating, enrichment, assessment, research, forecasting, outcome sync, calibration, watching, signal workflow, retention of our own operational tables. New work must stay within these unless the milestone explicitly and legitimately extends them.

## Forbidden capabilities (hard boundary — do not implement, scaffold, or "prepare")

EV calculation · trade recommendations · paper trading · portfolio sizing · order placement · wallet/private-key handling · live trading/execution · autonomous trading · crypto wallets. `docs/SAFETY_BOUNDARIES.md` states what milestone would have to be explicitly accepted before each could exist. If a task appears to require one of these, **stop and report back instead of building it**.

## Testing expectations

`docs/TESTING_POLICY.md` in one line: everything green, no live LLM/web calls in unit tests (mock every provider), gated live tests skip by default, migrations get up/down tests, and run the safety grep before declaring done:

```bash
grep -rinE "expected_value|kelly|position_siz|paper_trad|place_order|submit_order|create_order|wallet|recommended_side|trade_recommend|execute_trade" app/ --include="*.py"
```

Expected result: no implementation surface (docstrings stating the boundary are fine).

## Deployment expectations

EVO-X2 is a **shared production host**. User-level systemd only; own directory/venv/SQLite; nothing global; flags roll out per the documented sequences (deploy dark → validate template mode → flip one flag → process 1–3 items → inspect). Update the runbook and deployment report when state changes.

## Report-back format

End milestone work with: what was built (mapped to requirements) · validation (test counts, live-smoke evidence) · safety confirmation (grep + boundary statement) · deployment state (what is/isn't on EVO-X2) · risks/follow-ups. Commit as `<MILESTONE-ID>: <summary>` with a body that documents decisions — the git log is the project's memory.
