# Probability Arena

**Kalshi read-only market intelligence** (CRYPTO-002: measurement loop, baseline runner, real-time watcher, retention, signal workflow, baseball + soccer external research canaries, an evidence-aware baseball forecaster, Crypto Arena — a read-only Solana memecoin surveillance lane with a risk engine — and a MarketOps Autopilot coordinating all of it, still strictly read-only).

Scans active Kalshi markets over the public REST API, ranks them on tradability signals (spread, liquidity, volume, time to expiration, resolution clarity), and stores time-series snapshots in Postgres. Optionally maintains live orderbook snapshots over WebSocket when API credentials are configured.

## For coding agents

Start with **[`AGENTS.md`](AGENTS.md)** and run `python -m app.cli agent-context` — together they give the project phase, architecture canon, feature-flag state, allowed/forbidden capabilities, and the testing/deployment policies. The full canon lives under [`docs/`](docs/) (PROJECT_CANON, SAFETY_BOUNDARIES, CAPABILITY_MATRIX, ROADMAP, EVO_X2_RUNBOOK, FEATURE_FLAGS, TESTING_POLICY, ADRs).

## Safety notes

- **Read-only by design. No order placement exists.** There is no trading, betting, order placement, wallet, execution, portfolio-sizing, or paper-trading code anywhere in this repo — the REST adapter only issues GETs (market list, market/event/series detail), the WebSocket client only sends channel subscriptions, and the CLI commands (`scan`, `enrich-details`, `assess-resolution`, `collect-research`) only read market data and write to our own database.
- **Forecasts are probabilities and reasoning artifacts only.** No EV calculation, no position sizing, no paper trading, no trade recommendations, no execution. The forecast schema deliberately has no trade/EV/sizing fields, and tests assert the absence of trading language in forecast output.
- **Calibration is read-only scoring.** Settlement outcomes are synced via plain detail GETs (no trading permissions needed) and forecasts are scored with Brier / log loss / absolute error. Nothing here calculates EV, recommends trades, paper trades, or executes orders.
- **The baseline runner is still read-only.** MVP-004D schedules the measurement loop and records audit rows; it exists to *accumulate calibration data* — the evidence base needed before any EV or paper-trading milestone can even be evaluated. It adds no trading capability of any kind.
- **Opportunity signals are informational only.** OPS-002's watcher records price ticks and deterministic signals (what moved, why, with evidence) for human/research review. Signals carry no EV, no sizing, no trade directives.
- **Goalserve fallback validation (TENNIS-GOALSERVE-001).** `tennis-goalserve-probe --probes N --interval-sec N --top N` tests the pre-registered fallback after API-Tennis's decisive live-state failure: bounded repeated fetches of Goalserve's tennis live feed (hard caps: ≤8 probes/≤10 calls; key is path-embedded so URLs are never logged — reports carry a masked display URL only; no request without `GOALSERVE_TENNIS_API_KEY`, default empty), normalized into the same fixture shape the tape linker consumes (sets/game/point/serve/in-play state), matched against current Kalshi candidates, with per-probe state-change detection and verdicts (`goalserve_pass` / `partial_tune_once` / `fail_market_only_or_sportradar` / `insufficient_live_window` / `no_key`). Persists nothing; provider validation only — not a model, not EV, never advice.
- **Live-feed validation probe (TENNIS-LIVE-FEED-002).** `tennis-api-livefeed-probe --duration-sec N --top N` answers the measured gap from the tape sessions (REST `get_livescore` = 0 rows in 25 probes while Kalshi books moved 24 points in-play): does the provider's documented WebSocket emit usable live ITF/Challenger state? Bounded connection (hard cap 300s, key host-only and never printed, connection errors reported by type name only), events correlated to current Kalshi candidates through the existing tape linker, side-by-side REST comparison, honest verdicts (`api_tennis_ws_pass` / `partial` / `fail_goalserve_next` / `insufficient_live_window` / `no_key`) with a pre-registered Goalserve fallback plan in the research doc. Persists nothing; provider validation only — not a model, not EV, never advice.
- **Synchronized tennis tape (TENNIS-TAPE-001, Phase 0).** `tennis-tape-capture-once --limit N [--dry-run]` runs one manual bounded capture: a score pass over the configured provider (hard cap 4 fixture calls, deduped by date), one chunked market-quote pass over live tennis candidates, and honest linking (`source_backed_link` / `fuzzy_candidate` / `unresolved` / `provider_no_match` / `incompatible_market_type`), persisting ONLY tape rows (runs / score snapshots / market snapshots / links — migration 0025); dry-run persists nothing; no signals, no timer, no scheduled path. `tennis-tape-report --hours N` replays the tape's volumes, link quality, freshness, and score-to-market deltas. Measurement infrastructure only — no models, not EV, not trading, never advice.
- **Tennis provider research + scaffold (TENNIS-PROVIDER-001).** `docs/TENNIS_PROVIDER_RESEARCH_2026_07_10.md` compares live-score providers against the measured universe (~79% ITF-family, ~15% Challenger): **API-Tennis** recommended for first bounded validation (documented Challenger/ITF coverage, 14-day trial, $40/mo entry), **Goalserve** fallback (explicit all-tier claim, 5s point-by-point, 30-day trial); Sportradar disqualified for this universe (ITF removed from its Tennis API in 2025); ESPN retired (measured 0/176). Includes an `api_tennis` fetcher scaffold behind the existing provider selection — **no request without `TENNIS_PROVIDER_API_KEY` (default empty, never committed, never echoed)**; adapts fixtures into the shape TENNIS-001's event matching already consumes. Provider research/validation plumbing only — no models, not EV, never advice; TENNIS-TAPE-001 stays parked pending a passed validation and explicit acceptance.
- **Tennis tick watcher (TENNIS-WATCHER-001).** Closes the market-side half of the tennis observation gap: `tennis-watch-scan-once --limit N [--dry-run] [--scheduled]` runs one bounded read-only quote pass over active tennis markets (match-winner first) through the existing Kalshi adapter and records plain `market_price_ticks` rows — same table, shape, and retention as the realtime watcher, with NO signal detection and NO watcher-run rows. `tennis-watch-report --hours N` shows active-vs-tick-covered tennis markets, freshness, quote completeness, and series/market-type mixes (DB-only). The scheduled entry point no-ops unless `ENABLE_TENNIS_TICK_WATCHER=true` (default false; no timer installed); manual runs always allowed. Market observation only — no trading, no forecasts, not EV, never advice.
- **Tennis live-source validation (TENNIS-LIVE-SOURCE-001).** `tennis-live-source-report --top N --hours N` validates whether persisted tennis markets can map to source-backed live match state, using the existing TENNIS-001 provider scaffold only: ticker → players → tour/date parsing (structural validation always), then per-candidate scoreboard event matching when a provider is configured. With the default `TENNIS_RESEARCH_PROVIDER=template` nothing is fetched and every row reports an honest `provider_gap`; with a configured provider, fetches are read-only and bounded (once per tour/date, hard cap). Reports classification mix, provider match rate, missing player mappings, freshness/lag when measurable, and stale-provider warnings. Coverage measurement only — no probability updates, not EV, never advice.
- **Live market/state observation (LIVE-MARKET-001).** `live-market-state-report --domain sports_tennis --top N [--hours N]` observes currently-ticking markets in a domain from persisted rows only: quote quality (tight/moderate/wide/missing), market-update freshness, volatility diagnostics over 1m/5m/10m tick windows (`volatile_state`/`calm_state`/`insufficient_live_data` — diagnostic labels, never signals), spread/liquidity drift, quote instability, and a tennis match-winner state scaffold that extracts set/game/server/status from persisted TENNIS-001 research packets when a source-backed packet exists — otherwise `template_only` with an explicit `provider_gap` (no validated live tennis score feed; nothing fabricated, nothing fetched). Foundation for future in-game research observation only — not EV, not trading, never advice; changes no gate/forecast/promotion/flag/automation.
- **Cost-adjusted shadow measurement (COST-MODEL-001).** `edge-cost-shadow-report --hours N --top N` re-measures midpoint follow-through net of friction: half-spread, a documented conservative Kalshi fee assumption (`kalshi_fee_rate_assumption`, default 0.07 — the published taker-fee shape charged at BOTH measurement ends, no rebates), and executable touch prices (forecast above market: trigger ask → horizon bid; below: trigger bid → horizon ask; missing quotes counted as uncovered, never guessed). Cohorts (baseline, the pre-registered EDGE-SELECTION policies, market types, gap-vs-move, liquidity/spread/confidence buckets, series) get conservative labels — a cohort positive at frictionless midpoints but non-positive after friction is `cost_killed`; `promising_friction_adjusted_shadow` needs n≥75 + positive touch AND fee-adjusted closure + toward≥0.55 + concentration guards + an out-of-sample window for pre-registered policies. Measurement only — not EV, not PnL, never advice; changes no gate/forecast/promotion/flag/automation.
- **Candidate retirement (EDGE-RETIRE-001).** All six EDGE-SELECTION-001 candidates are **RETIRED** on out-of-sample evidence (`docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md`): every candidate failed its first substantial post-lock window — the primary inverted from discovery 0.539/+0.42 to validation 0.286/−1.22 — while the negative control outperformed them all, and the cost model had independently killed every cohort. `edge-selection-retirement-report` prints the frozen record plus the retired policies' current post-lock behavior (registry observation only). Standing rules: retired policies are ineligible for any live gate/paper/MVP discussion; resurrection requires a new prereg + new lock; successor hypotheses should be mechanism-first with cost-adjusted gates from day one. MVP-005B remains blocked.
- **Pre-registered selection validation (EDGE-SELECTION-001).** `edge-selection-validation-report --hours N [--since ISO] [--until ISO]` evaluates ONLY the policies frozen in `docs/EDGE_SELECTION_PREREG_2026_07_09.md` (6 candidates + baseline + `spread_only` negative control) against fixed pre-registered gates (n≥75, 60m toward≥0.55, positive closure, concentration guards, out-of-sample window), labelling every window discovery/validation/mixed — the window that selected a policy can never validate it, and the report says so. `validated_shadow` is a protocol status, never an authorization; the report always prints that MVP-005B remains blocked unless a human explicitly accepts it. Validation protocol only — changes no gate/forecast/promotion/flag/automation.
- **Trigger-timing shadow simulation (TRIGGER-TIMING-001).** `trigger-timing-shadow-report --hours N` replays alternate measurement times over ticks that already exist: for each historical watchlist row it asks what the gap and its follow-through would have looked like if edge-precheck had measured 2/5/10/15 minutes after the price_move_threshold trigger, or only once the midpoint went flat / the spread stabilized / the gap followed the recent move. The recorded forecast is held fixed; gaps that fell below the live min-abs-gap during the delay count as `gap_evaporated` (mean reversion beat the measurement — itself evidence); follow-through horizons are measured from the delayed time. Shadow only — no trigger, gate, forecast, promotion, flag, or automation changes; not PnL, never advice; labels motivate observation only.
- **Forecaster-anchoring diagnostic (FORECAST-ANCHOR-001).** `forecast-anchor-diagnostic-report --hours N` measures, per watchlist row, whether the forecast moved when the market moved (prior-forecast joins, adjustment ratio |Δforecast|/|Δmarket|, anchor buckets from `anchored_static` to `moved_with_market`), reports follow-through BY anchor bucket, and answers whether anchoring or trigger timing/selection dominates the negative follow-through. Measured values only — never advice; changes no forecast/gate/promotion/flag/automation; verdicts are evidence for future explicitly-accepted milestones, not instructions.
- **Shadow filter analysis (EDGE-FILTER-001).** `edge-filter-shadow-report --hours N` replays 18 candidate adverse-selection filters (gap-vs-recent-move, sharp pre-move, market-type/series exclusions, combined quality policies) over existing watchlist rows and reports each surviving population's follow-through, mixes, drift, examples, and a conservative label (`promising_shadow` needs n≥30 + rate/closure bar + no single-game concentration; young bar-clearing cohorts stay `too_thin`). Shadow only — nothing is filtered live; changes no gate/forecast/promotion/flag/automation; not PnL, never advice; MVP-005B stays blocked unless its own gate clears AND a human accepts.
- **Follow-through diagnostic (FOLLOWTHROUGH-001).** `edge-followthrough-diagnostic-report --hours N` explains *why* gap follow-through is negative: timing (signal/forecast/snapshot ages, pre-measurement market moves, whether the gap opposes the move the market just made), direction (continued-away vs reverted per horizon, spread/liquidity drift), deterministic per-cohort failure-mechanism verdicts (`adverse_selection_candidate` / `stale_or_chasing_move` / `measurement_artifact_possible` / `too_thin` / `promising_needs_more_sample` / `neutral`), and concrete failure examples. Measured market movement only — not PnL, never advice; changes no gate/forecast/promotion/automation; no EV/paper-trading/sizing/orders/wallets/signing/execution.
- **Operational observability (OPS-011).** `db-growth-report` and `prune-retention --dry-run` report storage growth, tick age/domain buckets, and per-table retention projections; DB-growth and signal-flood alerts are calibrated (configurable warning/critical tiers). Ops/observability only — no change to forecasting, edge logic, or trading behavior; no EV/paper-trading/sizing/orders/wallets/swaps/execution.
- **Tick aggregation (OPS-012).** `aggregate-market-ticks` rolls raw `market_price_ticks` — the dominant SQLite growth driver — into fixed-interval `market_price_tick_buckets` (OHLC midpoint, open/close bid/ask, spread/liquidity ranges, tick counts; migration 0023), idempotently and bounded, **never deleting raw ticks**; `tick-aggregation-report` shows coverage/compression and a **staged, not enacted** recommendation (a future milestone may reduce raw tick retention toward 24–48h only after coverage is proven healthy — raw retention is unchanged by OPS-012). Buckets are **telemetry summaries, never trading signals**, age out on their own 90-day window, and add no EV/paper-trading/recommendations/sizing/orders/wallets/keys/signing/execution.
- **Tick aggregation hardening (OPS-013).** Aggregation commits **per sub-window** (seconds of SQLite lock hold, not one long transaction), retries a locked DB as a bounded apply+commit unit (re-applying its upserts each attempt — a bare commit-retry after rollback would silently lose the window), records failed/oversized windows loudly on a new `tick_aggregation_runs` audit spine (migration 0024), and adds systemd timer artifacts that are **not auto-installed** with a `--scheduled` path that no-ops unless `ENABLE_TICK_AGGREGATION_TIMER=true` (default false). `tick-aggregation-report` gains a raw-retention-reduction **readiness** section (coverage/clean-cycles/error-free/fresh-feed gates) — evidence for a future, separately accepted milestone; **raw tick retention is unchanged and no raw tick is deleted**. Storage/durability only — no EV/paper-trading/recommendations/sizing/orders/wallets/keys/signing/execution.
- **Signal processing refreshes intelligence, nothing else.** OPS-004's workflow promotes selected signals and refreshes enrichment/assessment/research/forecast for their markets — the same read-only artifacts the pipeline already produces, just on demand. `paper_candidate_pending` is a human review label; **no paper trading, EV calculation, trade recommendation, or execution exists anywhere.**
- **LLM resolution judgment is OFF by default** (`ENABLE_LLM_RESOLUTION=false`). The deterministic rule-based judge needs no credentials or network beyond Kalshi; tests never call an LLM. When enabled, the LLM only *reads* rules text and returns a structured quality verdict — it has no tools and no trading capability.
- Public market data requires **no credentials**. The Kalshi API key is only needed for the optional WebSocket orderbook feed, and even then the client only sends channel subscriptions.
- Keep your Kalshi private key **outside the repo** (it is `.gitignore`d by extension, but store it elsewhere, e.g. `~/.kalshi/`). Never commit `.env`.
- The `resolution_clarity` ranking component is a **placeholder** (constant 0.5). Do not treat scores as trading advice; they measure market microstructure quality, not edge.
- Respect Kalshi's [API terms and rate limits](https://trading-api.readme.io/). The scanner caps fetches via `SCANNER_MAX_MARKETS` and results are cached in Redis for `CANDIDATES_CACHE_TTL_SECONDS`.
- **Targeted game-level scans (SCANNER-002/OPS-010)** supplement the generic scan with per-series fetches (`TARGETED_MARKET_SERIES`, e.g. `KXWCGAME`, `KXMLBTOTAL`) so measurable game-level markets aren't crowded out of the first page by props. This is scanner **coverage** only: it calculates no EV, recommends no trades, does no paper trading, sizes no positions, places no orders, and touches no wallets/keys/swaps/execution.

