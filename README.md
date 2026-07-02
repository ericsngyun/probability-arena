# Probability Arena

**Kalshi read-only market intelligence** (MVP-002: migration-safe, live-integration-ready).

Scans active Kalshi markets over the public REST API, ranks them on tradability signals (spread, liquidity, volume, time to expiration, resolution clarity), and stores time-series snapshots in Postgres. Optionally maintains live orderbook snapshots over WebSocket when API credentials are configured.

## Safety notes

- **Read-only by design. No order placement exists.** There is no trading, betting, order placement, wallet, or execution code anywhere in this repo — the REST adapter only issues GETs, the WebSocket client only sends channel subscriptions, and the CLI's only command is `scan`. None should be added under MVP-001/002.
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

Schema is managed by Alembic (`alembic/versions/`); migrations run automatically at app/CLI startup.

## Ranking

`score = weighted mean of` `spread` (0.30) + `liquidity` (0.25) + `volume` (0.20) + `expiration` (0.15) + `resolution_clarity` (0.10), each component normalized to [0, 1]. Weights live in `app/services/ranking.py` (`RankingWeights`) and every snapshot stores its component breakdown in `score_components` for auditability.
