# RAW-PAYLOAD-STORAGE-001 — explicit raw-payload capture policy

**Status:** ACTIVE on EVO-X2 since 2026-08-04T04:13Z (`RAW_PAYLOAD_CAPTURE_MODE=none`). Decision and evidence in §10–§12.

Several tables store the **complete provider response** next to the normalized
columns that were extracted from it. On the 2026-08-04 backup snapshot that is
**1,582.9 MiB — about a third of the 4.4 GiB database** — and in the two largest
tables it is 95–96% of every row.

This milestone adds an explicit policy for whether that body is kept. **Storage
hygiene only**: no provider call, no normalized field, no row count, no timing
anchor, no transaction boundary, no schema migration, and **no historical row is
touched**.

---

## 1. Inventory

Measured against a decompressed copy of `backup-20260804T013626Z.db.gz`
(snapshot instant `2026-08-04T01:36Z`) — never the live database, because a
`dbstat`/full-column scan holds a read lock long enough to block the writer under
`journal_mode=delete`.

| Table.column | Rows | avg B | p50 | p90 | p95 | max | Total MiB | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `market_price_ticks.raw_payload` | 426,300 | 2,051 | 1,986 | 2,322 | 2,333 | 2,743 | **833.9** | governed |
| `market_snapshots.raw_payload` | 153,943 | 2,220 | 2,167 | 2,688 | 2,868 | 6,241 | **325.9** | governed |
| `crypto_token_discovery_events.raw_payload` | 313,445 | 749 | 743 | 1,003 | 1,162 | 11,596 | **224.0** | governed |
| `opportunity_signals.raw_payload` | 33,415 | 1,985 | 1,924 | 2,322 | 2,469 | 3,739 | **63.2** | governed |
| `market_detail_enrichments.raw_market_detail` | 12,384 | 2,052 | 1,977 | 2,343 | 2,498 | 3,739 | **24.2** | governed |
| `market_detail_enrichments.raw_series_detail` | 12,384 | 1,473 | 1,559 | 1,802 | 1,875 | 2,155 | **17.4** | governed |
| `market_detail_enrichments.raw_event_detail` | 12,384 | 553 | 520 | 627 | 631 | 1,699 | **6.5** | governed |
| `crypto_token_risk_assessments.raw_payload` | 242,527 | 165 | 155 | 213 | 213 | 279 | 38.2 | **PINNED** |
| `market_research_packets.raw_response` | 12,442 | 1,044 | 495 | 2,383 | 2,398 | 5,851 | 12.4 | **PINNED** |
| `crypto_price_ticks.raw_payload` | 150,719 | 43 | 43 | 45 | 45 | 88 | 6.2 | **PINNED** |
| `crypto_horizon_observations.raw_payload` | 43 | 1,469 | 1,305 | 2,248 | 2,265 | 2,767 | 0.1 | **PINNED** |
| `market_forecasts.raw_response` | 12,443 | 1,039 | 367 | 2,197 | 2,201 | 2,807 | 12.3 | ungoverned |
| `market_resolution_assessments.raw_response` | 12,442 | 486 | 471 | 557 | 557 | 1,285 | 5.8 | ungoverned |
| `market_outcomes.raw_payload` | 1,788 | 2,347 | 2,404 | 2,632 | 2,795 | 3,855 | 4.0 | ungoverned |
| `crypto_token_birth_events.raw_payload` | 4,416 | 813 | 788 | 979 | 1,126 | 11,596 | 3.4 | propagated sink |
| `crypto_opportunity_signals.raw_payload` | 70,654 | 44 | 44 | 45 | 45 | 47 | 3.0 | ungoverned |
| `edge_precheck_snapshots.raw_context` | 8,314 | 174 | 174 | 174 | 174 | 174 | 1.4 | ungoverned |
| `tennis_tape_score_snapshots.raw_payload` | 1,219 | 816 | 805 | 903 | 941 | 959 | 0.9 | ungoverned |
| `cross_venue_market_candidates.raw_context` | 926 | 67 | 67 | 68 | 68 | 68 | 0.1 | ungoverned |
| **TOTAL** | | | | | | | **1,582.9** | |