## Architecture

```
app/
  main.py                 FastAPI app, lifespan (migrations + optional WS service)
  cli.py                  python -m app.cli scan --limit N
  config.py               pydantic-settings; WS enabled only if credentials present
  db.py                   SQLAlchemy engine/session, programmatic Alembic runner
  models.py               markets, market_snapshots, orderbook_snapshots, scanner_runs
  schemas.py              Pydantic contracts (MarketData, RankedMarket, API responses)
  adapters/kalshi.py      REST adapter: fetch + parse active markets (cursor paging,
                          legacy int-cent and current *_dollars/*_fp payload shapes)
  services/eligibility.py Deterministic candidate hygiene gate (thresholds below)
  services/enrichment.py  Market detail enrichment (detail/event/series metadata)
  services/resolution.py  Resolution-criteria judges (rule-based / mock / optional LLM)
  services/research.py    Research packet collectors (template / mock / optional LLM+web)
  services/forecasting.py Forecast engine (template baseline / mock / optional LLM)
  services/outcomes.py    Outcome sync (read-only settlement state per market)
  services/calibration.py Forecast scoring (Brier / log loss) + cohort summaries
  services/pipeline.py    Baseline runner: 8-stage audited loop + overlap lock
  services/watcher.py     Real-time watcher: price ticks + informational signals
  services/signal_workflow.py Signal promotion + signal-triggered intelligence refresh
  services/ranking.py     Weighted scoring: spread, liquidity, volume, expiration, clarity
  services/scanner.py     fetch -> assess eligibility -> rank eligible -> persist
  services/ws_snapshots.py Optional WS orderbook snapshot service (credential-gated)
  services/cache.py       Best-effort Redis cache (degrades gracefully)
  routers/markets.py      GET /markets/candidates
alembic/                  Migrations (0001 initial schema, 0002 audit + raw_payload)
tests/                    Adapter, ranking, persistence, migrations, CLI, cache tests
```

## Quick start (Docker Compose)

```bash
cp .env.example .env          # defaults work out of the box
docker compose up --build
```

The api container runs Alembic migrations automatically on startup (databases created by pre-Alembic MVP-001 are detected and stamped at revision `0001` before upgrading). Then:

- `GET http://localhost:8000/health` — liveness + whether WS is enabled
- `GET http://localhost:8000/markets/candidates?limit=25` — top **eligible** candidates (triggers a scan, cached ~30s)
- `GET http://localhost:8000/markets/candidates?include_rejected=true` — also returns gated-out markets with their `rejection_reasons` (debugging)
- `GET http://localhost:8000/markets/candidates?include_resolution=true` — attaches each candidate's latest persisted resolution assessment (cheap DB lookup; never triggers new assessments)
- `POST http://localhost:8000/markets/{ticker}/enrich-details` — fetch and persist detail/event/series metadata for one known market (response excludes the raw payloads; those stay DB-only)
- `POST http://localhost:8000/markets/{ticker}/resolution-assessment` — assess one known market ad hoc and persist the result
- `POST http://localhost:8000/markets/{ticker}/research-packet` — build and persist a research packet (uses latest enrichment + resolution; `avoid` markets are forced to `research_risk=high`)
- `GET http://localhost:8000/markets/{ticker}/research-packets?limit=10` — recent packets for a ticker, newest first (raw collector output stays DB-only)
- `POST http://localhost:8000/markets/{ticker}/forecast` — build and persist a forecast from the latest research packet (409 if none exists; pass `?prepare=true` to create one first)
- `GET http://localhost:8000/markets/{ticker}/forecasts?limit=10` — recent forecasts, newest first; `GET /markets/candidates?include_forecast=true` attaches each candidate's latest forecast. **GETs never create forecasts or call models.**
- `POST http://localhost:8000/markets/{ticker}/sync-outcome` — sync a market's settlement state (read-only detail GET); `GET /markets/{ticker}/outcome` returns the latest persisted outcome (404 if never synced)
- `GET http://localhost:8000/forecasts/scores` — recent forecast scores, filterable by `score_status`, `market_ticker`, `forecaster_name`, `evidence_depth`
- `GET http://localhost:8000/calibration/summary` — aggregate Brier / log-loss / absolute-error by evidence depth, risk, forecaster, domain, and tag
- `GET http://localhost:8000/pipeline/runs` and `GET /pipeline/runs/{id}` — pipeline audit records (runs and per-stage details)
- `GET http://localhost:8000/signals` (filters: `signal_status`, `signal_type`, `market_ticker`), `GET /signals/{id}`, `PATCH /signals/{id}/status` — opportunity signal review workflow (`new` → `reviewed` / `dismissed` / `promoted_to_research`)
- `http://localhost:8000/docs` — OpenAPI UI

### Live Kalshi smoke test

With the stack up, one round-trip against the real (public, credential-free) Kalshi API:

```bash
curl -s http://localhost:8000/health
curl -s "http://localhost:8000/markets/candidates?limit=5" | python3 -m json.tool
```

Or without the API server, via the CLI:

```bash
docker compose run --rm api python -m app.cli scan --limit 100
```

## CLI

```bash
python -m app.cli scan --limit 100
```

Runs migrations, fetches up to `--limit` open markets plus the configured targeted series (SCANNER-002; deduped by ticker, per-series failures reported but never fatal), assesses eligibility, ranks the eligible ones, persists a `scanner_runs` audit row (with `source=cli`, `duration_ms`, and error details on failure) plus per-market snapshots and eligibility assessments, then prints the top 20, targeted-scan counts, and a rejection-reason summary.

```bash
python -m app.cli enrich-details --limit 20
```

Fetches detail/event/series metadata for the top `--limit` eligible candidates of the most recent successful scan and persists one `market_detail_enrichments` row per market (raw payloads included for audit). Individual fetch failures are skipped, never fatal.

```bash
python -m app.cli assess-resolution --limit 20
```

Takes the top `--limit` eligible candidates from the most recent successful scan (running a fresh scan if none exists), scores each market's resolution criteria with the configured judge, and persists one `market_resolution_assessments` row per market linked to that scan.

```bash
python -m app.cli collect-research --limit 10
```

Builds research packets for the top `--limit` eligible candidates of the most recent successful scan, preferring markets that already have an enrichment and a researchable resolution, and prints per-market lines plus domain/risk summaries. It deliberately does **not** trigger enrichment or assessment on its own — pass `--prepare` to create missing upstream rows first.

```bash
python -m app.cli forecast --limit 10
```

Creates forecasts for the top eligible candidates that already have research packets (markets without packets are skipped unless `--prepare` is passed), preferring enriched + researchable markets, and prints per-market lines plus domain/evidence-depth/risk summaries.

```bash
python -m app.cli sync-outcomes --limit 100      # settlement state for known markets (forecasted first)
python -m app.cli score-forecasts --limit 500    # Brier/log-loss/abs-error where outcomes are settled
python -m app.cli calibration-report             # aggregate summary by cohort
```

**Recommended sequence:** `scan` → `enrich-details` → `assess-resolution` → `collect-research` → `forecast` → `sync-outcomes` → `score-forecasts` → `calibration-report`. Each stage automatically prefers the previous stage's output when it exists.

## Baseline runner (scheduled operation)

```bash
python -m app.cli run-baseline        # the whole sequence above as ONE audited pipeline run
python -m app.cli pipeline-status     # recent runs + latest stage table
```

`run-baseline` executes all eight stages in order, recording a `pipeline_runs` row plus one `pipeline_stage_runs` row per stage (timing, item counts, error type/message). Options: `--scan-limit`, `--candidate-limit`, `--sync-outcome-limit`, `--score-limit` (defaults from `BASELINE_*` env vars), `--fail-fast` (default off — a failed stage is recorded and later stages still run where safe, e.g. off the previous successful scan), and `--dry-run` (records the audit row only; executes nothing).

**Overlap lock:** a `running` pipeline row acts as the lock — a second invocation exits gracefully as `skipped` with a pointer to the active run. Crashed leftovers older than 6 hours are treated as stale and ignored.

**Scheduled operation (systemd):** timer artifacts live in `infra/systemd/` and are deliberately **not** auto-installed. Recommended cadence is every 4 hours for data accumulation (edit `OnCalendar` to `daily` for a lighter footprint):

```bash
sudo cp infra/systemd/probability-arena-baseline.{service,timer} /etc/systemd/system/
# edit paths/user in the .service file for your deployment first
sudo systemctl daemon-reload
sudo systemctl enable --now probability-arena-baseline.timer
systemctl list-timers probability-arena-baseline.timer
journalctl -u probability-arena-baseline.service -f
```

**Why this exists:** the template baseline forecaster is anchored to the market midpoint, so its accumulated Brier/log-loss over many settlements approximates the market's own calibration. That dataset is the bar any future forecaster (`ENABLE_LLM_FORECASTING`, `ENABLE_EXTERNAL_RESEARCH`) must beat — and it must exist **before** EV or paper trading is worth discussing. Let the timer run for a few weeks, then compare cohorts in `calibration-report`.

## Real-time opportunity watcher

The 4-hour baseline runner accumulates calibration data; the **watcher** is a separate, faster loop (default every 60 s) that polls fresh quotes for the latest scan's eligible candidates, records `market_price_ticks`, and emits deterministic, **informational-only** `opportunity_signals`:

| Signal | Fires when |
|---|---|
| `price_move_threshold` | midpoint moved ≥ `WATCHER_PRICE_MOVE_THRESHOLD` (default $0.07) since the last tick |
| `spread_tightened` | spread crossed into the ≤ `WATCHER_MAX_SPREAD` band |
| `newly_two_sided` | market gained a two-sided quote |
| `liquidity_appeared` | liquidity proxy crossed ≥ `WATCHER_MIN_LIQUIDITY_PROXY` |
| `price_crossed_latest_forecast` | midpoint crossed the latest persisted forecast probability |

All detectors compare the previous tick to the new one (a first observation never fires), and repeated `(ticker, signal_type)` alerts are deduped within `WATCHER_SIGNAL_COOLDOWN_SECONDS` (default 900). Every signal stores its reason, evidence, and raw payload for audit.

```bash
python -m app.cli watch-once --limit 100    # one manual pass (always available)
python -m app.cli watch-loop --interval 60  # continuous; requires ENABLE_REALTIME_WATCHER=true
```

`watch-loop` exits cleanly on SIGINT/SIGTERM and survives per-pass errors. An optional systemd user unit exists at `infra/systemd/user/probability-arena-watcher.service` — **separate from the 4-hour baseline timer**, not auto-installed, and inert unless `ENABLE_REALTIME_WATCHER=true`.

Review signals via the API (`GET /signals`, `PATCH /signals/{id}/status`).

## Signal workflow (promotion → intelligence refresh)

The recommended live sequence once a signal lands:

```
watcher catches signal (new)
  → promote signal            (promoted_to_research)
  → process promoted signal   (research_refreshed → forecast_refreshed)
  → review the refreshed forecast (optionally label paper_candidate_pending)
```

```bash
python -m app.cli signals-recent --limit 20            # what did the watcher catch?
python -m app.cli promote-signals --limit 5            # promote top-N new signals
python -m app.cli process-promoted-signals --limit 5   # refresh enrichment/assessment/packet/forecast
python -m app.cli signal-report                        # workflow overview
```

**Promotion** (`promote-signals`, `POST /signals/{id}/promote`) only accepts `new` signals (promoting an already-promoted signal is a no-op; dismissed/reviewed/processed → 409). Batch promotion is deterministic: priority order `price_move_threshold` → `price_crossed_latest_forecast` → `spread_tightened` → `liquidity_appeared` → `newly_two_sided`, newest first within a type, at most one signal per market per batch.

**Processing** (`process-promoted-signals`, `POST /signals/process-promoted`) handles promoted signals oldest-first: fresh detail enrichment → fresh resolution assessment → fresh research packet → fresh forecast, then links `refreshed_research_packet_id`/`refreshed_forecast_id` onto the signal and marks it `forecast_refreshed`. Failures are captured on the signal (`processing_error_type`/`message`) and leave it at the last completed stage; errored signals are skipped on later runs until the error is cleared. **Conservative by design:** whichever services the existing env flags select are used — template research and template baseline forecasts unless `ENABLE_EXTERNAL_RESEARCH` / `ENABLE_LLM_FORECASTING` are true. This is workflow plumbing, not alpha: a refreshed template forecast still carries no independent edge.

