# Probability Arena

**Kalshi read-only market intelligence** (MVP-004B: gating + enrichment + resolution assessment + research packets + capped-confidence forecasts).

Scans active Kalshi markets over the public REST API, ranks them on tradability signals (spread, liquidity, volume, time to expiration, resolution clarity), and stores time-series snapshots in Postgres. Optionally maintains live orderbook snapshots over WebSocket when API credentials are configured.

## Safety notes

- **Read-only by design. No order placement exists.** There is no trading, betting, order placement, wallet, execution, portfolio-sizing, or paper-trading code anywhere in this repo — the REST adapter only issues GETs (market list, market/event/series detail), the WebSocket client only sends channel subscriptions, and the CLI commands (`scan`, `enrich-details`, `assess-resolution`, `collect-research`) only read market data and write to our own database.
- **Forecasts are probabilities and reasoning artifacts only.** MVP-004B adds structured probability forecasts, and stops there: no EV calculation, no position sizing, no paper trading, no trade recommendations, no execution. The forecast schema deliberately has no trade/EV/sizing fields, and tests assert the absence of trading language in forecast output.
- **LLM resolution judgment is OFF by default** (`ENABLE_LLM_RESOLUTION=false`). The deterministic rule-based judge needs no credentials or network beyond Kalshi; tests never call an LLM. When enabled, the LLM only *reads* rules text and returns a structured quality verdict — it has no tools and no trading capability.
- Public market data requires **no credentials**. The Kalshi API key is only needed for the optional WebSocket orderbook feed, and even then the client only sends channel subscriptions.
- Keep your Kalshi private key **outside the repo** (it is `.gitignore`d by extension, but store it elsewhere, e.g. `~/.kalshi/`). Never commit `.env`.
- The `resolution_clarity` ranking component is a **placeholder** (constant 0.5). Do not treat scores as trading advice; they measure market microstructure quality, not edge.
- Respect Kalshi's [API terms and rate limits](https://trading-api.readme.io/). The scanner caps fetches via `SCANNER_MAX_MARKETS` and results are cached in Redis for `CANDIDATES_CACHE_TTL_SECONDS`.

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

Runs migrations, fetches up to `--limit` open markets, assesses eligibility, ranks the eligible ones, persists a `scanner_runs` audit row (with `source=cli`, `duration_ms`, and error details on failure) plus per-market snapshots and eligibility assessments, then prints the top 20 and a rejection-reason summary.

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

**Recommended sequence:** `scan` → `enrich-details` → `assess-resolution` → `collect-research` → `forecast`. Each stage automatically prefers the previous stage's output when it exists.

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

Schema is managed by Alembic (`alembic/versions/`); migrations run automatically at app/CLI startup.

## Ranking

`score = weighted mean of` `spread` (0.30) + `liquidity` (0.25) + `volume` (0.20) + `expiration` (0.15) + `resolution_clarity` (0.10), each component normalized to [0, 1]. Weights live in `app/services/ranking.py` (`RankingWeights`) and every snapshot stores its component breakdown in `score_components` for auditability.