Governed columns total **1,495.1 MiB — 94% of all raw-payload storage.**

The ungoverned ones are listed with their reason in
`raw_payload_policy.UNGOVERNED_BY_DESIGN`, so the coverage decision is auditable
rather than an accident of where the work stopped. In short: several are already
bounded structured dicts rather than provider bodies (`edge_precheck.raw_context`
is a fixed 174-byte thresholds dict; `tennis_tape` is already `_strip_bulk`'d),
and the rest are a few MiB at a few dozen rows a day — below the line at which a
writer change earns its risk.

### Writers

| Writer | Column |
|---|---|
| `scanner.py:211` | `market_snapshots.raw_payload` |
| `watcher.py:393` | `market_price_ticks.raw_payload` |
| `watcher.py:274` | `opportunity_signals.raw_payload` |
| `tennis_watcher.py:316` | `market_price_ticks.raw_payload` |
| `enrichment.py:111-113` | `market_detail_enrichments.raw_{market,event,series}_detail` |
| `crypto_scout.py:453` | `crypto_token_discovery_events.raw_payload` |

## 2. Reader audit

Every column was traced through ORM attribute access, aliased locals, SQLAlchemy
column references, `cast`/`LIKE`/`json_extract`, raw `text()` SQL, `getattr`,
`__dict__`/`vars()` copying, Pydantic serialization, API routers, CLI, telemetry,
backup code, and Alembic. An independent agent then re-ran the audit
adversarially, tasked with **falsifying** the "no reader" claim rather than
confirming it.

**It falsified one column**, which is the finding this section exists for:

> `crypto_token_discovery_events.raw_payload` **is** read — `crypto_tape.py:342`
> copies it verbatim into `crypto_token_birth_events.raw_payload`.

Nothing reads that sink, so suppressing the source is safe *in effect*; but it is
a **propagation, not an absence**, and the first pass had it wrong. It is now
recorded in `raw_payload_policy.PROPAGATED_COLUMNS`, and under suppression the
birth event receives the envelope, which `is_suppressed()` identifies. A test
pins that the propagation stays coherent.

The audit also found a second copy site: `crypto_scout.py:173` copies the
**pinned** `crypto_price_ticks.raw_payload` into
`crypto_opportunity_signals.raw_payload`. Because the source is pinned, that copy
keeps a real body and is unaffected — which is why that column is deliberately
left ungoverned.

### Production readers (PINNED — the capture mode cannot suppress these)

| Column | Reader | What it reads |
|---|---|---|
| `crypto_token_risk_assessments.raw_payload` | `crypto_tape.extract_creator_address` / `extract_cohort_counts`, `crypto_provider_health:131`, `crypto_risk_engine:681`, `frontier_eval:446`, `provider_budget:49` | creator/deployer address, sniper/insider/bundler counts, `provider_errors`, and a **SQL `LIKE`** used to derive SolanaTracker request accounting |
| `market_research_packets.raw_response` | `baseball_forecasting:106`, `soccer_forecasting:131`, `tennis_forecasting:111` | the extracted match evidence the forecasters forecast from |
| `crypto_price_ticks.raw_payload` | `crypto_scout:267-269`, `crypto_risk_engine:388`, `crypto_tape:441` | `boosts_active` (boost delta), `dex_id` |
| `crypto_horizon_observations.raw_payload` | `crypto_horizon:1177` | the observation audit dict, re-read on retry/report |

### Governed columns

`no_reader_found` or `test_only`. Tests that assert full-payload equality still
pass, because they run under the default `full`.

**Design consequence.** The pin list is not advisory — `capture()` returns the
payload unchanged for any column in `PINNED_FULL`, whatever the mode says. The
audit could still be incomplete; this removes the consequence of that rather
than relying on the audit being perfect.

**Precisely: the pin list is inert at runtime, and the fail-closed check is what
actually protects.** No production call site passes a pinned column — the four
pinned columns are safe because their writers were never wrapped, and a test
forbids wrapping them. `PINNED_FULL` is therefore documentation-with-teeth for a
future writer, not the live guard. The live guard is that `capture()` will only
suppress a column in `GOVERNED_COLUMNS`.

**And that failed closed only after review.** The security review found
`capture()` checking only `column in PINNED_FULL`, which fails **open**: a typo
in a pin name, a table rename, or a new writer copy-pasting a pin with one wrong
character would silently drop out of the pin list and start suppressing data a
reader needs. It now requires the column to be in `GOVERNED_COLUMNS` to suppress
anything at all; an unrecognised identifier keeps the full body and warns once.
Nineteen parametrised tests pin this, and they fail if the check is reverted.

## 3. Minimum provenance contract

A suppressed column stores a bounded envelope, never NULL:

```json
{"raw_payload_suppressed": true, "mode": "none",
 "source": "kalshi_rest", "bytes": 2049, "digest": "a1b2c3d4e5f60718"}
```

~95 bytes against a ~2,050-byte body — **95% smaller**.

- `source` — provider attribution
- `bytes` — the size of what was discarded, so avoided storage is measurable
- `digest` — first 16 hex of SHA-256 over the canonically-sorted serialization.
  Enough to prove two rows saw the same body, or to match a replayed provider
  response, without spending 64 bytes per row on a hash of discarded data. It is
  **identity matching, not integrity attestation**: anyone who can write the row
  can forge a matching envelope. It is comparable only against another
  `payload_digest()` call, never against a hash of the stored text.
- everything else — request/observation timestamp, ingestion timestamp,
  normalized identifiers, price/liquidity/state, run provenance — already lives
  in the row's own normalized columns and is **not duplicated** here.

**Never stored *in the envelope*:** credentials, authorization headers, provider
keys, arbitrary exception text, unbounded HTML/JSON, environment values, or any
fragment of the payload — not even its keys. The envelope's key set is closed
and asserted.

That guarantee is about the **envelope**, not about the milestone as a whole. A
full body kept under `errors_only` is stored verbatim, and an error body is the
payload class most likely to echo the request URL back — this repo does send a
provider key in a query string (`tennis_providers.py`, `params={"APIkey": ...}`).
So error-body capture is gated behind
`raw_payload_policy.ERROR_BODY_CAPTURE_ALLOWLIST`, which is **empty**: no writer
may keep an error body until its error shape is shown not to carry request
detail, or a redaction pass exists. This is why `errors_only` currently behaves
exactly like `none` everywhere.

**Why not NULL.** NULL already means *"the provider gave us nothing"* —
`tests/test_watcher.py:86` asserts exactly that, and
`tests/test_enrichment.py:110` asserts a genuinely absent event detail. Writing
NULL on suppression would destroy a real distinction and make suppression
invisible. It would also break `market_detail_enrichments.raw_market_detail`,
which is `NOT NULL` — the envelope keeps it valid **without a migration**.

## 4. Capture modes

```env
RAW_PAYLOAD_CAPTURE_MODE=full   # DEFAULT — today's behaviour, unchanged
RAW_PAYLOAD_CAPTURE_MODE=none   # never store the body; envelope only
```

**Two modes, not three.** An `errors_only` mode was designed, implemented and
then dropped on review. A provider *error* body is the payload class most likely
to echo the request URL back, and this repo sends a provider key in a query
string (`tennis_providers.py`, `params={"APIkey": ...}`) — so no writer could be
allowed to keep one without a redaction pass. With that allowlist necessarily
empty, `errors_only` behaved identically to `none` for every column,
unconditionally. An environment value that provably cannot behave differently
from another is a misconfiguration trap, not a feature: an operator reading
`.env.example` would pick it as a safe middle ground and get full suppression.
The security reasoning is preserved as a note in the module for whoever
reintroduces error capture deliberately.

An unrecognised value **fails closed to `full`** and logs a warning. It is never
read as `none`: a typo in a host `.env` must not silently start discarding
provider bodies.

`payload is None` stays `None` in every mode.

Three further properties, each of which review found missing at first:

- **`capture()` cannot raise.** `json.dumps` raises `RecursionError` on deeply
  nested input and `default=str` re-raises whatever a hostile `__str__` raises —
  neither is a `TypeError`/`ValueError`. This runs inside a scan's write
  transaction, so an escape would abort the whole scan, and it would do so *only*
  under suppression, because `full` returns before touching json. An asymmetric
  abort surface that exists only in the mode being activated is the wrong risk to
  carry; every failure now falls back to keeping the body.
- **It is idempotent.** Re-capturing an envelope returns it unchanged.
  `RAW-PAYLOAD-RECLAMATION-001` is precisely the caller that would otherwise
  produce an envelope-of-an-envelope whose `bytes`/`digest` describe the
  envelope and destroy the original provenance.
- **It is monotone.** A body at or below `MIN_SUPPRESSIBLE_BYTES` (160) is kept,
  because the envelope costs ~118 B and suppressing a 66-byte body — which
  `crypto_scout`'s `EVENT_PAIR_SEEN` really does produce — would make the row
  *bigger*.

## 5. Writer implementation

Each writer changed by exactly one wrap:

```python
raw_payload=_capture_raw(
    market.raw, source="kalshi_rest", column="market_price_ticks.raw_payload",
),
```

No second provider request, no provider behaviour change, no normalized-field
change, no row-count change, no timing-anchor change, no MarketOps stage-order
change, no new transaction, no retry change, no payload logging, no migration.

## 6. Measurement

```bash
python -m app.cli raw-payload-storage-report --format text|json [--recent 5000]
python -m app.cli raw-payload-storage-report --full-scan   # backup copy only
```

Reports capture mode and, per governed column, how many of the **newest N rows**
carry a full body versus a suppressed envelope, and the bytes they store. Writes
nothing, calls no provider, and **never prints payload contents** — it identifies
suppressed rows by the marker key, never by inspecting payload data. There is no
`--confirm`.

**The window is the newest N rows by primary key, not a time range.** A time
range was the first design and review killed it: *none* of these tables has an
index on its timestamp column, so `WHERE created_at >= ...` is a full table scan.
Worse, in `market_price_ticks` the `created_at` column sits *after* `raw_payload`
in row order, so evaluating the predicate forces SQLite to traverse each row's
overflow chain — the "cheap" default would have read essentially the whole
834 MiB column while holding a SHARED lock under `journal_mode=delete`. A
descending primary-key `LIMIT` is index-ordered and bounded, and it answers the
activation question more directly anyway: *are the newest rows envelopes?*

PINNED columns are listed but **never queried** — the capture mode cannot touch
them, so scanning 266 MiB of them buys no decision value.

Two smaller corrections review forced: SQLAlchemy's JSON type stores Python
`None` as the JSON literal `null`, **not** SQL NULL, so an `IS NOT NULL` filter
alone counted genuinely-absent payloads as present and the operator's primary
verification signal would never have reached its floor. And the report does not
call `run_migrations()`, unlike most commands here, so it can be pointed at a
backup snapshot without mutating the evidence.

**The window is the default on purpose.** Whole-table totals require a full scan
of a multi-hundred-MiB JSON column, and a long read lock under
`journal_mode=delete` blocks the concurrent writer's `COMMIT` — the hazard
`SQLITE-BACKUP-COORDINATION-001` exists to avoid, and the same trap
`retention-coverage-report --no-dbstat` sidesteps. `--full-scan` is for a
decompressed backup copy, not the live database.

**Honest caveat, found in review:** the window columns are **not** indexed —
`created_at` carries no index on `market_price_ticks`, `opportunity_signals`,
`crypto_token_discovery_events` or `market_detail_enrichments`, and
`market_snapshots` has no `created_at` at all (the report falls back to
`captured_at`, which exists only as the trailing column of a composite index and
cannot serve a leading range predicate). So even the default invocation scans
those tables. Two things bound the damage: `app/db.py` sets no `isolation_level`,
so pysqlite runs each SELECT in autocommit and the shared lock is held
per-statement rather than across the whole report; and the report is an
on-demand operator command, not a scheduled one. Run it off-peak, outside the
01:25–01:45 UTC backup window, or against a backup copy. The report also does
**not** call `run_migrations()`, unlike most commands in this repo, precisely so
it can be pointed at a snapshot safely.

## 7. Projected effect

At `none`, per-day writes avoided (governed columns only, from measured rates):

| Column | Rows/day | avg B | MiB/day avoided | Net growth? |
|---|---:|---:|---:|---|
| `market_price_ticks.raw_payload` | 207,450 | 2,051 | **405.9** | no — 2-day retention, churn |
| `market_snapshots.raw_payload` | 4,962 | 2,220 | **10.5** | **yes** |
| `crypto_token_discovery_events.raw_payload` | 11,573 | 749 | **8.3** | **yes** |
| `opportunity_signals.raw_payload` | 1,531 | 1,985 | **2.9** | **yes** |
| `market_detail_enrichments.*` (3 cols) | 487 | 4,078 | **1.9** | **yes** |
| envelope written back | 226,977 | ~118 | −25.6 | (−2.2 of it on growth tables) |
| **Net** | | | **≈404 MiB/day less written**, of which **≈21.4 MiB/day is net growth** | |

Against measured net growth of ~70 MiB/day, that is a **~31% reduction in net
database growth**, plus ~400 MiB/day less write churn (which also shrinks the
nightly backup and its duration).

*(Corrected on review: the envelope is 118 B as SQLAlchemy actually stores it,
not the 95 B of a compact `json.dumps`; the envelope row count must include the
three enrichment columns separately; and the earlier draft subtracted envelope
cost from the churn figure but not from the growth figure. 31% rather than 33% —
still the largest lever available, but a document whose stated virtue is honesty
should not have one line net and the next line gross.)*

The ~834 MiB currently held live by `market_price_ticks` would drain out of the
working set within its 2-day window, becoming reusable free pages.

**This does not shrink the file.** See §9.

## 8. Compatibility

Default `full` preserves behaviour exactly — a disposable-database comparison
shows it produces a byte-identical file to no policy at all. The suite is green
in **both** modes (2,370 passed under `full`, 2,370 under `none`), which matters
more than green-under-default alone: without a green `none` baseline, a real
activation regression would be indistinguishable from pre-existing noise. Getting
there required pinning the capture mode in the two product tests that assert
full-payload equality through a real writer (`test_enrichment.py`,
`test_models.py`) and decoupling this milestone's own `test_default_is_full` from
ambient env, which would otherwise fail forever on an activated host.

The suite carries one pre-existing order/load-dependent flake
(`test_meme_news.py::test_velocity_scoring_from_previous_snapshot`) that also
fails on unmodified `main` under CPU contention and passes in isolation. It is
not caused by this change. API
outputs are unchanged (12 of the 13 governed/ungoverned columns have no field on
any Out schema at all; the 13th is `exclude=True`). Normalized columns are proven
identical across all three modes. Backup verification, retention reporting,
candidate readiness, anchor feed, forecast scorability and reliability are
untouched. Zero provider-call delta; no second scan; no cohort or observation
action.

## 9. Logical versus physical

Suppression avoids **future** writes. It deletes nothing and it does **not**
shrink the SQLite file — the file length is a high-water mark. Existing payloads
stay exactly where they are.

- Historical reclamation → **RAW-PAYLOAD-RECLAMATION-001** (§13)
- File compaction → **SQLITE-COMPACT-COPY-001** (§13)

**The `db_growth_warning` alert stays critical after activation.** It measures
the file, and the file does not shrink. Only `SQLITE-COMPACT-COPY-001` can clear
it. An operator who remembers "31% reduction" and nothing else will expect the
4.4 GiB alert to improve; it will not.

## 10. Activation decision

```text
ACTIVATE RAW_PAYLOAD_CAPTURE_MODE=none
```

Justified on the reader audit, **not** on the size gate:

- No production reader requires any governed payload. Audited three times, once
  adversarially with the explicit goal of falsifying the claim; the one reader it
  found (§2) is a propagation into a sink nothing reads, and is recorded.
- Normalized fields plus the bounded envelope are sufficient for incident work:
  every value any consumer actually uses lives in its own column, and the
  envelope keeps source, discarded size and a digest.
- Parity is established, not asserted — the suite is green in **both** modes, a
  disposable-database comparison shows `full` is byte-identical to no policy,
  and end-to-end writer runs show identical normalized columns and row counts.
- Rollback is one `.env` line plus a watcher restart (§14), with no schema, no
  timer and no historical mutation to undo.
- `errors_only` was not a candidate: it was dropped as provably dead
  configuration (§4).

## 11. Deployment

Merged as `eb264b1`, EVO-X2 fast-forwarded `03db6a8 -> eb264b1`. Diff audited
first: **0 files** under `alembic/`, `app/models.py`, `app/db.py` or `infra/`.

### Dark deployment — PASS

Capture mode left at its default (`.env` key absent, effective `full`).
MarketOps not restarted. Natural cycle **#7520**:

```text
status=ok  stage_errors={}   exactly one crypto_scan
anchor_feed ok ext=0 | readiness ext=0 | backup_freshness healthy ext=0
db_growth critical/updated       Alembic 0027
market_snapshots suppressed rows: 0     <- full behaviour unchanged
```

### Activation — 2026-08-04T04:13Z

| Proof | Value |
|---|---|
| `.env` SHA-256 before | `16e57439ba5e08a8d86b98a2f8592b5d3d475eac87c2439317b17f13310fc694` |
| `.env` SHA-256 after | `041394bba799f0de48452fcb66a886a29937b93d3f4eeef5f60f4ed5bfeeb3a4` |
| Lines | 162 -> 165 (blank + comment + setting) |
| Assignments | 86 -> 87 |
| Keys differing | exactly one: `RAW_PAYLOAD_CAPTURE_MODE` |

No unrelated value read or echoed; `.env` is gitignored and not committed.
MarketOps was **not** restarted.

**The watcher WAS restarted** (`probability-arena-watcher.service`, up since
2026-07-04, PID 1919798 -> 3426405, first new run #44021 `ok`, 150 markets). That
is required, not optional — §14 — and `EVO_X2_RUNBOOK.md:66` prescribes exactly
this for a flag that affects the watcher. Without it the authorized change would
have been inert for 99% of its target. No MarketOps cycle was forced.

## 12. First natural active cycle and initial growth comparison

Cycle **#7521**, `2026-08-04 04:18Z`, naturally scheduled:

```text
status=ok  stage_errors={}  duration 36,927 ms
stages: promote/process/crypto_scan/sync/score/edge_precheck/champion = ok
anchor_feed ok ext=0 | readiness ext=0 | backup_freshness healthy ext=0
db_growth critical/updated       Alembic 0027
```

Capture state by writer (bounded metadata only — no payload was printed):

| Column | Rows since activation | Suppressed | Note |
|---|---:|---:|---|
| `market_price_ticks.raw_payload` | 750 | **750** | 118 B vs 2,051 B — **94.2%** |
| `opportunity_signals.raw_payload` | 1 | **1** | |
| `crypto_token_discovery_events.raw_payload` | 56 | **50** | the 6 kept are `EVENT_PAIR_SEEN`'s ~66 B bodies — the monotone guard working as designed |
| `market_snapshots.raw_payload` | 0 | 0 | its writer is the **4-hourly baseline timer**, which last ran 10 min *before* activation |
| `market_detail_enrichments.raw_*` | 0 | 0 | same writer |

### Initial comparison (bounded — 5 min 8 s, not a trend)

| Column | Suppressed | Stored | Would have stored | Avoided |
|---|---:|---:|---:|---:|
| `market_price_ticks` | 750 | 88,500 B | 1,538,250 B | 1.38 MiB |
| `crypto_token_discovery_events` | 50 | 5,851 B | 37,450 B | 0.03 MiB |
| `opportunity_signals` | 1 | 118 B | 1,985 B | ~0 |
| **Total** | | | | **1.41 MiB in 5m08s** |

Extrapolating that rate alone gives ~395 MiB/day, close to the ~404 MiB/day
projection in §7 — but it is five minutes of one writer and is stated as a
consistency check, **not** a measured daily rate. `market_snapshots` and the
enrichment columns, which carry the *net growth* reduction, have not yet had a
writer run.

| Check | Result |
|---|---|
| MarketOps duration | 40,577 ms avg (10 cycles before) -> 36,927 ms (after) — no regression |
| `database_locked` events | **4** — unchanged |
| Provider calls | unchanged; all local hooks `external_calls=0` |
| MarketOps runs not ok | 0 |
| Watcher runs not ok | 0 (6 runs since activation) |
| Alembic | 0027 |
| Report cost on live DB | **0.60 s** (`--recent 2000`) |

---

## 13. Reclamation and compaction boundary

Neither is executed here.

**RAW-PAYLOAD-RECLAMATION-001** must separately address: exactly which historical
rows are eligible (governed columns only, never pinned); a fresh verified backup
as a prerequisite; batched updates sized against writer contention; explicit
transaction bounds; rollback evidence; the fact that freed space becomes
freelist pages and the file does not shrink; and post-verification.

**SQLITE-COMPACT-COPY-001** must remain separate, and should follow reclamation
rather than precede it — compacting first would simply re-compact ~1.5 GiB of
unread payload. Preferred direction: `VACUUM INTO` a new file on a separate
volume → verify → maintenance-window swap under explicit approval.

## 14. Rollback — and the restart it requires

```env
RAW_PAYLOAD_CAPTURE_MODE=full
```

```bash
systemctl --user restart probability-arena-watcher.service
```

**The restart is not optional, and an earlier draft of this section was wrong to
say otherwise.** `get_settings()` is `@lru_cache`d and nothing calls
`cache_clear()`, so a long-lived process keeps the mode it started with.
`probability-arena-watcher.service` runs a continuous loop
(`EVO_X2_RUNBOOK.md:15`), and it is the sole writer of
`market_price_ticks.raw_payload` — 405.9 of the 404 MiB/day, i.e. essentially
all of the benefit. The oneshot MarketOps/scanner/enrichment timers do pick up
`.env` on their next run, so **without the restart an operator sees a partial
effect in both directions**: on rollback the report would print
`capture_mode=full` while the watcher kept suppressing. `EVO_X2_RUNBOOK.md:66`
already states this rule for flags generally; it applies here.

This is why the report prints the caveat that its `capture_mode` is the
*reporting process's* mode, not the watcher's.

Beyond that, nothing needs undoing: rows written while suppressed keep their
envelope (valid, bounded, self-describing, and distinguishable from both a real
body and a genuine NULL), no schema changed, no timer exists, and no historical
row was modified. Reverting the code has the same effect as reverting the flag.

**A mixed table is expected and fine.** After activation a governed column holds
older full bodies and newer envelopes; after rollback, envelopes then bodies
again. `is_suppressed()` distinguishes them structurally, and the report counts
each separately.