`GET /signals/recent`, `GET /signals/report`, and the new statuses (`research_refreshed`, `forecast_refreshed`, `paper_candidate_pending`) complete the review loop. Nothing beyond `paper_candidate_pending` exists — no paper trading, no EV, no positions, no orders.

## Baseball external research canary

**This is not global external research.** `ENABLE_EXTERNAL_RESEARCH` stays `false`; the canary is a separate, narrow flag:

- `ENABLE_BASEBALL_EXTERNAL_RESEARCH=false` (default) — plus `BASEBALL_RESEARCH_TIMEOUT_SECONDS=15`, `BASEBALL_RESEARCH_MAX_SOURCES=8`, `BASEBALL_RESEARCH_COLLECTOR_VERSION=v1`.

When the flag is on, signal processing uses `BaseballExternalResearchCollector` **only** when all four conditions hold: the signal is promoted, its domain is `sports_baseball`, its fresh resolution assessment is `researchable`, and the flag is true. Everything else (other domains, flag off, non-researchable markets, explicitly injected collectors) uses the template collector.

Evidence comes from the **public MLB Stats API** (`statsapi.mlb.com` — official league data, read-only GETs, no credentials): live game state (score/inning/outs/bases), probable pitchers, confirmed lineups, weather/venue. The Kalshi ticker is parsed for game date + matchup and matched against the MLB schedule. Every fact carries a source reference; every source persists url/title/source_type/credibility/freshness. Facts fill template gaps (lineups/pitchers/weather drop out of `missing_info`) and boost `research_completeness_score` above the 0.65 template ceiling — making the packet `source_backed` downstream, which raises the forecast confidence cap from 0.55 to 0.75. If the game can't be identified or fetched, the collector **falls back to template content honestly**: evidence depth stays `template_only`, the reason lands in `missing_info` and `raw_response`, and the canary report counts it as a fallback.

```bash
python -m app.cli process-promoted-signals --limit 5   # per-signal line shows research=<collector>/<depth>
python -m app.cli research-canary-report               # packets by collector/domain/depth + fallbacks
```

`GET /signals/report` includes the same canary metrics (`research_canary`), and processed signals carry a `refreshed_packet` summary (collector, evidence depth, completeness).

**Safe rollout on EVO-X2:**

```bash
cd ~/projects/probability-arena && git pull --ff-only
.venv/bin/python -m app.cli run-baseline --dry-run           # migrations (none new) + sanity
# 1. keep the flag false; verify template mode still works:
.venv/bin/python -m app.cli promote-signals --limit 3
.venv/bin/python -m app.cli process-promoted-signals --limit 3
# 2. flip the canary on: set ENABLE_BASEBALL_EXTERNAL_RESEARCH=true in .env
# 3. process 1-3 promoted baseball signals and inspect:
.venv/bin/python -m app.cli process-promoted-signals --limit 3
.venv/bin/python -m app.cli research-canary-report
.venv/bin/python -m app.cli signal-report                    # review the refreshed forecasts
```

Still read-only end to end: no EV calculation, no trade recommendations, no paper trading, no sizing, no orders, no wallets, no execution.

## Soccer / World Cup external research canary

The second external-research canary (SOCCER-001), mirroring the baseball pattern for `sports_soccer` markets (World Cup, Champions League, EPL, MLS). `ENABLE_EXTERNAL_RESEARCH` stays `false`; the canary has its own flags:

- `ENABLE_SOCCER_EXTERNAL_RESEARCH=false` (default) — plus `SOCCER_RESEARCH_PROVIDER=template`, `SOCCER_RESEARCH_TIMEOUT_SECONDS=15`, `SOCCER_RESEARCH_MAX_SOURCES=8`, `SOCCER_RESEARCH_COLLECTOR_VERSION=v1`.

Signal processing uses `SoccerExternalResearchCollector` **only** when all four conditions hold: the signal is promoted, its domain is `sports_soccer`, its fresh resolution assessment is `researchable`, and the flag is true. Everything else (other domains, flag off, non-researchable markets, explicitly injected collectors) uses the template collector; the baseball canary keeps its own independent gate.

The live data source is selected by `SOCCER_RESEARCH_PROVIDER`. `template` (the default) configures no fetcher, so even with the flag on the collector produces honest template-depth packets with the reason recorded — a deliberate dark-launch mode that makes the collector visible in reports before any external call happens. `espn` enables the **public ESPN soccer API** (`site.api.espn.com` — read-only GETs, no credentials), with the league slug (`fifa.world`, `uefa.champions`, `eng.1`, `usa.1`) mapped from the Kalshi ticker prefix (`KXWC`, `KXUCL`, `KXEPL`, `KXMLS`).

The ticker is parsed for event date, teams, market type (winner/total/spread where detectable), and line/threshold when present — and **degrades honestly** (template fallback with reason) on unknown shapes. Evidence gathered when available: live score, match clock/period, red cards, penalty-shootout state, confirmed lineups, and basic match stats (possession/shots). Every fact carries a source reference; every source persists url/title/source_type/credibility/freshness. Confirmed lineups close that template gap; unfetched facts (pre-match team news, recent form) stay listed in `missing_info`. Evidence boosts `research_completeness_score` above the 0.65 template ceiling, making the packet `source_backed` downstream. Fallbacks (no fetcher, unparseable ticker, no scoreboard match, fetch failure) keep `template_only` depth and are counted by `research-canary-report`.

```bash
python -m app.cli process-promoted-signals --limit 5   # per-signal line shows research=<collector>/<depth>
python -m app.cli research-canary-report               # soccer-external rows alongside baseball-external
```

**Safe rollout on EVO-X2:**

```bash
cd ~/projects/probability-arena && git pull --ff-only
.venv/bin/python -m app.cli run-baseline --dry-run           # migrations (none new) + sanity
# 1. deploy dark (both knobs at defaults); verify template mode still works:
.venv/bin/python -m app.cli process-promoted-signals --limit 3
# 2. set ENABLE_SOCCER_EXTERNAL_RESEARCH=true, keep SOCCER_RESEARCH_PROVIDER=template:
#    promoted soccer signals now use soccer-external but fall back honestly (observable, no external calls)
# 3. set SOCCER_RESEARCH_PROVIDER=espn; process 1-3 promoted soccer signals and inspect:
.venv/bin/python -m app.cli process-promoted-signals --limit 3
.venv/bin/python -m app.cli research-canary-report
.venv/bin/python -m app.cli signal-report
```

This canary **does not trade, paper trade, calculate EV, or recommend positions** — it only upgrades research packets from template to source-backed evidence. No sizing, no orders, no wallets, no execution.

## Baseball evidence-aware forecaster

The first non-midpoint forecaster — behind its own flag, consuming only persisted packets:

- `ENABLE_BASEBALL_EVIDENCE_FORECASTING=false` (default) — plus `BASEBALL_FORECASTER_VERSION=v1`, `BASEBALL_FORECAST_MAX_CONFIDENCE=0.70`, `BASEBALL_FORECAST_MIN_COMPLETENESS=0.75`.

`ForecastingService` selects `BaseballEvidenceAwareForecaster` only when **all** conditions pass: flag on, domain `sports_baseball`, packet `source_backed`, completeness ≥ 0.75, resolution `researchable`. Everything else — and any explicitly injected forecaster — keeps the template baseline. **It makes no external calls itself**: evidence (score, inning/half/outs, base runners, probable pitchers, lineups, weather) is parsed from the persisted packet's facts and raw payload.

**Model (deterministic v1, fully stated in each forecast's output):** the market midpoint is the prior; recognized market types get an evidence estimate — pace-projected totals (`KXMLBTOTAL`, line parsed from the ticker), current margin vs required margin for spreads (`KXMLBSPREAD`), current margin for game winners (`KXMLBGAME`). The blend weight and slope grow with game progress (late-game evidence moves the needle more than early-game), and the total shift away from the prior is hard-capped at ±0.25. Player props, first-5-innings markets, and anything unrecognized fall back to the template baseline with a `market_type_unknown` tag and a skeptic note. Missing critical facts cap confidence at 0.50 and force high risk.

Every forecast populates bull/bear cases, skeptic notes (including the assumed-line caveat), key assumptions, change triggers, and calibration tags — `baseball_evidence_v1`, `market_type_total|spread|winner|unknown`, `late_game`/`early_game`, `live_game_state`, `evidence_adjusted`/`anchored_to_market_mid` — so `calibration-report` and `research-canary-report` (now with a `forecasts by forecaster` breakdown) can compare `template_baseline` vs `baseball_evidence` cohorts as outcomes settle.

**Safe rollout on EVO-X2:**

```bash
cd ~/projects/probability-arena && git pull --ff-only
.venv/bin/python -m app.cli run-baseline --dry-run          # sanity (no new migrations)
# 1. flags false: process a promoted signal in template mode, verify unchanged
# 2. set ENABLE_BASEBALL_EXTERNAL_RESEARCH=true in .env
# 3. set ENABLE_BASEBALL_EVIDENCE_FORECASTING=true in .env
# 4. process 1-3 promoted baseball signals and inspect:
.venv/bin/python -m app.cli process-promoted-signals --limit 3
.venv/bin/python -m app.cli research-canary-report          # forecaster breakdown
.venv/bin/python -m app.cli signal-report                   # refreshed forecasts + tags
```

No EV calculation, no trade recommendations, no paper trading, no sizing, no orders, no wallets, no execution — forecasts remain auditable reasoning artifacts that calibration will judge.

## Crypto Arena — read-only Solana memecoin surveillance (CRYPTO-001)

A parallel, isolated crypto lane that observes Solana token/pair activity and persists auditable surveillance data. **Read-only in CRYPTO-001, full stop:** it does not trade, paper trade, calculate EV, size positions, or recommend positions. Wallet/private-key handling is forbidden (ADR-002); Jupiter/swap/transaction construction and signing are out of scope and gated far behind future milestones (`docs/SAFETY_BOUNDARIES.md`).

**Discovery** (`DexScreenerAdapter`, public `api.dexscreener.com`, no credentials): latest token profiles + boosted tokens (Solana-filtered), pairs per token with price/liquidity/volume/age/boost metadata. Rate limits, HTTP errors, and schema drift all degrade to empty results, recorded on the scan's audit row. `CryptoDiscoveryService` upserts `crypto_tokens`/`crypto_pairs`, records discovery events (`profile`/`boost`/`pair_seen`), price ticks, and — when `ENABLE_CRYPTO_RISK_PROVIDER=true` — provider risk assessments (`CRYPTO_RISK_PROVIDER=mock` is the only CRYPTO-001 implementation; real providers are CRYPTO-002).

**Signals** (`CryptoSignalService`, deterministic, latest tick vs previous, deduped per token+type within `CRYPTO_SIGNAL_COOLDOWN_SECONDS`): `new_pair`, `liquidity_appeared`, `volume_spike`, `price_momentum`, `boost_detected`, `liquidity_removed`, plus provider-dependent `holder_risk`, `rug_risk`, `suspicious_supply_control` (inactive without a risk provider). Signals are informational telemetry with reason + evidence for later human review and — after gated milestones — paper simulation.

```bash
python -m app.cli crypto-scan-once --limit 100   # one read-only discovery pass (always allowed manually)
python -m app.cli crypto-signals-recent --limit 20
python -m app.cli crypto-report                  # totals, signals by type/status, risk levels, provider errors
```

`GET /crypto/signals` · `GET /crypto/tokens` · `GET /crypto/pairs` · `GET /crypto/report` serve the same data (raw provider payloads stay DB-only). `ENABLE_CRYPTO_SCOUT=false` reserves future loop/timer use — no crypto watch-loop exists in CRYPTO-001. Retention prunes only `crypto_price_ticks`/`crypto_watcher_runs` (`CRYPTO_RETENTION_DAYS=7`); tokens, pairs, events, risk assessments, and signals are kept.

**Future (each explicitly gated):** CRYPTO-003 paper simulator (gated like MVP-005B) → WALLET-001 policy-controlled transaction *proposal* gateway only (no signing, no keys), much later.

## Crypto risk engine (CRYPTO-002)

**Read-only risk intelligence.** The engine upgrades Crypto Arena from discovery into harsh risk scoring — and the output is explicitly *not* trade advice: a composite risk score/level is an **avoid/flag verdict for human review; "severe" means avoid/flag, never short/sell**. No wallet, private-key, swap, or transaction-construction code exists anywhere in this project.

Two layers, combined per token:

- **`HeuristicRiskEngine` (always available, no credentials, no network):** deterministic categories from data CRYPTO-001 already persists — `low_liquidity`, `liquidity_removed`, `new_pair_too_young`, `extreme_price_movement`, `suspicious_volume_spike`, `fake_volume_suspected`, `boosted_token` (context weight, not automatic severity), `missing_metadata`, plus `provider_unknown` honestly recorded when no provider corroboration exists.
- **Optional providers** (`ENABLE_GOPLUS_RISK`, `ENABLE_SOLANA_TRACKER_RISK`, both default false; API keys optional, header-only, never printed): holder/sniper/insider/bundler concentration vs configured thresholds, mint/freeze authority, rug/honeypot verdicts — categories like `high_holder_concentration`, `mint_authority_enabled`, `provider_rug_flag`. A failing provider is isolated (recorded in `provider_errors`), never fatal: the engine falls back to heuristics. `ENABLE_RUGCHECK_RISK` is reserved (no adapter yet).

Six normalized sub-scores (liquidity, holder, authority, market structure, manipulation, provider) roll up into a weighted `composite_risk_score` and level (`low|medium|high|severe|unknown`); rug/honeypot/liquidity-removed evidence floors the composite at severe. Everything persists on `crypto_token_risk_assessments` with `risk_reasons` (ordered category codes) and `provider_names` — fully auditable.

**Risk signals activate only on evidence:** `rug_risk`, `holder_risk`, and `suspicious_supply_control` fire when an assessment carries matching severe/high flags or categories, and stay inactive when no risk data exists (CRYPTO-001 behavior is preserved when `ENABLE_CRYPTO_RISK_ENGINE=false`).

```bash
python -m app.cli crypto-risk-assess --limit 50   # assess recent tokens from persisted data (always allowed)
python -m app.cli crypto-risk-report              # engine mode, level breakdown, worst tokens, reasons, provider health + holder-coverage overlay
python -m app.cli crypto-report                   # now shows the engine mode alongside surveillance totals
```

### Holder/sniper/insider/bundler/creator coverage (MEME-RISK-003)

**Read-only risk intelligence — it does not trade, paper trade, compute EV, recommend, size, place orders, or use wallets/keys/swaps/signing/execution.** GoPlus alone leaves the sniper/bundler/creator dimensions uncovered; MEME-RISK-003 closes that and makes the gap *explicit*:

- **New Birdeye provider** (`ENABLE_BIRDEYE_RISK`, key optional/header-only): top-holder + **creator/deployer concentration** coverage. Its live payload mapping is **pending validation** — it degrades to honest absence (no fabricated data) if the shape differs, exactly like the TENNIS ESPN provider.
- **New `creator_concentration` heuristic category** (`CRYPTO_RISK_MAX_CREATOR_PCT=15`) — fires only when a provider actually supplies `creator_pct`, so GoPlus-only assessments are unchanged.
- **Explicit coverage reporting** — provider absence is stated, never silent:

```bash
python -m app.cli crypto-provider-health-report   # per-provider status/key-present/dimensions + EXPLICIT coverage gaps + observed coverage
python -m app.cli meme-risk-coverage-report        # holder-risk coverage for the meme-news lane (which tokens have provider data)
```

The SolanaTracker adapter supplies the full **sniper/insider/bundler** set from the same `/tokens/{address}` risk object (SOLANA-TRACKER-002: the fields arrive as `totalPercentage`, parsed directly — no extra request, no budget impact; live coverage sniper/insider ≈ 100%, bundler ≈ 83%). Keys are reported present/absent only — never their values. Existing GoPlus/MarketOps/EDGE-AUTO/MEME-NEWS behavior is unchanged; flags default off.

`GET /crypto/risk-assessments` · `GET /crypto/tokens/{token_address}/risk` · `GET /crypto/risk-report` serve the same data (raw payloads stay DB-only).

### SolanaTracker request budget (PROVIDER-BUDGET-001)

**Provider cost/usage observability — not trading.** The SolanaTracker Advanced plan (**≈ $58–59/month USD** recurring data-provider OpEx, official ceiling **200,000 requests/month**) gets request accounting + budget guardrails. Usage is derived read-only from the existing `crypto_token_risk_assessments` rows (no new table).

```bash
python -m app.cli crypto-provider-budget-report   # plan/limit, requests today/hour/month, run-rate, remaining budget, success/error rate, coverage-per-request, keep/tune rec
```

Operational targets: ≤150k/month, ≤5k/day, ≤200/hour, ≤20–30 lookups per 10-minute window. The guardrail can only **skip** optional SolanaTracker lookups — when a scan hits `SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT` (25) or the day reaches `SOLANA_TRACKER_STOP_DAILY_REQUESTS` (6000), further SolanaTracker calls are skipped and those tokens fall back to **GoPlus + heuristics** (a fully supported mode). It never adds calls, never changes **GoPlus/Birdeye** behavior, and defaults sit far above current usage so nothing is skipped under normal load — the STOP is a cost circuit breaker. Skips are logged and the report shows the WARN/STOP state, so the budget is never silently exceeded. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, or execution.

## Crypto lifecycle tape (CRYPTO-TAPE-001)

**Read-only replayable token lifecycle recording — research infrastructure, never advice.** The tape moves the crypto lane from point-in-time scoring to lifecycle intelligence: token birth, early holder/actor structure, risk-provider enrichment, liquidity path, social metadata, and deterministic survival outcomes over 15m/1h/6h/24h horizons.

The tape is **derived**: one assembly pass consolidates rows the existing lanes already persist (`crypto_tokens`/`crypto_pairs`/`crypto_price_ticks`/discovery events/risk assessments + meme attention snapshots/catalyst events) into five tape tables — `crypto_token_lifecycle_runs` (audit spine), `crypto_token_birth_events` (one per token: first evidence, launch source, first pair/dex, mint/freeze authority, metadata/social links, initial market state, bonding-curve state, raw provenance), `crypto_token_lifecycle_snapshots` (consolidated market + holder-concentration + risk + social/catalyst + quote-quality state per run, with per-source provenance ids), `crypto_token_actor_observations` (creator address/holding if a provider exposed one, sniper/insider/bundler counts, holder distribution; **public-chain addresses only**, with honest placeholders for first buyers and cohort/cluster refs), and `crypto_token_survival_outcomes` (per birth event, recomputed each run until the 24h window closes: `survived_15m/1h/6h/24h`, `liquidity_removed`, `dead_volume`, `severe_risk`, `graduated_or_migrated`, `provider_gap` — NULL means "not yet measurable or source gap", never a guess).

It makes **zero external calls and has zero provider-budget impact**; fields no source ever provided stay NULL and are named in `missing_info`. Tape tables are not retention-pruned (they are the durable research record; only the raw ticks they were derived from are).

```bash
python -m app.cli crypto-tape-run-once --limit 25 --hours 48 --dry-run  # compute + report, persist nothing
python -m app.cli crypto-tape-run-once --limit 25 --hours 48            # persist ONLY lifecycle tape rows
python -m app.cli crypto-tape-report --hours 24 --top 5                 # coverage, survival labels, actor patterns
python -m app.cli crypto-tape-session --duration-hours 6 --interval-min 30 --limit 25 [--dry-run]  # bounded repeated passes (CRYPTO-TAPE-CADENCE-001)
```

**Session helper (CRYPTO-TAPE-CADENCE-001):** `crypto-tape-session` runs a fixed, hard-capped number of `run_once` passes in ONE invocation with a sleep between, then exits — not a timer, not a daemon, never autonomous. It exists because survival horizons only mature when the tape observes tokens repeatedly (CRYPTO-RETROSPECT-001 found provider gaps dominating for exactly this reason). Hard caps: duration ≤36h, interval clamped 15–120 min, ≤144 captures/session; aborts on abnormal pass status or a detectable MarketOps error. Each pass is the same derived, zero-external-call assembly. `--dry-run` prints the planned schedule and runs exactly one dry probe — nothing persisted, no sleeping. The session summary reports captures, rows written, horizon maturity (known/unknown per horizon), and the provider-gap share trend across captures.

No new flag, no timer, no scheduled path (a later milestone would gate that); MarketOps is unchanged. A survival label is measured token behavior — no EV, no recommendation, no sizing, no paper trading, no orders, no wallets/keys/swaps/signing/execution.

## Crypto retrospective analysis (CRYPTO-RETROSPECT-001)

**Which persisted features actually separate the lifecycle outcomes?** A compute-on-demand measurement layer (like MEME-SHADOW: no table, no migration, nothing persisted, zero external calls, zero provider-budget impact) that joins observable features to the CRYPTO-TAPE-001 survival outcomes over the recent token universe. Persisted tape birth events are preferred as anchors; other tokens get an on-the-fly derivation from the same already-persisted rows (never written back).

```bash
python -m app.cli crypto-retrospect-report --hours 48 --top 5
```

Feature dimensions: top10/sniper/insider/bundler/creator concentration buckets (thresholds anchored to the risk engine's), risk level/score, per-risk-reason cohorts, liquidity depth, volume-to-liquidity shape, boost/attention, social metadata presence, launch venue (launchpad vs AMM), graduation, provider coverage, and per-missing-info cohorts. Outcomes: `survived_15m/1h/6h/24h`, `liquidity_removed`, `dead_volume`, `severe_risk`, `graduated_or_migrated`, `provider_gap` — immature/unmeasurable outcomes stay unknown and never enter a rate.

Interpretation is deliberately conservative: cohorts under 12 samples are `too_thin`; a dimension whose primary outcome is mostly unmeasurable is `provider_gap_dominates`; otherwise the best-vs-worst measured-cohort rate delta yields `no_separation` (<0.10), `weak_separator` (≥0.10), or `strong_risk_separator`/`strong_survival_separator` (≥0.25). A separation label is evidence about feature/label quality for review triage and future milestone design — never advice, never EV, never a trade direction.

**Tape-backed cohort stratification (CRYPTO-RETROSPECT-002):** fresh derived-only tokens (just discovered, horizons still immature) dilute the window and can manufacture apparent patterns, so the report separates mature **tape-backed** evidence (a persisted birth event with repeated re-observations) from **derived-only** noise.

```bash
python -m app.cli crypto-retrospect-report --hours 72 --cohort tape-backed   # or all (default) / derived-only
```

`--cohort` re-lenses the headline. Two sections are **always** shown over the full window: a `data_source_mix` (tape-backed / derived-only / immature counts, horizon maturity known/unknown per source, and provider-gap rate per source), and a per-dimension `source_stratification` that computes the interpretation three ways (all vs tape-backed vs derived-only), flags **dilution** when the all-window view hides a tape-backed signal, and assigns a source label: `tape_too_thin`, `tape_readable`, `all_window_diluted`, `derived_only_dominates`, `consistent_across_sources`, or `tape_only_hint`. This is what tells you whether an apparent pattern (like the current top10-concentration spread) actually lives in matured tape or is just fresh-token noise.

## Crypto tape coverage forensics (CRYPTO-COVERAGE-001)

**Why do survival horizons stay unmeasurable even after repeated cadence sessions?** A compute-on-demand diagnostic (no table, no migration, nothing persisted, zero external calls) that decomposes every unknown survival outcome into an explicit, actionable cause and asks whether the recorder's token-selection and revisit policy can ever mature 6h/24h outcomes efficiently.

```bash
python -m app.cli crypto-tape-coverage-report --hours 168 --top 5 --limit 25
```

It separates the two failure modes that need opposite fixes:

- **Upstream tick coverage** — a survival horizon only matures from `crypto_price_ticks`, which the *background crypto scout* collects, not the tape. If the scout stopped ticking a token near its 6h/24h mark, the horizon is unmeasurable no matter how many tape sessions run (`token_inactive_or_disappeared`, `no_price_tick_near_horizon`, `no_pair_or_liquidity_state_near_horizon`, `outside_tolerance_only`).
- **Revisit / selection** — the recorder picks tokens recent-first, so an *old* token whose 6h/24h is due ranks below the per-run limit and is never recomputed even when the ticks it needs already exist (`token_not_revisited_after_due`); a genuine recompute bug is `source_rows_exist_but_join_failed`.

Report sections: a per-horizon **coverage funnel** (born → due → revisited → raw data → within tolerance → measurable → provider_gap, with rates), a **gap-cause histogram**, a **bottleneck verdict** (is 6h/24h limited by upstream coverage or revisit policy?), a **selection analysis** (appearances per token, how often due old cohorts rank below the limit, whether recent-first starves them), a **shadow-only selection comparison** estimating how many currently-maturable 6h/24h outcomes each policy (`current_recent_selection` / `due_horizon_first` / `fixed_cohort_revisit` / `mixed_new_and_due`) would pick up on the next run, and concrete examples per cause. It changes **no stored outcome label and no live recorder selection** — it is pure measurement to inform a future, separately-accepted selection milestone.

## Crypto horizon observation (CRYPTO-HORIZON-OBS-001)

**Fixing the upstream coverage gap CRYPTO-COVERAGE-001 found.** The forensics proved the 6h/24h maturation ceiling is upstream tick coverage — the background scout doesn't tick aged tokens near their long horizons — not tape selection. This is the first crypto lane that *fetches* to fill that gap: a small, **frozen** research cohort gets **manual** market/liquidity observations near each 15m/1h/6h/24h mark via the existing read-only DexScreener adapter, persisting an ordinary `crypto_price_tick` (so the tape's survival horizons actually mature) plus an audit observation row.

**Manual only, by construction: no timer, no scheduled path, no loop, no autonomy, no flag.** It uses DexScreener (free, no key), so it has **zero SolanaTracker budget impact**. Misses are recorded honestly (`token_inactive` / `provider_no_pair` / `no_liquidity_state` / `request_failed`) — never fabricated.

```bash
python -m app.cli crypto-horizon-cohort-create --limit 25 --hours 48 [--dry-run]   # freeze a fixed cohort (max 100)
python -m app.cli crypto-horizon-schedule-report --cohort-id N [--top N]             # exact UTC/PT windows + next manual action
python -m app.cli crypto-horizon-reminder-plan --cohort-id N                         # static deduplicated reminders; installs nothing
python -m app.cli crypto-horizon-observation-report --cohort-id N --shadow          # pre-observation coverage-gain + provider-load estimate
python -m app.cli crypto-horizon-observe-once --cohort-id N --limit 25 [--dry-run]   # ONE bounded pass over due horizons
python -m app.cli crypto-horizon-observation-report --cohort-id N --top 5            # completion/liquidity rates, gates, examples
```

**Manual scheduling workflow (CRYPTO-HORIZON-SCHEDULE-001):** schedule report
→ static reminder plan → human checks `observe-once --dry-run` → human explicitly
runs `observe-once` → observation and outcome-reconciliation reports. The
schedule reuses the observation planner's exact targets and inclusive window
boundaries, renders UTC plus DST-safe `America/Los_Angeles` timestamps, and
deduplicates overlapping windows that can truly share one bounded pass. It is
compute-on-demand only: no reminder persistence, provider call, timer, cron,
daemon, flag, MarketOps hook, or automatic observation invocation exists.

The **planner** classifies each (token, horizon) as `not_due` / `due_now` / `already_observed` / `overdue_unobserved` / `inactive`; a pass fetches only `due_now` horizons, nearest-target-first, one fetch per token (serving all its due horizons), hard-capped at the limit (≤100 calls). An *observed* horizon is frozen; a *failed* horizon (no usable liquidity) is retried in place on a later pass — never duplicated. The **report** gives completion rate and liquidity-field completion by horizon, inactive/no-pair rates, target-distance distribution, early-liquidity diagnostics for 15m/1h, and **measurement-only success gates** (15m≥0.80, 1h≥0.80, 6h≥0.70, 24h≥0.60, liquidity-state≥0.80). Observation only — no EV, no recommendation, no sizing, no orders, no wallets/keys/swaps/signing/execution.

**Pair selection & outcome proof (CRYPTO-HORIZON-OBS-002).** The first real pass failed 3/5 observations as `no_liquidity_state` because selection was naive `max(liquidity_or_0)`. OBS-002 replaces it with a deterministic **active-pair-quality** selection over *all* candidate pairs: it requires a valid price and positive liquidity (an *eligible* pair), prefers recent activity/volume and an exact base-token match, penalises stale/inactive pools, and preserves an honest `no_liquidity_state` only when **no** candidate carries liquidity — never fabricating liquidity from FDV/market-cap/volume. It captures compact per-candidate diagnostics (never the full raw payload) in the observation's audit field.

```bash
python -m app.cli crypto-horizon-pair-selection-report --cohort-id N --top 5             # per failed token: was it avoidable? which policy would pick which pair?
python -m app.cli crypto-horizon-outcome-reconciliation-report --cohort-id N --top 5      # proof: did an observation flip an outcome unknown->known?
```

The **pair-selection report** shows, per failed observation, the candidate count, current selection, whether another returned pair had usable liquidity, which shadow policy (`first_returned` / `maximum_liquidity_usd` / `highest_recent_volume_with_liquidity` / `newest_active_pair` / `pump_or_launchpad_preferred_then_amm` / `active_pair_quality_score`) would select which pair, and the projected completion improvement. The **outcome-reconciliation report** proves the payoff cohort-specifically: for each observed horizon it recomputes survival **with vs without** the observation's exact tick (read-only), isolating that tick's contribution and sidestepping aggregate counts polluted by unrelated new births — `transitioned_unknown_to_known` is the headline. The observation report's counts are also reconciled into explicit disjoint buckets (`horizon_due_total`, `due_now`, `overdue_unobserved`, `attempted`, `observed`, `missed_attempted`, `skipped_not_due`) with each rate naming its denominator.

## MarketOps Autopilot (OPS-006)

**Read-only coordination, not new capability.** One autopilot cycle sequences the existing services: inspect fresh signals → auto-promote top-N → process promoted (fresh enrichment/assessment/research/forecast via whatever the env flags select) → crypto scan → outcome sync → forecast scoring → champion/challenger snapshot → local DB alerts → one `marketops_runs` audit row. Every stage is individually guarded — a failing stage records its error in the run summary plus a `provider_error` alert and the cycle continues (`MARKETOPS_FAIL_FAST=false`). The autopilot can promote, process, research, forecast, score, and report; it **cannot trade, paper trade, calculate EV, size positions, place orders, or move money** — those capabilities do not exist anywhere in this codebase.

**Auto-promotion is deterministic (OPS-009):** candidates inside minute-level, domain-aware freshness windows (baseball/soccer/live sports 20m, general 60m; the legacy hour knob survives as a coarse upper bound) are ranked by a **measurement-readiness score** — freshness, source-backed capability (baseball/soccer first), market-type measurability (spread/total/winner/advance high; unknown low; player props lowest, since team-level evidence cannot price a player), watcher signal-type priority, and live book quality (two-sided midpoint, spread/liquidity vs the edge-precheck thresholds, tick freshness). The score orders promotion only — it is never EV, value, or a trade quantity. Run summaries record promoted ages, domain/market-type/signal-type breakdowns, skipped-stale and unmeasurable-candidate counts. Original rules still hold: only `new`, non-errored signals aged between `MARKETOPS_MIN_SIGNAL_AGE_SECONDS` and `MARKETOPS_MAX_SIGNAL_AGE_HOURS`; source-backed-capable domains (`sports_baseball`, `sports_soccer`) first, then watcher signal-type priority, then newest; at most one signal per ticker per cycle, capped at `MARKETOPS_PROMOTE_LIMIT`; tickers already awaiting processing or refreshed within the last hour are skipped.

**Alerts** are local DB rows only (no Slack/Discord yet), deduped while open: `service_health_warning`, `too_many_signals`, `no_recent_signals`, `crypto_signal_spike`, `source_backed_forecast_created`, `champion_challenger_sample_update`, `provider_error`, `db_growth_warning`.

```bash
python -m app.cli marketops-run-once            # one cycle (always allowed manually)
python -m app.cli marketops-report              # last run, canary/forecaster/crypto/cc snapshot, open alerts, recommended action
python -m app.cli marketops-alerts --limit 20   # newest alerts (--status open|resolved)
python -m app.cli marketops-resolve-alert 3
python -m app.cli marketops-loop --interval 300 # refuses to start unless ENABLE_MARKETOPS_AUTOPILOT=true; clean SIGINT/SIGTERM
```

`GET /marketops/runs`, `GET /marketops/runs/{id}`, `GET /marketops/report`, `GET /marketops/alerts`, `PATCH /marketops/alerts/{id}/resolve` serve the same data.

**Rollout (EVO-X2): dark → run-once → optional timer.** Deploy with the flag false, run `marketops-run-once` manually and inspect the report/alerts, then optionally install `infra/systemd/user/probability-arena-marketops.{service,timer}` (5-minute cadence; **not auto-installed** — install commands are in the timer file).

**Overlap guard (OPS-007):** concurrent cycles are impossible — a second invocation (manual run during a timer firing, or vice versa) records a graceful `skipped` (`already_running`) run; a `running` row older than `MARKETOPS_LOCK_STALE_AFTER_MINUTES=30` is treated as crashed and never wedges the system. SQLite connections carry a `SQLITE_BUSY_TIMEOUT_MS=30000` write-lock wait (Postgres unaffected).

## Meme/news scout + domain expansion (MEME-NEWS-001)

**Read-only discovery and scouting — it does not trade, paper trade, compute EV, recommend trades, size positions, place orders, or use wallets/keys/swaps/signing/execution.**

*Part A — meme attention scout* (`meme-scan-once`, `meme-scout-report`): over the newest DexScreener token profiles + boosted tokens (public read-only GETs already in scope) it records a per-token **`attention_score`** (0–1) into `meme_attention_snapshots`. The score combines freshness, liquidity growth, volume growth, boost velocity, profile/metadata completeness, and social/catalyst presence, then applies the existing read-only risk overlay as a penalty and dampens by provider confidence (missing provider data — the holder/sniper/insider/`provider_unknown` gap — lowers confidence). **`attention_score` is an interest/velocity signal for human review — explicitly not a buy score, trade score, EV, alpha score, or recommendation, and it triggers no behavior.**

*Part B — generic catalyst abstraction* (`catalyst-report`): a source-agnostic `meme_catalyst_events` table (source · subject · catalyst_type · magnitude). Today only public read-only dexscreener sources populate it (`profile_seen`, `boost`, `boost_increase`, `social_present`); rss/x/discord/telegram are schema placeholders added later **only if explicitly configured** — no authenticated scraping.

*Part C — domain-expansion scout* (`domain-scout-report`): a read-only inventory over the probability markets we have already scanned, grouped by domain/series prefix (`domain_market_inventory_snapshots`). Per domain: market/active counts, two-sided rate, volume/liquidity proxy, resolution-clarity proxy, whether an evidence forecaster already exists, public data-source notes, and a **`canary_priority`** ranking candidate expansion domains (weather, tennis, basketball, golf, esports, …). It **adds no forecaster and changes no promotion/forecast/edge logic** — planning intelligence only.

```bash
python -m app.cli meme-scan-once --limit 30   # read-only DexScreener pass → attention snapshots + catalysts
python -m app.cli meme-scout-report           # attention aggregates, top tokens (interest only)
python -m app.cli catalyst-report             # catalyst-event stream by type/source
python -m app.cli domain-scout-report         # market-domain inventory + canary priority
```

Flags `ENABLE_MEME_SCOUT` / `ENABLE_DOMAIN_SCOUT` (default false) are reserved for any future loop/timer; the manual commands above are always allowed (mirroring the crypto lane). No EV, paper trading, recommendation, sizing, order, wallet/key, swap, signing, or execution exists anywhere in this milestone.

### Scheduled meme/news discovery lane (MEME-NEWS-002)

**Read-only scheduled discovery — `attention_score` is not EV, not a recommendation, not an instruction, and no wallet/key/swap/order/signing/execution/sizing/paper-trading/live-trading exists.** Turns the manual scout into a bounded, always-on lane (a `systemd --user` timer firing every 10 minutes), with a windowed report and derived, local, informational alerts.

```bash
python -m app.cli meme-news-run-once            # one bounded read-only cycle (manual: always allowed)
python -m app.cli meme-news-run-once --scheduled  # timer mode: no-ops unless ENABLE_MEME_NEWS_SCOUT=true
python -m app.cli meme-news-report --hours 24   # last run, runs/window, new tokens, catalysts, attention p50/p90/max, top tokens, severe/high-risk, provider confidence, missing holder coverage, row counts, errors
python -m app.cli meme-news-alerts --hours 6    # derived notable events (informational, never a trade trigger)
```

The runner (`MemeNewsScoutRunner`) wraps `MemeScoutService.scan_once` in a bounded, no-raise cycle that records the same audit spine and degrades gracefully on provider errors — it runs as its own systemd unit, independent of and unable to affect MarketOps/EDGE-AUTO. **`ENABLE_MEME_NEWS_SCOUT` (default false) gates only the `--scheduled` path**; manual `meme-news-run-once` and all reports are always allowed. Alerts cover: new token above `MEME_NEWS_ATTENTION_ALERT_THRESHOLD`, attention jump, boost increase, severe/high-risk token (an avoid/flag verdict — never a trade direction), and provider degradation (the holder/sniper/insider coverage gap) — all local DB-derived report rows, **no push notifications, no recommendations**. Systemd units live in `infra/systemd/user/probability-arena-meme-news.{service,timer}` (**not auto-installed**; install instructions in the timer comments and runbook). Retention (`MEME_NEWS_RETENTION_DAYS=14`) prunes `meme_scout_runs` / `meme_attention_snapshots` / `meme_catalyst_events` to bound the always-on lane (documented); domain-scout inventory tables are kept.

## Cross-venue observation (POLY-002)

**Read-only Kalshi ↔ Polymarket semantic matching + measurement — not advice, not
arbitrage.** A deterministic normalizer (title/outcome/date) + matcher runs over
already-persisted Kalshi markets/snapshots and POLY-001 Polymarket markets to
identify **comparable** markets and MEASURE observable differences.

```bash
python -m app.cli cross-venue-match-once          # one read-only matching/observation pass (persists candidates)
python -m app.cli cross-venue-match-once --recent-hours 48 --domain sports   # narrow the sample
python -m app.cli cross-venue-match-once --kalshi-limit 8000 --polymarket-limit 600   # explicit wide pass
python -m app.cli cross-venue-report              # candidate counts, comparables, midpoint-difference distribution, spread/liquidity, freshness
python -m app.cli cross-venue-candidates --label comparable_market_candidate   # list candidates from the latest run
```

Each candidate carries a `match_label` (`comparable_market_candidate` /
`unresolved_semantic_match` / `incompatible_outcome` / `incompatible_resolution`
/ `low_confidence_match`) and measurement-only fields: `kalshi_midpoint` /
`polymarket_midpoint` on a 0–1 probability scale, spreads, liquidity proxies, and
**`observed_difference` = |kalshi_mid − polymarket_mid|** (a measured
probability-point gap). It **does not compute EV, label arbitrage, recommend
trades, paper trade, size positions, place orders, or use wallets/keys/signing/
swaps/execution** — there is no side/size/EV/action/order/wallet field by
construction. No external call (it matches persisted rows), no timer. Ambiguous
data yields `unresolved_semantic_match`, never a forced match.

**Sample selection (XVENUE-OPS-001).** Kalshi rows are loaded **most-recently-seen
first** (`last_seen_at DESC`), so the default run considers current markets rather
than the oldest-inserted rowid slice — which on a long-running DB is stale rows
still flagged `active` but not refreshed for days (e.g. games that already
resolved), with no overlap against a freshly-scanned Polymarket sample. Defaults
are bounded and representative (`--kalshi-limit 4000`, `--polymarket-limit 500`);
`--recent-hours N` drops markets not seen inside the window, and `--domain` /
`--market-type` narrow the sample. Every run prints its **sample composition** —
rows loaded/considered, per-domain and per-market-type breakdown on both venues,
stale/no-snapshot counts, domain overlap, and a low-overlap coverage note when no
comparable rows surface (a note about observation coverage, **never** a signal).
These options change *which* persisted rows are considered; they never change a
label, relax a precision gate, or force a match.

## Cross-venue observation windows (XVENUE-OBS-001)

**Read-only coverage reporting for high-overlap slates** (World Cup
semifinal/final, MLB slates, election windows). The manual sequence lives in
`docs/XVENUE_OBSERVATION_RUNBOOK.md` — targeted scan → coverage census → match →
report → candidates-by-label — and ends with a one-screen window verdict:

```bash
python -m app.cli xvenue-observation-report   # composes the latest persisted scan + match runs
```

It reports scan provenance (window, mode, queries), rows considered on both
venues, the comparable split **clean vs flagged-for-review** (clean = no
`large_observed_difference_requires_review`), side-uncertain/unresolved counts,
mismatch reasons, sample clean candidates, and an **overlap assessment**
(`no_scan_data` / `no_match_run` / `insufficient_overlap` /
`overlap_no_clean_comparable` / `clean_comparable_present`) — plus a warning when
the latest match run predates the latest scan. Derived on demand from persisted
rows: no external call, nothing persisted, no timer, no new match label. A clean
comparable is a **coverage fact** — the venues listed the same proposition — and
the assessment is observation language for human review, never an opportunity,
arbitrage, EV, or trade signal. The runbook's domain guidance is grounded in
measured live data: game-winner ↔ game-winner is the realistic clean shape for
World Cup and MLB windows; politics is census-first (thin Kalshi supply,
resolution gaps); crypto currently has no comparable supply; tennis counts only
match-winner ↔ match-winner.

## Cross-venue matcher precision (POLY-PRECISION-001)

**Read-only semantic + midpoint CORRECTNESS work.** It identifies no arbitrage,
computes no EV, recommends no trades, paper trades nothing, sizes nothing, places
no orders, and uses no wallets/private keys/signing/execution.

POLY-COVERAGE-001 widened the Polymarket sample and exposed two POLY-002 defects.

**1. The Polymarket midpoint had no side.** A Polymarket market's book
(`best_bid`/`best_ask`) and `outcome_prices[0]` price `outcomes[0]` — verified
against the live API, the book midpoint equals `outcome_prices[0]` in 97/97
sampled markets with a book. But `outcomes[0]` is `"Yes"` only ~74% of the time;
~26% of markets name entities instead (`["Kansas City Royals","New York Mets"]`,
`["Over","Under"]`). Kalshi, meanwhile, encodes a game market's Yes side in the
**ticker suffix**, not the title: `KXMLBGAME-…SDLAD-SD` and `…-LAD` both read
"San Diego vs Los Angeles D Winner?". The old code compared P(`outcomes[0]`) to
Kalshi's P(Yes) regardless, which is what produced the large spurious gaps.

A midpoint — and therefore any `observed_difference` — is now produced **only**
when the Polymarket outcome has been explicitly aligned to the Kalshi YES
proposition, by Yes/No labels (including reversed `["No","Yes"]` ordering),
Over/Under direction, or a uniquely-matched named entity. Otherwise both are
**absent**, the pair is annotated `outcome_side_uncertain` or
`midpoint_side_uncertain`, and it stays `unresolved_semantic_match`. Nothing is
guessed.

**2. "O/U 2.5" classified as `yes_no`.** `normalize_title` strips punctuation, so
the slash was gone before the over/under test ran — and for the same reason the
`[+-]\d` handicap test could never fire at all. Over/under, handicap and scoreline
signals are now read from the raw title, before stripping.

On top of that, deterministic compatibility gates, each recording its own reason:

| Gate | Rejects | Reason |
|---|---|---|
| Outcome type | `yes_no` no longer matches `winner` | `outcome_type_mismatch` |
| Market scope | player prop vs game vs tournament future | `market_type_mismatch` |
| Threshold | over/under line, handicap line, entity-anchored scoreline | `threshold_mismatch` |
| Entity | disjoint named entities (generic words filtered) | `entity_mismatch` |
| Sport | Counter-Strike 2 vs Valorant | `sport_or_game_mismatch` |

Scorelines are compared **anchored on the entity**, because Kalshi writes
"Spain wins 3-1" where Polymarket writes "Spain 1 - 3 Belgium" — the same Spain,
contradictory scores. Sport identity comes from a prefix-anchored Kalshi ticker or
an unambiguous title term; entity overlap ignores generic words so "Esports",
"Gaming", "Map" and "Winner" can never be the sole evidence two markets match.

A measured gap above `LARGE_OBSERVED_DIFFERENCE` (0.35) with semantic confidence
below `HIGH_SEMANTIC_CONFIDENCE` (0.85) adds
`large_observed_difference_requires_review`. That is a suspicion that the **match
is wrong** (or a quote is stale) — **never an opportunity, an edge, an arbitrage,
or an action** — and a large gap alone never rejects a pair. Ambiguity always
degrades to `unresolved_semantic_match`, never to `comparable_market_candidate`.

Measured on a scratch DB seeded with real Kalshi rows, against the same
396-market Polymarket sample:

| | before | after |
|---|---|---|
| candidates (audit rows) | 143 | 143 |
| `comparable_market_candidate` | 9 | **2** |
| measured-gap p50 | 0.39 | **0.125** |

The five `KXMLBGAME` false pairs (gaps 0.37–0.49) became `outcome_side_uncertain`
with no midpoint. The two survivors are the genuinely identical GPT-5.6 markets,
both flagged for review — their gap is a stale Kalshi quote, which is exactly what
the flag is for.

## Memecoin multi-agent diagnostic (MEME-MAS-001)

**Read-only diagnostic intelligence — not advice.** Five deterministic "agents"
(pure functions — no LLM, no external calls, no new providers) turn
already-persisted data into diagnostic sub-scores and a `review_priority` that
triages how much **human review** a token warrants:

- **Coin Structure** — liquidity/volume quality, top10/sniper/insider/bundler concentration, authority/rug/honeypot, provider coverage
- **Catalyst Velocity** — attention score + jump, boosts, social metadata, catalyst frequency, profile completeness
- **Timing** — token age, momentum, boost recency, attention persistence
- **Risk Auditor** — severe/high risk, concentration red flags, fake-volume / liquidity-removed, missing/unknown provider coverage
- **Composite Review** — `review_priority`: `low` · `monitor` · `elevated_review` · `high_review` · `reject_risk`

```bash
python -m app.cli meme-mas-report --hours 24 --top 10        # top candidates by review_priority, risk rejects, missing coverage, sub-score distributions, reason traces
python -m app.cli meme-mas-assess --limit 20                 # per-token diagnostic traces
python -m app.cli meme-mas-calibration-report --lookback-hours 48   # MEME-MAS-002 before(v1)/after(v2) label calibration via MEME-SHADOW
```

**MEME-MAS-002 recalibration** (informed by MEME-SHADOW-001, which showed missing
provider coverage and concentration flags predicted worse survival while the old
velocity-heavy composite diluted them): the scorer is now profile-based (`v2`
default; `v1` preserved for the before/after report). v2 penalizes risk more
heavily (missing coverage, concentration, fake-volume/liquidity-removed), dampens
the review score fully by risk, blends **momentum + structure**, and **gates
high_review** so it requires strong momentum, clean structure, non-missing
provider coverage, and no concentration flags. It also emits first-class
`momentum_quality` / `structure_quality` / `coverage_quality` outputs. `reject_risk`
hard gates are unchanged. Still read-only diagnostic label calibration — no trade
behavior.

Inputs are `meme_attention_snapshots` + `crypto_token_risk_assessments` +
`meme_catalyst_events` — recomputed **on demand** (no new table/migration, no
external request, no SolanaTracker budget impact). **`review_priority` is a
human-review triage label, NOT a trade signal** — it computes no EV, does no
paper trading, sizes no positions, places no orders, recommends no trade/side,
and uses no wallets/keys/swaps/signing/execution. `reject_risk` is an avoid/flag
verdict for review, never a trade direction.

## Memecoin label follow-through (MEME-SHADOW-001)

**Read-only calibration measurement — not advice, not PnL.** Does a MEME-MAS
`review_priority` actually predict later token behavior? MEME-SHADOW reconstructs
the label at each historical attention snapshot (reusing the MEME-MAS agents with
the risk assessment *as-of* that moment), then measures how the **same token**
moved afterwards from its own later snapshots.

```bash
python -m app.cli meme-shadow-report --lookback-hours 48       # outcome by review_priority / sub-score / risk reason / concentration, + calibration recommendation
python -m app.cli meme-mas-objectives-report --lookback-hours 48   # MEME-MAS-003 multi-objective calibration (momentum / survival / risk-adjusted / queue-efficiency / coverage), v1 vs v2
```

**MEME-MAS-003 multi-objective calibration** — because a single survival yardstick
misjudges volatile high-momentum review tiers, `meme-mas-objectives-report` scores
review_priority across five separate axes (v1 vs v2): **momentum_followthrough**
(positive-move rate + median price), **survival_quality** (survival/rug/severe-end),
**risk_adjusted_movement** (median move × survival — a measured diagnostic, never a
return/PnL), **review_queue_efficiency** (queue share + momentum-positive lift vs
overall), and **coverage_quality** (label-independent covered-vs-missing outcomes).
It shows high_review is momentum-positive even when survival-lower, elevated_review
is safer, and missing coverage predicts worse outcomes. Measurement only — it
changes no label.

Metrics per cohort: price/liquidity/volume change at **5m/15m/1h/6h/24h**,
survival (no-liquidity-collapse) rate, rug/liquidity-removed incidence, attention
persistence, risk-level transition. Cohorts are labelled `too_thin` (n < 12) or
`measured`, and an overall recommendation reports whether the labels separate
outcomes (`labels_separate_outcomes` / `no_material_separation_recalibrate` /
`review_priority_inverted_recheck` / `too_thin_to_calibrate`). `price_change` is
**measured market movement of the token, exactly like the edge follow-through
analysis — not PnL, not a fill, not EV, not paper trading, not a trade
recommendation, not position sizing.** It changes no label and authorizes
nothing; computed on demand (no table, no external call, no SolanaTracker budget
impact).

## Polymarket market-data observer (POLY-001)

A **read-only second prediction-market venue**. It observes Polymarket
microstructure alongside Kalshi using only public, no-authentication endpoints —
the Gamma market catalog (`gamma-api.polymarket.com/markets`) and the CLOB
read-only order book (`clob.polymarket.com/book`). No API key, wallet, or
signing is used or required, and the authenticated CLOB trading endpoints are
deliberately **not implemented**.

```bash
python -m app.cli polymarket-scan-once --limit 50   # one bounded read-only scan (manual: always allowed)
python -m app.cli polymarket-scan-once --scheduled  # timer mode: no-ops unless ENABLE_POLYMARKET_SCOUT=true
python -m app.cli polymarket-report --hours 24      # markets seen/active/categories, two-sided + orderbook availability, spread/depth/liquidity proxies, newest + highest-volume/liquidity markets, provider health, row counts
python -m app.cli polymarket-domain-report          # per-category inventory from the latest scan
```

`PolymarketScoutService.scan_once` fetches the market catalog, persists a
`polymarket_markets` snapshot per market, fetches up to `POLYMARKET_ORDERBOOK_LIMIT`
token order books into `polymarket_orderbook_snapshots` (spread/depth/liquidity
proxies), rolls up a per-category `polymarket_domain_inventory_snapshots`, and
records a `polymarket_scout_runs` audit row — degrading gracefully to "nothing
observed" on any provider problem. **`ENABLE_POLYMARKET_SCOUT` (default false)
reserves loop/timer use only** (no timer is installed; manual runs
and all reports are always allowed). Retention (`POLYMARKET_RETENTION_DAYS=14`)
prunes markets/orderbook/scout-run rows; the domain-inventory coverage table is
kept. Prices and order books are **informational quotes for human review — never
EV, a recommendation, an instruction, or a trade trigger**. Cross-venue semantic
linking to Kalshi shipped in **POLY-002** (comparability verdicts + measured
probability-point differences; no arbitrage, EV, or trade-candidate labels exist).
No sizing, orders, wallets, keys, swaps, signing, or execution anywhere.

## Polymarket coverage expansion (POLY-COVERAGE-001)

**Read-only coverage expansion + a supply census.** POLY-002 found `0`
comparable candidates against a 50-market, tournament-winner-heavy Polymarket
sample: the constraint was *supply*, not the matcher. This milestone broadens and
targets WHICH public markets POLY-001 observes. It **does not identify arbitrage,
compute EV, recommend trades, paper trade, size positions, place orders, or use
wallets/private keys/signing/execution**, and it never forces a match.

```bash
# broader: bounded pagination (Gamma `offset`), category + resolution-window filters
python -m app.cli polymarket-scan-once --limit 400 --orderbook-limit 20 \
    --category 21 --end-date-min 2026-07-08T00:00:00Z --end-date-max 2026-07-22T00:00:00Z

# targeted: search queries derived deterministically from persisted Kalshi titles/tickers (no LLM)
python -m app.cli polymarket-scan-once --targeted --limit 400 \
    --end-date-min 2026-07-08T00:00:00Z --end-date-max 2026-07-22T00:00:00Z
python -m app.cli polymarket-scan-once --query "world cup" --query mlb   # explicit queries

python -m app.cli polymarket-coverage-report --top 20   # per-domain/market-type SUPPLY census
python -m app.cli cross-venue-match-once --polymarket-limit 600   # rerun POLY-002 on the wider sample
```

**Query-parameter contract** (verified against the live public API — Gamma returns
HTTP 200 and *silently ignores* unknown parameters, so only parameters observed to
change the result set are used): `/markets` honours `offset` (real pagination),
`tag_id`, `end_date_min`/`end_date_max`, and caps a page at 100 rows.
`/public-search` paginates by `page` (1-based) and **ignores `offset`**; it exposes
no date parameter, so active/closed and the resolution window are filtered
client-side. Search returns markets nested inside events, and those nested markets
omit `endDate` and `events` — both are inherited from the parent event, without
which every search-sourced market would lack a resolution time and could never be
labelled comparable.

`--targeted` counts evidence for a fixed topic registry across already-persisted
Kalshi ACTIVE markets, using whole-word title terms **and prefix-anchored ticker
series** (`KXWC*`, `KXMLB*`, `KXITF*`…). Tickers matter: Kalshi's `category` is
empty and its titles are game-prop text, so ~1,100 active World Cup and ~1,160
tennis markets are invisible to title-only matching. Prefix anchoring is required
too — a substring test for `FED` matches the MLB ticker of pitcher Erick Fedde.
Topics are emitted only when Kalshi evidences them, ranked by evidence count.

Budgets are hard: targeted queries claim the market budget first, each capped at a
**fair share of the remaining budget** so one high-yield topic (a single `mlb`
search returns hundreds of season/draft futures) cannot starve the others; the
catalog then fills the remainder. Skipped queries, fair-share caps, and Kalshi
census truncation are **logged, never silent**. `polymarket_scout_runs` records the
scan provenance (`scan_mode`, `pages_fetched`, `market_fetch_errors`,
`duplicates_dropped`, `queries_used` — the queries actually *sent*, migration 0022).

`polymarket-coverage-report` is a **supply census**, not a matcher: per-domain and
per-market-type counts on both venues, order-book coverage, two-sided rate,
spread/depth, domain overlap, and which domains have (or lack) the structural
prerequisites for a comparison to be *attempted* — with the reason when they don't
(`no_polymarket_markets`, `no_kalshi_markets`, `no_polymarket_resolution_time`,
`no_kalshi_resolution_time`, `no_yes_scale_outcome_type_on_either_venue`).
`comparable_supply` means **a comparison could be attempted here**, never *this is
an opportunity*. It pairs no markets, scores no pair, and measures no price
difference.

## Edge precheck (MVP-005A) — probability-gap measurement

**Measurement, never advice.** The gate ADR-004 defined has crossed (paired champion/challenger n=36, both deltas negative), so the accepted design (`docs/MVP_005A_EDGE_PRECHECK_DESIGN.md`) is now implemented: for recent forecasts, record `probability_gap = forecast_probability − market_midpoint` (signed, probability units — **not dollar EV**) with validity checks, into append-only `edge_precheck_snapshots` rows. By construction the table has no side, size, EV, or action fields.

Statuses (deterministic precedence, all failures recorded in `invalidation_reasons`): `invalid_resolution_risk` → `invalid_not_source_backed` → `invalid_stale_forecast` → `invalid_stale_market_snapshot` → `invalid_low_confidence` → `invalid_wide_spread` → `invalid_low_liquidity` → `no_gap` → `watchlist` → `paper_candidate_later`. A gap must persist across `EDGE_PRECHECK_REQUIRED_PERSISTENCE_SNAPSHOTS=3` same-direction valid measurements before earning `paper_candidate_later` — which is **a review label for a possible future, separately-gated MVP-005B; it is not an instruction and triggers no behavior**.

```bash
# Targeted modes (MVP-005A.1) — what automation should use: measure exactly
# the fresh forecasts, not a broad sweep
python -m app.cli edge-precheck --latest-marketops-run          # forecasts refreshed by the latest cycle
python -m app.cli edge-precheck --marketops-run-id 47
python -m app.cli edge-precheck --recent-refreshed-signals --limit 10
python -m app.cli edge-precheck --forecast-id 123               # or --forecast-ids 1,2,3

# Broad sweep — manual diagnostics only (stale-forecast noise by design)
python -m app.cli edge-precheck --limit 50            # requires ENABLE_EDGE_PRECHECK=true…
python -m app.cli edge-precheck --force-readonly      # …or an explicit one-off (still measurement rows only)
python -m app.cli edge-precheck-report                # statuses, cohorts, gap stats — labeled measurement-only
```

Targeted modes skip forecasts measured within `EDGE_PRECHECK_DEDUPE_SECONDS=120` and (except explicit `--forecast-id`) select only source-backed forecasts (`EDGE_PRECHECK_TARGET_ONLY_SOURCE_BACKED=true`). If MarketOps integration is ever enabled it is strictly **cycle-scoped**: only forecasts refreshed by that same cycle's processed signals are measured — never a latest-N sweep.

`GET /edge-precheck/snapshots`, `GET /edge-precheck/report`, `POST /edge-precheck/run` (flag-gated, or `force_readonly=true`). MarketOps can run a measurement pass per cycle only when **both** `MARKETOPS_INCLUDE_EDGE_PRECHECK=true` and `ENABLE_EDGE_PRECHECK=true` (both default false). No orders, paper trades, position sizes, wallets, swaps, or execution exist anywhere; MVP-005B (paper simulator) remains a separate, explicitly-gated future milestone.

### Edge cohort analysis (EDGE-ANALYSIS-001)

**Analysis only — no advice, no PnL.** Slices the accumulated `edge_precheck_snapshots` watchlist/`paper_candidate_later` population into cohorts and measures **gap follow-through per cohort** (did the midpoint later move toward the forecast at 5/15/30/60-minute horizons — market-movement measurement, *not* fills/positions/PnL), so a human can see which market types and conditions actually show follow-through and which should be deprioritized in future gating.

```bash
python -m app.cli edge-cohort-report --hours 24
```

Cohort dimensions: market type · domain · gap sign · absolute-gap bucket · confidence bucket · signal type · liquidity bucket · spread bucket · game phase · persistence count. Per cohort it reports sample/watchlist/candidate/invalid counts, mean |gap|, average confidence, per-horizon moved-toward rate + gap-closure, and a conservative **label**: `too_thin` (below the sample floor), `promising`, `neutral`, `weak`, or `exclude_candidate`. `promising` requires a minimum follow-through sample count — a high rate on a handful of rows stays `too_thin`. The report's recommendation section lists cohorts to observe more vs. deprioritize and states explicitly whether **MVP-005B-design remains blocked** (it stays blocked unless a cohort clears both a strict sample floor and toward-rate bar *and* overall follow-through does too — and even then advancing requires explicit human acceptance; this report unlocks nothing). It changes no flag, threshold, promotion, forecast, or edge logic.

### Edge shadow-policy analysis (EDGE-POLICY-001)

**Read-only shadow analysis — it changes no live gating.** Simulates candidate cohort **filters** over the already-recorded watchlist / `paper_candidate_later` rows to ask: would excluding weak cohorts leave a stronger measurement population? It re-slices existing rows only — no flag, threshold, promotion, forecaster, edge-precheck, or service change.

```bash
python -m app.cli edge-policy-report --hours 24
```

Simulates 13 named policies (`baseline_all_watchlist`, `exclude_winner`, `exclude_late_game`, `exclude_confidence_065_plus`, `exclude_abs_gap_gt_015`, `exclude_liquidity_1m_10m`, `small_gap_only_005_010`, `spread_2_5c_only`, `liquidity_lt_100k_only`, `totals_only`, `spreads_only`, `exclude_all_current_bad_cohorts`, `conservative_candidate_policy`). Per policy: included/watchlist/candidate/invalid counts + invalid rate, per-horizon (5/15/30/60m) moved-toward rate and gap closure, market-type/domain/gap/confidence/persistence distributions, a **settlement-conditioned** block on resolved markets (forecast Brier vs market-midpoint Brier, delta, log-loss, forecast-beats-market rate — calibration only, **not EV, not PnL, not a trade**), and a **label**: `too_thin` / `worse_than_baseline` / `neutral` / `promising_shadow` / `reject_policy`. The decision section reports whether any policy clears the follow-through gate (n≥20 and moved-toward ≥0.55 at 30m or 60m), improves over baseline, preserves sample, whether settlement disagrees with short-horizon follow-through, and whether **MVP-005B-design remains blocked** (it does unless a policy clearly clears the gate — and even then advancing needs explicit human acceptance). No EV, paper trading, recommendation, sizing, order, or capital anywhere.

## Frontier evaluation (EVAL-001)

**Evaluation only — it measures the desk, it never acts.** One report covers the full read-only pipeline over a time window: signal quality (seen/promoted/processed rates, by type and domain), forecast quality (champion/challenger paired metrics, forecaster/market-type/confidence breakdowns), edge-precheck quality (statuses, reasons, persistence and gap-direction distributions, valid-measurement rate), **gap follow-through** (did later midpoints move toward the forecast — market-movement analysis at 5/15/30/60-minute horizons, explicitly *not PnL*, no fills, no positions), microstructure validity (two-sided rate, spread/liquidity percentiles), crypto risk quality (levels, provider health, post-risk-signal liquidity movement), latency (MarketOps p50/p90/p99, signal→forecast→measurement lags), and a **safety audit** (AST-level identifier scan: banned trading vocabulary must not exist as code identifiers; boundary docstrings pass).

```bash
python -m app.cli frontier-eval-report --hours 24 --domain sports_baseball --include-crypto --include-safety --save-run
```

`GET /eval/frontier-report` serves the same report. The **readiness scorecard** is deliberately conservative: `not_ready` → `observe_more` → `ready_for_manual_edge_measurement` → `ready_for_cycle_scoped_edge_automation` → `ready_for_paper_design`. No watchlist rows means `not_ready`, full stop. **There is no live- or autonomous-trading label by design, and no readiness label ever authorizes live capital** — the ladder gates further *measurement* milestones only. `--save-run` persists a `frontier_eval_runs` audit row.

## Database backups (OPS-007)

```bash
python -m app.cli backup-db          # consistent gzipped snapshot (sqlite3 online backup API) + retention pruning
python -m app.cli list-db-backups
python -m app.cli verify-db-backup data/backups/backup-<stamp>.db.gz   # integrity_check + expected tables
```

`BACKUP_DIR=data/backups`, `BACKUP_RETENTION_DAYS=30`. Non-SQLite databases get safe `pg_dump` guidance instead (never executed). Optional daily timer artifacts: `infra/systemd/user/probability-arena-backup.{service,timer}` (not auto-installed). `db-stats` reports backup count/size.

## Soccer evidence-aware forecaster (SOCCER-002)

The soccer counterpart of the baseball evidence forecaster — behind its own flag, consuming only persisted source-backed soccer packets (SOCCER-001), making no external calls itself:

- `ENABLE_SOCCER_EVIDENCE_FORECASTING=false` (default) — plus `SOCCER_FORECASTER_VERSION=v1`, `SOCCER_FORECAST_MAX_CONFIDENCE=0.70`, `SOCCER_FORECAST_MIN_COMPLETENESS=0.75`.

`ForecastingService` selects `SoccerEvidenceAwareForecaster` only when **all** conditions pass: flag on, domain `sports_soccer`, packet `source_backed`, completeness ≥ 0.75, resolution `researchable`. Everything else — and any explicitly injected forecaster — keeps the template baseline; the baseball gate is untouched.

**Model (deterministic v1, fully stated in each output):** market midpoint is the prior; goal margin (winner/advance/spread) or pace-projected totals produce an evidence estimate; the blend weight grows with match progress and extra time; a level match decays a must-WIN market toward the draw; **penalty shootouts are treated as near-coin-flips (confidence capped at 0.50) except team-to-advance markets, which use the shootout score**; **red cards reduce confidence and add context but never inflate the estimate**; **player-goal markets fall back to template — team-level data must not price a specific player**; the shift from the prior is hard-capped at ±0.25. Tags: `soccer_evidence_v1`, `market_type_winner|total|advance|spread|player_goal|unknown`, `early_match`/`late_match`, `extra_time`, `penalty_context`, `red_card_context`, `live_match_state`, `evidence_adjusted`/`anchored_to_market_mid`.

Soccer evidence forecasts (confidence up to 0.65–0.70) clear the edge-precheck confidence gate (0.60), making World Cup markets measurable; and `champion-challenger-report --domain sports_soccer --challenger soccer_evidence_v1` compares them against the baseline as outcomes settle. **Forecasts are measurement inputs only — no dollar EV, no trade recommendations, no paper trading, no sizing, no orders, no wallets, no execution.**

## Tennis evidence canary (TENNIS-001)

**Read-only evidence and forecasting — it does not trade, paper trade, compute EV, recommend bets, size positions, place orders, or use wallets/keys/swaps/signing/execution.** Adds a tennis canary so `sports_tennis` **match-winner** markets can produce source-backed research packets and evidence-aware forecasts, mirroring the soccer/baseball canaries.

- **Collector** (`TennisExternalResearchCollector`, `tennis-external`): template scaffold + live match evidence (match status, set score, current-set game score, current server, winner/retirement/walkover, tournament, surface, rank/seed) with per-source provenance. Behind `ENABLE_TENNIS_EXTERNAL_RESEARCH` + `TENNIS_RESEARCH_PROVIDER`. **Provider `template` (default) keeps it fallback-only; `espn` selects a read-only public ESPN tennis client whose live payload mapping is PENDING validation** — if the shape differs it produces no usable evidence and falls back honestly (evidence stays `template_only`). Unknown/prop/non-winner tickers fall back honestly.
- **Forecaster** (`TennisEvidenceAwareForecaster`, `tennis_evidence`): match-winner only in v1. Market midpoint is the prior; a conservative, **tightly capped (±0.20)** adjustment comes from the subject player's set/game margin, weighted up as the match progresses; retirement/walkover and completed matches resolve near-certain (still within the cap); **missing critical facts cap confidence at 0.50 with high risk**; overall confidence cap `TENNIS_FORECAST_MAX_CONFIDENCE=0.65`. Calibration tags: `tennis_evidence_v1`, `market_type_winner`, `match_state`, `source_backed`, and `evidence_adjusted`/`evidence_insufficient`.

```bash
# Both default off; enable the canary (research + forecasting) in .env:
#   ENABLE_TENNIS_EXTERNAL_RESEARCH=true   TENNIS_RESEARCH_PROVIDER=template|espn
#   ENABLE_TENNIS_EVIDENCE_FORECASTING=true
python -m app.cli research-canary-report   # tennis-external collector + tennis_evidence forecaster counts appear automatically
```

`ForecastingService` selects `TennisEvidenceAwareForecaster` only when all conditions pass (flag on, domain `sports_tennis`, packet `source_backed`, completeness ≥ 0.75, resolution `researchable`); `SignalProcessingService` selects the collector under `ENABLE_TENNIS_EXTERNAL_RESEARCH` for researchable tennis signals. An explicitly injected collector/forecaster always wins (tests); the existing baseball/soccer/MarketOps/EDGE-AUTO/meme-news behavior is unchanged. **Forecasts are measurement inputs only — no EV, recommendations, sizing, orders, wallets, or execution.**

## Champion/challenger comparison

**Why this exists:** per ADR-004, no EV or paper-trading work may even be designed until a challenger forecaster demonstrably beats the market-anchored baseline. This layer is that gate, made concrete:

```bash
python -m app.cli champion-challenger-report --domain sports_baseball
# options: --baseline template_baseline --challenger baseball_evidence_v1 --paired-only --min-count 30
```

`GET /calibration/champion-challenger` serves the same comparison (filters: forecasters, `domain`, `market_type`, `signal_type`, `min/max_created_at`, `paired_only`).

**Method:** the latest score per forecast, then the latest *scored* forecast per (forecaster, ticker) as that side's representative. **Paired** comparison (same ticker, same outcome — per-market win rate and mean deltas) is the stronger evidence; **unpaired** aggregates and all cohort tables (market type, signal type, confidence bucket, evidence depth, risk, domain, game stage) are labeled as less reliable. Comparisons are computed on demand and deliberately **not persisted** — they are deterministic functions of the append-only `forecast_scores`/`market_forecasts` tables, so a stored copy would only be a cache that can drift.

**Interpretation:**
- `delta_brier < 0` and `delta_log_loss < 0` favor the challenger (deltas are challenger − baseline).
- Paired comparisons beat unpaired; cohort tables are always unpaired.
- Sample-size labels gate everything: `insufficient_sample` (n<30) → the report attaches an explicit "do NOT infer edge" warning; `early_signal` (30–99); `useful_sample` (100–299); `stronger_sample` (≥300).
- A challenger that only *sounds* smarter shows up here as delta ≥ 0 — the whole point.

## Retention & database stats

**Tick growth is the reason retention exists:** at `WATCHER_MARKET_LIMIT=100` and a 60 s interval, the watcher writes ~100 tick rows/minute (~144 k/day) while it runs. Retention prunes **operational tables only**:

| Table | Default window (env var) |
|---|---|
| `market_price_ticks` | 7 days (`TICK_RETENTION_DAYS`) |
| `watcher_runs` | 30 days (`WATCHER_RUN_RETENTION_DAYS`) |
| `pipeline_runs` + `pipeline_stage_runs` | 90 days (`PIPELINE_RUN_RETENTION_DAYS`; a `running` row is never pruned) |
| `opportunity_signals` | keep forever by default (`SIGNAL_RETENTION_DAYS=0`; set > 0 to prune) |

Intelligence and calibration tables — markets, snapshots, scanner runs, eligibility assessments, enrichments, resolution assessments, research packets, forecasts, **outcomes, forecast scores** — are **never pruned** (enforced in code and tests). Deletes run in `RETENTION_BATCH_SIZE` batches to keep transactions short. This deletes only our own telemetry rows; the project remains read-only toward the outside world.

```bash
python -m app.cli prune-retention --dry-run   # counts only, deletes nothing
python -m app.cli prune-retention             # prune per configured windows
python -m app.cli db-stats                    # redacted DB URL, row counts, size, latest runs, signal counts
```

Hooks (both **off** by default): `ENABLE_PIPELINE_RETENTION=true` appends a `retention` stage to each baseline run; `ENABLE_WATCHER_RETENTION=true` lets the watcher loop prune at most once per day (never on every iteration). The recommended production setup is neither — install the dedicated daily timer instead (`infra/systemd/user/probability-arena-retention.{service,timer}`, not auto-installed).

### Safe EVO-X2 deployment sequence (watcher + retention)

Still read-only end to end. On EVO-X2:

```bash
cd ~/projects/probability-arena
git pull --ff-only
.venv/bin/pip install -q -r requirements-dev.txt      # if deps changed
.venv/bin/python -m app.cli run-baseline --dry-run    # applies migrations, audit-only
.venv/bin/python -m app.cli watch-once --limit 50     # one manual watcher pass
.venv/bin/python -m app.cli db-stats                  # verify tick/signal counts look sane
# install the daily retention timer BEFORE any permanent watcher:
cp infra/systemd/user/probability-arena-retention.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now probability-arena-retention.timer
# only after watch-once + db-stats validation, enable the watcher loop:
#   set ENABLE_REALTIME_WATCHER=true in .env, then
cp infra/systemd/user/probability-arena-watcher.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now probability-arena-watcher.service
```

## Outcome tracking & calibration

**Outcome sync** re-reads each market's detail payload (read-only GET, no trading permissions) and upserts one `market_outcomes` row per ticker: `outcome_status` (open/closed/settled/canceled/unknown), `winning_side` (yes/no/void/unknown), `resolved_probability` (1.0/0.0/null), settlement price, and the raw payload for audit. Parsing tolerates missing fields and API shape drift — unrecognized statuses map to `unknown`, never a crash.

**Forecast scoring** compares each forecast's `estimated_probability` against the resolved outcome: Brier score `(p − y)²`, binary log loss with an ε-clamp (`1e-6`, so p = 0/1 stays finite), and absolute error. Unresolved outcomes produce `pending_outcome` scores; canceled/void/unknown outcomes produce `unscorable`. Scoring is append-only for audit but never duplicates: a forecast is re-scored only when its outcome state changes (e.g. pending → settled).

**Calibration reports** aggregate over the latest score per forecast, grouped by evidence depth, forecast risk, forecaster, domain, and calibration tag. This is the ground-truth loop the template baseline exists for: the market-mid-anchored baseline should score close to the market's own calibration, so any future forecaster (e.g. `ENABLE_LLM_FORECASTING=true` with external research) has a measurable bar to beat — before any EV or trading conversation happens.

## Forecast engine

A forecast is a structured reasoning artifact: `estimated_probability`, `confidence`, `evidence_depth`, `forecast_risk`, a summary, `bull_case`/`bear_case`, `skeptic_notes`, `key_assumptions`, `missing_info`, `what_would_change_mind`, and `calibration_tags` — linked back to the research packet and resolution assessment it consumed, with raw forecaster output kept DB-only for audit.

**Evidence depth** is computed deterministically from the research packet: `template_only` (completeness ≤ 0.65 and no facts beyond local Kalshi metadata), `source_backed` (external facts and completeness above the ceiling), or `mixed`. **Confidence caps** are enforced in post-processing on every forecast regardless of forecaster:

| Condition | Cap (env var) |
|---|---|
| `template_only` evidence | 0.55 (`TEMPLATE_ONLY_MAX_CONFIDENCE`) |
| `source_backed`/`mixed` evidence | 0.75 (`SOURCE_BACKED_MAX_CONFIDENCE`) |
| Critical info missing (unresolved settlement source, or no/non-researchable resolution assessment) | 0.50 (`MISSING_CRITICAL_INFO_MAX_CONFIDENCE`) |

**Forecasters** (`app/services/forecasting.py`):

- `TemplateBaselineForecaster` (default) — deterministic neutral prior: anchors to the quoted market midpoint when a two-sided quote exists (public consensus as prior; tagged `anchored_to_market_mid`), otherwise 0.50. Populates all reasoning fields, including skeptic notes stating that it adds no independent information. Template-only forecasts stay at medium/high risk.
- `MockForecaster` — canned forecasts for tests.
- `LLMForecaster` — enabled only with `ENABLE_LLM_FORECASTING=true` (default **false**); requires an Anthropic credential. Consumes enrichment + resolution assessment + research packet via a structured-output Claude call (`FORECAST_MODEL_NAME`, default `claude-opus-4-8`); evidence depth, confidence caps, and risk are recomputed deterministically regardless of model output, and any failure (credentials, refusal, malformed output, API error) falls back to the template baseline, flagged `llm_error_fallback`.

An `avoid` resolution verdict forces `forecast_risk=high` at the service level. **MVP-004B does not trade, paper trade, calculate EV, or recommend positions.**

## Research packets

A research packet is the structured evidence bundle a future forecasting chain would consume: `source_queries` to run, `sources` (with type and confidence), `key_facts` (with provenance), `missing_info` gaps, a deterministic `research_completeness_score`, and a `research_risk` level. Packets link back to the scan, enrichment, and resolution assessment they were built from, and store the raw collector output for audit. **They contain no probability forecasts and no trade recommendations.**

Markets are first classified into a domain — `sports_baseball`, `sports_tennis`, `sports_soccer`, `macro`, `weather`, `politics`, `crypto`, or `general` — deterministically from ticker markers and enriched title/category/settlement-source text.

**Collectors** (`app/services/research.py`):

- `TemplateResearchCollector` (default) — deterministic, domain-templated queries and expected sources; the known settlement source becomes a high-confidence key fact; `missing_info` lists what the template cannot know without external research. Never touches the web.
- `MockResearchCollector` — canned packets for tests.
- `LLMWebResearchCollector` — enabled only with `ENABLE_EXTERNAL_RESEARCH=true` (default **false**); requires an Anthropic credential. Refines the template baseline with a Claude + web-search call (`RESEARCH_MODEL_NAME`, default `claude-opus-4-8`) and falls back to the template packet on any failure. Domain classification stays deterministic regardless.

A market whose latest resolution assessment says `avoid` still gets a packet, but the service forces `research_risk=high` no matter what the collector reports.

## Market detail enrichment

Kalshi's list endpoint omits the metadata that matters most for judging resolution quality — the settlement sources and secondary rules live on the market **detail**, **event**, and **series** endpoints. Enrichment (all read-only GETs) fetches those three levels per market, normalizes `rules_text` (primary + secondary), `settlement_source` (named sources like `ESPN (https://www.espn.com)`), title/subtitle/category, and persists them with the full raw payloads for audit.

Resolution assessment then prefers enrichment over list-level data: enriched `rules_text` replaces the sparse list rules, and a known `settlement_source` removes the `unclear_settlement_source` penalty outright (no text detection needed). Without an enrichment row, behavior falls back to list-level data unchanged — same deterministic scores as before. Live effect: sports candidates that scored a uniform 0.75 pre-enrichment assess at 1.00/low-risk once their series' named settlement sources are known.

Only eligible candidates are enriched by default (CLI batch); `POST /markets/{ticker}/enrich-details` allows ad-hoc enrichment of any known ticker.

## Resolution assessment

Answers "does this market have clear, objective settlement criteria?" before any research or forecasting effort is spent on it. Only **eligible** markets are assessed by default (the CLI batch); the POST endpoint allows ad-hoc assessment of any known ticker.

Each assessment produces: `clarity_score` (0–1), `resolution_risk` (low/medium/high/unknown), `tradeability` (researchable/needs_manual_review/avoid), `settlement_source`, `resolution_summary`, `ambiguity_flags`, `rejection_reasons`, and optional `llm_confidence` — persisted with `model_name`, `prompt_version`, and the raw judge output for audit.

**Judges** (`app/services/resolution.py`):

- `RuleBasedResolutionJudge` (default) — deterministic text heuristics. Penalizes missing/short rules text, subjective wording ("major", "significant", "expected", "likely", …), an undetectable settlement source, and multi-condition/parlay phrasing. Same input always produces the same score.
- `MockResolutionJudge` — canned results for tests.
- `LLMResolutionJudge` — enabled only with `ENABLE_LLM_RESOLUTION=true`; requires an Anthropic credential. Refines the rule-based baseline with a structured-output Claude call (`RESOLUTION_MODEL_NAME`, default `claude-opus-4-8`; `RESOLUTION_PROMPT_VERSION=v1`) and **falls back to the rule-based result** (flagged `llm_error_fallback`) on any failure, so the pipeline never hard-fails on the LLM.

Markets scoring below `MIN_CLARITY_SCORE` (default 0.70) are marked `needs_manual_review` (or `avoid` below 0.40) with a `clarity_below_min` rejection reason. The rule-based scoring is a first pass — expect sports markets to cluster around 0.75 because Kalshi's short rules sentences rarely name a settlement source.

## Candidate hygiene (eligibility gating)

Every fetched market passes a deterministic eligibility gate **before** ranking. Ineligible markets are excluded from `/markets/candidates` by default, their snapshots persist with `score = 0.0`, and every assessment (eligible or not) is written to `market_eligibility_assessments` with machine-readable `rejection_reasons` — so "why isn't market X a candidate?" is always answerable from the DB.

Thresholds (env-configurable):

| Variable | Default | Gate |
|---|---|---|
| `REQUIRE_TWO_SIDED_QUOTE` | `true` | Reject `one_sided_quote` unless both yes bid and ask exist |
| `EXCLUDE_ZERO_QUOTE_MARKETS` | `true` | Reject `no_quotes` when neither side is quoted |
| `MIN_LIQUIDITY` | `100` | Reject `liquidity_below_min` under 100 cents resting liquidity |
| `MIN_VOLUME_24H` | `25` | Reject `volume_24h_below_min` under 25 contracts/24h |
| `MAX_SPREAD` | `0.20` | Reject `spread_too_wide` over 20 cents yes bid/ask spread |
| `MIN_DAYS_TO_EXPIRATION` | `0.25` | Reject `expires_too_soon` (also `missing_expiration`) |
| `MAX_DAYS_TO_EXPIRATION` | `45` | Reject `expires_too_far` |

Multivariate/parlay-style markets (`KXMVE*`, combo titles) are flagged in `market_type_flags` and warned as `parlay_like_market`; additionally `KALSHI_MVE_FILTER=exclude` (default) filters Kalshi's auto-generated parlay flood server-side before it ever reaches the scanner.

Liquidity note: Kalshi's list endpoint no longer populates `liquidity`; when absent, the adapter derives a deterministic proxy — the notional value (cents) resting at the top of the book on both sides.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d postgres redis
cp .env.example .env
uvicorn app.main:app --reload
```

### Migrations

The app and CLI run `alembic upgrade head` automatically. To manage migrations by hand:

```bash
alembic upgrade head                       # apply
alembic downgrade 0001                     # roll back MVP-002 changes
alembic revision -m "describe change"      # new empty revision
```

### Tests

The default suite uses SQLite in-memory/tempfile and mocked HTTP — no network or services needed:

```bash
pytest
```

Live Kalshi integration tests are **skipped by default** and only run when explicitly enabled:

```bash
RUN_LIVE_TESTS=true pytest tests/test_live_kalshi.py -v
```

They hit the real public Kalshi REST API (read-only, no credentials, ~25 markets fetched) — keep them out of CI unless you want a hard dependency on Kalshi uptime.

## Optional: WebSocket orderbook snapshots

The WS service starts only when **all** of the following are set in `.env`:

```
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi-private-key.pem
KALSHI_WS_TICKERS=FED-25DEC-T4.00,CPI-26JAN-T3.0
```

It subscribes to the `orderbook_delta` channel for those tickers, maintains books in memory, and persists depth to `orderbook_snapshots` every ~30 seconds. Without credentials the API runs fine and simply skips this service.

## Schema

| Table | Purpose |
|---|---|
| `markets` | One row per observed ticker; mutable metadata (title, status, close time, rules) |
| `market_snapshots` | Point-in-time top-of-book + activity stats + ranking score per scan, plus `raw_payload` (raw Kalshi object, JSONB on Postgres) for debugging |
| `orderbook_snapshots` | Full depth (`yes_levels`/`no_levels` as `[[price_cents, qty], ...]`) from WS |
| `scanner_runs` | Audit trail of each scan: `started_at`/`finished_at`/`duration_ms`, `source` (api/cli), counts, `status`, and `error_type`/`error_message` on failure |
| `market_eligibility_assessments` | One row per market per scan: `is_eligible`, `rejection_reasons`, `warnings`, quote/spread/liquidity/volume/expiration inputs, `market_type_flags` |
| `market_resolution_assessments` | One row per assessment: judge identity (`model_name`, `prompt_version`), clarity/risk/tradeability verdict, flags, reasons, `raw_response`; `scanner_run_id` null for ad-hoc assessments |
| `market_detail_enrichments` | One row per enrichment: normalized `rules_text`/`settlement_source`/title/category plus raw market/event/series payloads for audit; `scanner_run_id` null for ad-hoc enrichments |
| `market_research_packets` | One row per packet: collector identity, domain, queries/sources/facts/gaps, completeness score, risk, raw collector output; links to scan, enrichment, and resolution assessment |
| `market_forecasts` | One row per forecast: forecaster identity, probability, capped confidence, evidence depth, risk, bull/bear/skeptic reasoning, assumptions, change triggers, calibration tags, raw output; links to scan, packet, and assessment |
| `market_outcomes` | One row per ticker (upserted): settlement status, winning side, resolved probability, settlement price, raw payload |
| `forecast_scores` | Append-only calibration scores: Brier, log loss, absolute error, status (scored/pending_outcome/unscorable), cohort tags; links to forecast and outcome |
| `pipeline_runs` | One row per baseline pipeline execution: status, timing, config, summary; a `running` row doubles as the overlap lock |
| `pipeline_stage_runs` | One row per stage per run: status, timing, item counts, error capture |
| `market_price_ticks` | One row per market per watcher pass: quotes, midpoint, spread, liquidity proxy, raw payload |
| `opportunity_signals` | Informational signals: type, review status, old/new midpoints, reason, evidence, optional forecast link |
| `watcher_runs` | One row per watcher pass: status, timing, markets/ticks/signals counts, error capture |

Schema is managed by Alembic (`alembic/versions/`); migrations run automatically at app/CLI startup.

## Ranking

`score = weighted mean of` `spread` (0.30) + `liquidity` (0.25) + `volume` (0.20) + `expiration` (0.15) + `resolution_clarity` (0.10), each component normalized to [0, 1]. Weights live in `app/services/ranking.py` (`RankingWeights`) and every snapshot stores its component breakdown in `score_components` for auditability.
