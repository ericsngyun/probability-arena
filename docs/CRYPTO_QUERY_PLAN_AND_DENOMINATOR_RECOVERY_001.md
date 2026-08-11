# CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001

Status: **MEASUREMENT, ONE GATED MAINTENANCE ACTION, NOTHING ACTIVATED.**
Nothing was merged, pushed, deployed or migrated. `enable_crypto_tape_reconciler`
is still default OFF, no reconciler timer was installed, no `.env` and no
systemd unit was touched, and `worktree/crypto-coverage-repair` is still not on
`main`. The only production mutation in this document is B8's explicit,
operator-controlled `ANALYZE` on the live EVO database, which writes exactly one
table (`sqlite_stat1`) and changes no schema, no row of application data, and no
Alembic revision (still **0027**).

Branch: `worktree/crypto-coverage-repair` (measured at `b0d4af5`).
Measured: 2026-08-11 on EVO-X2, against **throwaway online copies** of the
production database taken with `sqlite3.Connection.backup()` — the same API
`app/services/backup.py` uses — from a **read-only** source connection
(4 550 623 232 bytes copied in **2.41 s** with the live watcher running).
Scratch directory `/mnt/data/cqp-bench/`, deleted after use.

Harnesses, all committed with this document so every number is re-derivable:

| script | what it answers |
|---|---|
| `scripts/crypto_query_plan_audit.py` | B1 + B2 — real statements, real params, `EXPLAIN QUERY PLAN`, latency, before/after `ANALYZE` |
| `scripts/crypto_query_plan_mechanism.py` | B1 — which individual `sqlite_stat1` row flips each plan |
| `scripts/crypto_reconcile_coexistence_bench.py` | B3 — two real processes, realistic pacing, measured writer wait |
| `scripts/crypto_backlog_partition.py` | B4 + B5 + B7 — arrivals, typed backlog partition, retention model |
| `scripts/sqlite_analyze_maintenance.py` | B8 — the gated, operator-controlled live `ANALYZE` with evidence capture |

Raw JSON for every number below is committed under
`docs/evidence/crypto-query-plan-and-denominator-recovery-001/`. Where a file
has a `_v2` suffix it is a **re-run with a corrected harness**, and the section
that uses it says what was corrected and why — `mechanism_v2.json` (V1 variant
was testing an empty row set; selection query was bound to the wrong cutoff),
`backlog_partition_v2.json` (adds the §5.3 observation-coverage block, which
had no committed harness at all), and `coexist_*_probe_v2.json` (the arrival
sampler was coupled to the system it measured, biasing wait downward). The
superseded originals are kept where they still carry information —
`coexist_{before,after}_probe.json` is the only measurement taken on a
genuinely un-analysed copy of production, which after B8 can never be taken
again.

---

## 0. Headline

| | |
|---|---|
| Baseline confirmed | live DB has **no `sqlite_stat*` table at all** — `ANALYZE` has never run |
| B1 mechanism | three `_load_sources` queries pick `ix_*_chain`, an index whose **every** row is `'solana'`. The responsible statistic is one row per index in `sqlite_stat1` |
| B1 effect | `_load_sources` over 40 real tokens: **5.556 s → 0.078 s (71×)** |
| B1 correction | the temp B-tree sort **does not disappear** — it survives every variant. Only the row count it sorts changes |
| B2 verdict | **zero material regressions.** 25 hot paths measured; 6 statements changed plan — 3 improved by 4.9×–2 176×, 3 moved by under 0.2 ms; the probes that got slower moved 1.8–7.4 % on plans that are byte-identical or whose only change is worth 0.015 ms |
| B3 verdict | **PASS.** 0 lock failures and 0 retries in every realistic arm. Realistic-arrival writer wait p95 **0.53–0.63 s → 0.018–0.053 s** (12–34×). Worst-case hold is a storage-layer tail present in BOTH arms and is not improved |
| B4 arrivals | p95 **425/day** (recent 14 complete days), planning rate 530/day |
| B4 safe capacity | **120 → 1 180 tokens/pass** (conservative measured minimum, live competing writer); **480 → 4 720/day**; 0.91× of the planning rate → **8.9× margin** |
| B5 backlog | 11 926 tokens: **78.2 % RETENTION_LOST**, 13.3 % MISSING_REQUIRED_INITIAL_STATE, 6.6 % PARTIALLY_RECOVERABLE, 1.6 % no-evidence, **0.39 % (47 tokens) RECOVERABLE_NOW** |
| B5 the real finding | the observation lane's median **last** tick is **83 minutes** after birth; in a cohort where **nothing has been pruned**, 24 h tick coverage is **4.0 %**. The 24 h evidence is not being deleted — it was never recorded |
| B6 | the shipped frontier-first policy services arrivals with **7.5× margin** and leaves a **6.5-pass** frontier slack. **Not** INSUFFICIENT |
| B7 | recommend **7 d → 14 d**, LOW priority: +35.1 MiB absorbed by the existing 1.72 GB freelist, **file size change 0**; it buys outage tolerance, **not** denominator |
| B8 | live `ANALYZE` **EXECUTED** under gate: **0.4597 s**, 130 stat rows, **Δ file bytes 0**, `integrity_check ok`, Alembic still 0027, **0 new lock events**. One production unit **did fail** inside the change window — `tick_aggregation` at 08:04, on a pre-existing MarketOps race that also fired on 2026-08-08 and that this branch already fixes (§8.5). It recovered unaided on its next firing with **identical write-lock behaviour** against the new statistics (§8.6) |

---

## 1. B1 — the mechanism, not just the benchmark

### 1.1 Confirmed starting state

`SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'` returns **zero
rows** on the live database and on the copy. The task's baseline is confirmed:
the statistics tables do not merely sit empty, **they do not exist**. Every
`chain` column in the crypto lane has exactly one distinct value:

| table | rows | `chain` values |
|---|---|---|
| `crypto_token_discovery_events` | 396 669 | `solana` × 396 669 |
| `crypto_token_risk_assessments` | 311 214 | `solana` × 311 214 |
| `crypto_price_ticks` | 142 253 | `solana` × 142 253 |
| `crypto_pairs` | 26 736 | `solana` × 26 736 |
| `crypto_tokens` | 12 847 | `solana` × 12 847 |

### 1.2 Plans and latency, before vs after (same copy, same file, same cache)

Captured by driving the REAL `_load_sources` / `_universe` / backlog functions
and hooking SQLAlchemy for the statement text and its bound parameters, then
running `EXPLAIN QUERY PLAN` on each. 40 real tokens, 3 repetitions.

| statement | before: plan | before p50 | after: plan | after p50 | ratio |
|---|---|---|---|---|---|
| `crypto_token_discovery_events` | `SEARCH … USING INDEX ix_…_chain (chain=?)` + `USE TEMP B-TREE FOR ORDER BY` | **70.29 ms** | `SEARCH … USING INDEX ix_…_token_address (token_address=?)` + `USE TEMP B-TREE FOR ORDER BY` | **0.029 ms** | **2 431×** |
| `crypto_token_risk_assessments` | `ix_…_chain` + temp B-tree | **44.06 ms** | `ix_…_token_address` + temp B-tree | **0.016 ms** | **2 720×** |
| `crypto_price_ticks` | `ix_…_chain` + temp B-tree | **13.71 ms** | `ix_…_token_address` + temp B-tree | **0.010 ms** | **1 413×** |
| `crypto_pairs` | `ix_crypto_pairs_chain` | 2.5–4.8 ms † | `ix_crypto_pairs_base_token_address` | ~0.01 ms | ~300× |
| `meme_attention_snapshots` | `ix_meme_attention_token` | 0.020 ms | unchanged | 0.011 ms | 1.8× |
| `meme_catalyst_events` | `ix_meme_catalyst_subject_ref` | 0.012 ms | unchanged | 0.008 ms | 1.5× |

† **Methodological note, stated because it changes how one of these rows must
be read.** The per-statement timer closes when `cursor.execute()` returns. For
a query that needs a sorter (`USE TEMP B-TREE FOR ORDER BY`) or an aggregate,
that is the entire cost — the sorter must be fully materialised before the
first row is produced, so the three rows above that carry a temp B-tree are
exact.
For `crypto_pairs`, which needs no sorter, SQLite streams and the scan cost
lands in `fetch`, *after* the timer closed; the hook reported 0.019 ms while a
direct raw-`sqlite3` timing of the same statement on the same file measured
**2.5–4.8 ms**. The direct figure is the one in the table. **The probe-level
wall time is unaffected and is the authoritative headline number**, because it
brackets everything including fetch.

Probe-level wall for `_load_sources` over 40 tokens:

| arm | p50 | min | max |
|---|---|---|---|
| before, run 1 | **5.556 s** | 5.399 s | 5.567 s |
| before, run 2 (warm-cache control) | **5.345 s** | 5.244 s | 5.351 s |
| after `ANALYZE` (0.55 s to build) | **0.078 s** | 0.049 s | 0.086 s |

**Page-cache confound ruled out again, independently.** The second baseline run
was executed immediately after the first on the identical file, so every page
it touches was already resident. It came in **3.8 % faster** — not 71× faster.
The improvement is the plan.

### 1.3 Which statistic is responsible — isolated, one row at a time

`ANALYZE` writes 130 `sqlite_stat1` rows in one action, so "`ANALYZE` fixed it"
is compatible with any of them being the cause. `scripts/crypto_query_plan_mechanism.py`
therefore writes `sqlite_stat1` **by hand**, one subset at a time, re-opening
the connection between subsets (statistics are loaded with the schema, so a
fresh connection is what makes a hand-written row take effect).

`sqlite_stat1` rows for the three expensive tables, as `ANALYZE` writes them —
`stat` is `"<rows in index> <average rows per distinct value of column 1> …"`:

| tbl | idx | stat |
|---|---|---|
| `crypto_token_discovery_events` | `ix_…_chain` | `396669 396669` |
| `crypto_token_discovery_events` | `ix_…_token_address` | `396669 31` |
| `crypto_token_risk_assessments` | `ix_…_chain` | `311214 311214` |
| `crypto_token_risk_assessments` | `ix_…_token_address` | `311214 25` |
| `crypto_price_ticks` | `ix_…_chain` | `142253 142253` |
| `crypto_price_ticks` | `ix_…_token_address` | `142253 44` |
| `crypto_pairs` | `ix_crypto_pairs_chain` | `26736 26736` |
| `crypto_pairs` | `ix_crypto_pairs_base_token_address` | `26736 3` |

Variant results (median of 5 real tokens; quiet host; the plan column is
deterministic and is the load-bearing part):

| variant — which `sqlite_stat1` rows exist | discovery events | risk assessments | price ticks | pairs |
|---|---|---|---|---|
| **V0** none at all | `ix_…_chain` — 69.61 ms | `ix_…_chain` — 43.68 ms | `ix_…_chain` — 14.63 ms | `ix_…_chain` — 2.77 ms |
| **V1** a **synthetic** table row only (`idx IS NULL`, the row-count estimate) | `ix_…_chain` — 67.89 ms | `ix_…_chain` — 42.11 ms | **`ix_…_token_address` — 0.006 ms** | `ix_…_chain` — 2.38 ms |
| **V2** the **bad** index's row only (`ix_…_chain`) | **`ix_…_token_address` — 0.038 ms** | **`ix_…_token_address` — 0.022 ms** | **`ix_…_token_address` — 0.004 ms** | **`ix_…_base_token_address` — 0.010 ms** |
| **V3** the **good** index's row only (`ix_…_token_address`) | `ix_…_chain` — 66.54 ms | `ix_…_chain` — 41.46 ms | `ix_…_chain` — 14.41 ms | **`ix_…_base_token_address` — 0.008 ms** |
| **V4** both index rows | `ix_…_token_address` — 0.038 ms | `ix_…_token_address` — 0.064 ms | `ix_…_token_address` — 0.006 ms | `ix_…_base_token_address` — 0.008 ms |
| **V5** full `ANALYZE` of this table | 0.037 ms | 0.023 ms | 0.004 ms | 0.008 ms |
| **V6** full `ANALYZE` of everything | 0.037 ms | 0.022 ms | 0.005 ms | 0.008 ms |

**The responsible statistic is the row for the WRONG index, not the right one.**
V2 — a single row saying `ix_crypto_token_discovery_events_chain` returns
396 669 rows per lookup — is sufficient on its own to flip all four plans. V3 —
the row saying `ix_…_token_address` returns 31 — is **not** sufficient for the
three large tables: they stay on the `chain` index and stay slow.

The reason is exact and worth writing down, because it is the general rule for
this whole class of defect: with no `sqlite_stat1` row, SQLite falls back to a
fixed default estimate of roughly **10 rows** for an indexed equality
constraint. So:

* telling SQLite the good index returns **31** rows makes it look *worse* than
  the default it already assumed for the bad one (31 > 10) → no change (V3, the
  three large tables);
* telling SQLite the bad index returns **396 669** rows makes it look
  catastrophically worse than the default assumed for the good one → immediate
  switch (V2);
* `crypto_pairs` is the exception that proves the rule: its good index returns
  **3** rows, which beats the default 10, so V3 flips it too.

**V1 needed a correction and it changed the answer.** The first build of this
harness selected V1 by filtering `ANALYZE`'s own output for `idx IS NULL` —
but SQLite only writes such a row for a table with **no** indexes, so the filter
selected nothing and V1 was silently a duplicate of V0. The row is now
**synthesised** (`(tbl, NULL, "<row count>")`), which is what the variant always
claimed to test. The corrected result: the table's row-count estimate alone
does nothing on three of the four queries, but it **is** sufficient on
`crypto_price_ticks` (14.63 ms → 0.006 ms). Knowing a table holds 142 253 rows
is enough for SQLite to stop believing that walking one of its indexes end to
end is cheap, on that table's cost profile but not the others'. The general
rule stands — the `ix_*_chain` row is the reliable trigger, and it is the only
one that works on every query here — but "the table row does nothing" would
have been false, and was asserted from an empty test before this was fixed.

This is not "ANALYZE helps"; it is one specific row per table, and the same
defect exists anywhere a low-cardinality column carries its own index — which
is why B2 found the identical pattern on `crypto_opportunity_signals`.

**Methodology limit, stated because it differs from §1.2.** Unlike §1.2, which
captures statements from the running application, this variant matrix and §1.6
run **hand-written SQL** that mirrors `_load_sources` / `unreconciled_backlog`
in predicate and `ORDER BY` shape with `SELECT *` in place of the ORM's column
list. Index selection does not depend on the projection, and the V0 plans and
latencies reproduce §1.2's un-analysed measurements closely (69.6 vs 70.3 ms;
43.7 vs 44.1 ms; 14.6 vs 13.7 ms), which is the check that the substitution is
harmless. It is still a substitution, and §1.2 — not this table — is the
authoritative before/after.

### 1.4 The temp B-tree does NOT disappear — correction to the prior claim

The prior write-up expected the `USE TEMP B-TREE FOR ORDER BY` to vanish. **It
does not, in any variant.** It is present in the plan before `ANALYZE`, after
`ANALYZE`, and with hand-written statistics. That is correct behaviour and no
statistic can change it: the queries order by `(observed_at, id)` /
`(created_at, id)` and **no index in the schema provides that ordering** —
`ix_*_token_address` is a single-column index on a different column. A sorter
is structurally required.

What changes is not whether SQLite sorts, but **how many rows it feeds the
sorter**: 396 669 index entries before, ~31 after. The 2 431× is a
rows-into-the-sorter effect, not a sorter-elimination effect. Anyone who
removes the temp B-tree from the acceptance criteria of a future composite-index
proposal will be measuring the wrong thing; a composite `(chain, token_address,
observed_at)` index *would* eliminate it, and that is the only thing that would.

### 1.5 Rows scanned

SQLite's `scanstatus` API is not reachable from Python, so rows-scanned is
**derived from the plan plus a real `COUNT`**, and labelled as such:

| statement | before, entries walked | after, entries walked |
|---|---|---|
| discovery events | 396 669 (every row has `chain='solana'`) | 31 (mean rows per `token_address`) |
| risk assessments | 311 214 | 25 |
| price ticks | 142 253 | 44 |
| pairs | 26 736 | 3 |

Sum per token: **877 000 index entries walked before, ~103 after** — a factor
of 8 500 in work, which is the honest explanation for a 71× wall-clock win
(the remainder is Python/ORM overhead that no plan change touches).

### 1.6 Selection queries move the OTHER way — reported, not buried

Three of the six reconciliation probes got *slower* after `ANALYZE`:

| probe | before | after | plan change |
|---|---|---|---|
| `_universe` | 4.26 ms | **7.63 ms** | `ix_crypto_tokens_chain` → `ix_crypto_tokens_chain_address` |
| `unreconciled_backlog` | 10.97 ms | **23.48 ms** | same |
| `universe_size` / `backlog_size` / `oldest_unreconciled` | 2.01 / 5.24 / 5.11 ms | 1.48 / 5.21 / 5.09 ms | `ix_crypto_tokens_chain` → `SCAN crypto_tokens` |

Both indexes are equally useless for `chain=?` (`12847 12847` either way); the
planner swaps to the two-column one and pays for the wider index. Summed over
the six selection statements it is **+15.9 ms per pass** on the statement basis
(+3.37 ms on `_universe`, +12.51 ms on `unreconciled_backlog`, −0.6 ms across
the three that improved), or **+13.9 ms** on the authoritative probe-wall
basis. It is **1 pass in 4 per day**, against a per-token path
that runs 1 285 times per pass and got 71× faster: the pass-level result is in
§3 (135 → 1 300 tokens in the same 20 s). It is recorded here rather than
averaged away, and it is the one place a future composite index would also pay
for itself.

The mechanism harness attributes it precisely. On `unreconciled_backlog`'s
selection query, bound to the same `now − 48 h` cutoff the real function uses
(the first build of this harness bound `max(first_seen_at)` instead, which
selected every token in the table and produced timings that could not be
compared with §1.2 at all — corrected here), median of 5:

| variant | plan for `crypto_tokens` | median |
|---|---|---|
| V0 no statistics | `ix_crypto_tokens_chain` | 9.48 ms |
| V2 `ix_crypto_tokens_chain` row only | `ix_crypto_tokens_chain_address` | **21.50 ms** |
| V2b `ix_crypto_tokens_chain_address` row only | `ix_crypto_tokens_chain` | 9.42 ms |
| V4 both `crypto_tokens` index rows | `SCAN crypto_tokens` | **9.07 ms** |
| V6 full `ANALYZE` (adds the outcome-table statistics) | `ix_crypto_tokens_chain_address` | **21.42 ms** |

So the regression is caused by the `ix_crypto_tokens_chain` statistic — V2
alone reproduces it — and full `ANALYZE` does not rescue it. The interesting
row is V4: with **both** `crypto_tokens` index statistics and none for
`crypto_token_survival_outcomes`, SQLite picks the best plan of all (`SCAN`,
9.07 ms). That is a genuinely useful pointer for whoever revisits this query —
the fix is not a new index, it is that `chain = ?` should not be in the
predicate at all on a single-chain deployment.

---

## 2. B2 — hot-query regression audit (the gate that protects everything else)

25 representative production read paths, each driven through the **real
application function** (never a retyped SQL string), 2 repetitions per arm,
on one production copy measured before, again warm before, then after
`ANALYZE` on the same file. `run_once`-style entry points were deliberately not
used — they write and call out to the network; their constituent selectors were
driven instead, plus the documented read-only `*_report` / `*_coverage` /
`dry_run=True` composites.

**Classification rule, fixed before looking at results:** a probe is REGRESSED
only if its plan actually changed AND it got materially slower. A probe whose
plan is byte-identical before and after cannot have been affected by `ANALYZE`
— identical plan, identical data, so any latency delta is measurement noise, and
saying otherwise would be attributing I/O jitter to the planner.

| probe (production cadence) | before | before-warm | after | Δ | plans changed | class |
|---|---|---|---|---|---|---|
| `marketops.active_run` (5 min) | 34.4 ms | 22.5 ms | 19.2 ms | −14.9 % | 0/1 | UNCHANGED |
| `marketops.eligible_signals` (5 min) | 67.6 | 30.5 | 27.6 | −9.7 % | 0/1 | UNCHANGED |
| `marketops.recently_refreshed_tickers` (5 min) | 11.6 | 10.3 | 9.4 | −8.3 % | 0/1 | UNCHANGED |
| `marketops.tickers_awaiting_processing` (5 min) | 0.7 | 1.2 | 1.1 | −6.6 % | 0/1 | UNCHANGED |
| `marketops.select_signals_for_promotion` (5 min) | 41.4 | 34.0 | 28.8 | −15.2 % | 0/3 | UNCHANGED |
| `marketops.db_growth_report` (5 min, heaviest read in the app) | 1 169.9 | 2 110.1 | 1 164.3 | −44.8 % | 0/62 | UNCHANGED |
| `watcher.universe_tickers` (60 s) | 11.5 | 6.3 | 5.9 | −5.6 % | 0/3 | UNCHANGED |
| `watcher.latest_tick_for` ×50 (60 s) | 9.3 | 9.2 | 9.4 | +3.1 % | 0/1 | UNCHANGED |
| `watcher.last_signal_at` ×50 (60 s) | 4.4 | 4.3 | 4.5 | +5.9 % | 0/1 | UNCHANGED |
| `crypto_scout.upsert_token_probe` ×50 | 3.4 | 3.3 | 3.5 | +7.2 % | 0/1 | UNCHANGED |
| `crypto_scout.latest_tick_for_pair` ×50 | 16.2 | 6.0 | 6.2 | +2.9 % | 0/1 | UNCHANGED |
| **`crypto_scout.last_signal_at` ×50** | — | **602.2** | **4.3** | **−99.3 %** | **1/1** | **IMPROVED** |
| `crypto_scout.report_build` | 171.9 | 99.5 | 92.7 | −6.9 % | 0/11 | UNCHANGED |
| `meme_scout.risk_overlay` ×20 | 6.5 | 5.3 | 5.0 | −5.6 % | 0/1 | UNCHANGED |
| **`tick_agg.aggregate` dry-run 12 h (hourly)** | 4 941.9 | 4 933.0 | **4 399.5** | **−10.8 %** | **2/3** | **IMPROVED** |
| `tick_agg.report_build` | 4 148.6 | 4 151.6 | 4 225.1 | +1.8 % | 1/11 | NEUTRAL (see §2.1) |
| `outcome.select_sync_candidates` (5 min) | 138.4 | 169.2 | 140.0 | −17.2 % | 0/5 | UNCHANGED |
| `calibration.select_scoring_candidates` (5 min) | 689.7 | 758.8 | 814.8 → **685.6** | ≈0 | 1/3 | UNCHANGED |
| `calibration.summary` | 325.9 | 324.0 | 420.0 → **331.4** | +2.3 % | 0/1 | UNCHANGED |
| `champion_challenger.compare` (5 min) | 1 758.7 | 1 799.9 | 1 851.7 → **1 733.5** | −3.7 % | 0/2 | UNCHANGED |
| `outcome_coverage.build_report` | 2 578.8 | 2 516.6 | 2 494.3 | −0.9 % | 2/8 | UNCHANGED |
| `retention.prune_report` (daily) | 594.8 | 455.1 | 461.4 | +1.4 % | 0/30 | UNCHANGED |
| `pipeline.top_candidates` (4 h) | 11.7 | 5.7 | 5.9 | +4.6 % | 0/2 | UNCHANGED |
| `signal_workflow.build_report` | 1 055.6 | 1 032.6 | 1 099.4 → **983.5** | −4.8 % | 0/7 | UNCHANGED |
| `signal_workflow.promote_top_selection` | 1 280.9 | 1 266.0 | 1 294.1 | +2.2 % | 0/1 | UNCHANGED |

`crypto_scout.last_signal_at` has no first-column value because that probe
errored in the first run (the harness constructed `CryptoSignalService` without
its config); it was fixed and the whole before-arm was re-run, which is the
`before-warm` column. Its before/after comparison is therefore warm-vs-warm on
the same file, the same basis as every other row's Δ.

Where a cell shows `X → Y`, `X` is the 2-repetition after-arm and `Y` a
**7-repetition re-measurement on the same analysed copy**, run specifically
because those four probes looked slower at n=2 with an unchanged plan. Three
returned at or below their before values; `calibration.summary` came back at
331.4 ms against a before of 324.0/325.9 — **+2.3 %, with a byte-identical
plan**, which is the residual host noise floor on a 4.5 GB file, not a planner
effect. That is the whole content of the
"+29.6 %" that `calibration.summary` showed at n=2: I/O jitter on a 4.5 GB file,
not a planner decision.

### 2.1 The six statements whose plan actually changed

| where | before | after | latency |
|---|---|---|---|
| `crypto_opportunity_signals` per-token signal lookup | `ix_crypto_opportunity_signals_chain (chain=?)` + temp B-tree | `ix_crypto_opportunity_signals_token_address (token_address=?)` + temp B-tree | **15.45 ms → 0.007 ms per call** |
| `market_price_ticks` sub-window fetch (tick aggregation) | `SCAN market_price_ticks` + temp B-tree | `SEARCH … USING INDEX ix_market_price_ticks_ticker_observed (ANY(market_ticker) AND observed_at>? AND <?)` (skip-scan) + temp B-tree | **38.88 ms → 7.98 ms** |
| `market_price_ticks` sub-window count | `SCAN … USING COVERING INDEX ix_market_price_ticks_ticker_observed` | `SEARCH …` skip-scan on the same covering index | **23.65 ms → 2.01 ms** |
| `market_outcomes` bulk `IN (…12 k params…)` (calibration + coverage) | `SEARCH … USING INDEX ix_market_outcomes_market_ticker (market_ticker=?)` | `SCAN market_outcomes` | 2.39 → 2.56 ms — **neutral**; the table is 17 720 rows and the IN-list is ~12 k, so a scan is genuinely comparable |
| `market_research_packets` bulk `IN (…ids…)` | `SEARCH … USING INTEGER PRIMARY KEY (rowid=?)` | `SCAN … USING COVERING INDEX ix_market_research_packets_domain` | 1.76 → 1.79 ms — **neutral** |
| `market_price_ticks.observed_at >= ?` in `tick_agg.report_build` | `SCAN … USING COVERING INDEX ix_market_price_ticks_ticker_observed` | `SEARCH …` skip-scan on the same covering index | 0.0143 → 0.0292 ms — **the only changed plan that is slower**, by **15 microseconds**, on a statement that runs twice per report |

`crypto_scout.last_signal_at` is the same class of defect as `_load_sources`
and it was **not previously known**. It is the cooldown check on the crypto
scan path — `CryptoSignalService._passes_cooldown` calls it once per candidate
signal (`app/services/crypto_scout.py:145`) — and it was paying **15.45 ms per
call** to walk a 90 356-row index whose every entry is `'solana'`, plus a temp
B-tree over the result. `ANALYZE` fixes it as a side effect, at 0.007 ms.

The `market_price_ticks` skip-scan is the most consequential finding outside
the crypto lane: **tick aggregation is the only writer that has ever produced a
lock event on this host** (§8.4), and `ANALYZE` cuts the read half of its
sub-window loop by 4.9× and 11.8×.

### 2.2 B2 verdict

**No material production hot path regresses.** Twenty of twenty-five probes
have byte-identical plans throughout; among the five probes that contain a
changed statement, six statements changed in total — three improve by
4.9×–2 176×, two are neutral to within 0.2 ms, and exactly one is slower, by
15 microseconds. The gate that protects everything else is **GREEN**.

Two statements of precision, so the verdict is not read as more than it is:

* Two probes got slower **and** contained a changed plan —
  `calibration.select_scoring_candidates` (+7.4 % at n=2, and back to 685.6 ms
  at n=7, below its before value) and `tick_agg.report_build` (+1.8 %). In both
  the changed statement is worth ≤ 0.2 ms of a 0.7–4.2 second probe, so the
  movement is not attributable to it. "Every probe that got slower had an
  identical plan" would be a cleaner sentence and a false one.
* Two of the 25 probes (`signal_workflow.promote_top_selection`,
  `crypto_scout.upsert_token_probe_x50`) issue a statement written in the
  harness rather than calling the production function — they reproduce the
  selection predicate of `SignalPromotionService.promote_top` and
  `CryptoDiscoveryService._upsert_token`, whose real callers also write. The
  other 23 call the real read-only function.

---

## 3. B3 — competing writer (the load-bearing gate)

### 3.1 Why the old harness could not answer this

`scripts/crypto_reconcile_lock_bench.py` drives its competitor in a tight loop —
as many `BEGIN IMMEDIATE; UPDATE; COMMIT` cycles as one process can issue, i.e.
~100 % duty cycle. EVO runs one always-on writer (the watcher, roughly one write
burst per 60 s) and a 5-minute MarketOps cycle. A 100 %-duty competitor measures
a machine nobody runs. It is retained here, **separately labelled**, as an
adversarial bound only.

The primary mode is a **uniform-arrival estimator**. A real writer arrives at an
instant uncorrelated with the reconciler's lock cycle, so the wait it
experiences is a draw from the wait distribution of a uniformly-random arrival
during the pass. Sampling once per 60 s yields ≈0 samples in a 20 s pass — no
power at all — so the probe writer issues one **real** write transaction
(`INSERT INTO market_price_ticks`, the watcher's own write shape) per ~1 s,
jittered. Each attempt is an independent uniform arrival; at ~1 ms of work per
second its own duty cycle is ~0.1 %, so it samples the system without
materially perturbing it. Wait is measured **directly**, as the `perf_counter`
delta across `BEGIN IMMEDIATE` **in the other process** — never inferred from
the reconciler's lock hold, which is the inference that has already been wrong
once in this milestone.

**Pass criteria, fixed before the runs:** zero `database is locked` failures,
AND competing-writer wait p95 ≤ 1.0 s and max ≤ 5.0 s. The envelope comes from
the two real bounds on this host: `busy_timeout` is 30 000 ms and the watcher's
loop period is 60 s, so a 5 s wait is already 8 % of the loop budget and 1 s is
a comfortable operating margin.

### 3.2 Primary result — realistic arrivals, 5 trials per arm, fresh restore each trial

**Two things about this table were corrected after review, and both matter.**

1. *The sampler was biased.* The first build slept a fresh interval **after**
   each transaction. That couples the arrival process to the system: a
   transaction that waits a long time pushes the next arrival back, so exactly
   the intervals where the reconciler holds the lock longest are under-sampled
   and the measured wait is biased **downward**. The evidence showed the
   coupling directly — the competitor completed 16–18 attempts per trial before
   `ANALYZE` and 19–22 after, which cannot happen under a genuinely independent
   arrival process. It now fires on a fixed absolute schedule with a random
   initial phase, and a late attempt catches up instead of shifting the
   schedule. Attempt counts are now 21–22 in both arms.
2. *After B8, there is no un-analysed copy of production left.* `ANALYZE` ran
   on the live database, so every subsequent copy carries `sqlite_stat1`. The
   before arm is therefore **reconstructed**, with `--drop-stats`
   (`DELETE FROM sqlite_stat1` on the restored copy). That is faithful — SQLite
   treats an empty statistics table exactly as it treats a missing one, which
   §1.3's V0 variant demonstrates on an analysed file — and it is verifiable
   here: the reconstructed before arm reproduces the genuinely-un-analysed
   original run's 120–135 tokens/pass and 0.55–0.58 s hold p50 almost exactly.

| | before `ANALYZE` (reconstructed, `--drop-stats`) | after `ANALYZE` |
|---|---|---|
| `sqlite_stat1` rows at pass start | 0 | 130 |
| tokens processed / pass | 120 / 125 / 125 / 125 / 125 | **1 180 / 1 300 / 1 305 / 1 305 / 1 315** |
| pass wall | 20.13–20.53 s | 20.03–20.09 s |
| reconciler txn hold p50 | 0.5611–0.5681 s | **0.0205–0.0208 s** |
| reconciler txn hold p95 | 0.6200–0.7804 s | **0.0298–0.0328 s** |
| reconciler txn hold max | 0.645–0.863 s | 0.103–**1.768 s** (one outlier; 4 of 5 ≤ 0.122 s) |
| competing-writer wait p50 | 0.1285–0.3289 s | **0.0001 s** |
| competing-writer wait p95 | 0.5292–0.6296 s | **0.0183–0.0535 s** |
| competing-writer wait **max** | **0.729 s** | **0.929 s** (one outlier; 4 of 5 ≤ 0.053 s) |
| `database is locked` failures | **0** | **0** |
| retries | **0** | **0** |
| competitor attempts / successful writes | 21–22 | 21–22 |
| `ANALYZE` build time | — | 0.476–0.490 s |

**Both arms pass the criteria.** Per-trial p95 improvement ratios are
**11.8× / 15.8× / 28.8× / 28.8× / 34.3×** — the honest range is 12–34×, not
the 29–34× the first pass reported by pairing sorted extremes rather than
trials. Throughput improves **10.0×** (measured minimum 1 180 vs 120).

**The two max values are outliers and are not claimed as improvements.** The
after arm's worst single hold across ten trials (this run plus the original) is
1.768 s — **88 % of the 2.0 s SLO**, and *higher* than the reconstructed before
arm's worst of 0.863 s. Nine of those ten after-arm trials have a max hold
≤ 0.261 s and the p95 is 0.033 s, so this is a rare host-level stall (an fsync
pause on a 4.5 GB rollback-journal file), not a planner effect — the before arm
has an identical fat tail, with one trial at 2.025 s that **exceeded** the SLO.
The correct statement is: `ANALYZE` improves the hold p50 and p95 by ~20× and
does **not** eliminate the tail, which belongs to the storage layer.

The medians are internally consistent, which is the check that would have
caught a broken harness: before, the reconciler holds the lock 0.56 s × ~27
batches ≈ 15 s of a 20 s pass (75 % duty), so most random arrivals collide and
the median wait is a substantial fraction of a hold; after, it holds 0.021 s ×
~260 batches ≈ 5.4 s of 20 s (27 % duty), so most arrivals do not collide at
all and the median wait collapses to 0.1 ms.

The original run — coupled sampler, genuinely un-analysed copy, taken before
B8 — is retained as `coexist_{before,after}_probe.json` in the evidence
directory. Its before-arm wait p95 (0.329–0.629 s) sits **below** the corrected
run's (0.529–0.630 s), which is the direction the bias fix predicted.

### 3.3 Face-validity arm — the literal EVO cadences (watcher 60 s, MarketOps 300 s)

12 trials per arm, random initial phase (firing at t=0 of every trial would
sample one fixed point of the lock cycle and call it a distribution). Honest
but low-n by construction, which is exactly why it confirms rather than
carries:

| | before | after |
|---|---|---|
| arrivals landing inside a pass | 5 | 4 |
| tokens processed / pass | 125–135 | 1 105–1 300 |
| worst observed wait | **1.135 s** | **0.0004 s** |
| lock failures | 0 | 0 |

**One trial of the before arm breaches the declared p95 bound and is recorded
as a breach, not rounded off.** With `wait_n = 1` in that trial, its "p95" is
its single sample: **1.135 s, against the declared ≤ 1.0 s**. A p95 over one
observation is not a percentile, so this is not evidence that the pre-fix
system violated the envelope — but the criterion was declared over every
realistic arm, and applying it only to the arm that passes is how gates get
quietly moved. Classified: **before arm, paced mode — 1 of 12 trials breaches
p95; max still inside the 5.0 s bound; 0 lock failures.** It does not change
the B8 decision, which rests on the after arm (worst paced wait 0.0004 s).

### 3.4 Adversarial arm — SEPARATELY LABELLED, not a capacity number

The 100 %-duty tight-loop competitor, 3 trials per arm. **In both arms the
reconciler cannot complete**:

| | before | after |
|---|---|---|
| trial 1 | `skipped_contention`, **0 tokens** in 183.0 s | `database is locked` on its own selection read |
| trials 2–3 | `database is locked` on `backlog_size`'s `SELECT count(distinct …)` | same |
| competitor writes / trial | 5 149 – 24 155 | 3 792 – 14 443 |
| competitor lock failures | 0 | 0 |

This is a denial-of-service bound, not a coexistence measurement, and it is
the clearest possible demonstration of why the old harness could not answer
B3: it starves the reconciler identically before and after the fix, so it
cannot distinguish them. It does establish one useful safety property — under
total write starvation the reconciler **fails closed**
(`status="skipped_contention"`, or a raised `OperationalError` on a read) and
never leaves partial state. The database is in rollback-journal mode
(`journal_mode=delete`, not WAL), so a continuously-writing process blocks
readers too; that is expected and is why the real EVO writer profile, not this
one, is the capacity model.

### 3.5 B3 verdict

**PASS.** Zero `database is locked` failures and zero retries in every
realistic arm, both before and after. After `ANALYZE`, competing-writer wait
p95 is **0.018–0.053 s** and max **0.929 s**, both inside the 1.0 s / 5.0 s
envelope declared before the runs; before `ANALYZE` the probe arm is also
inside it (p95 0.53–0.63 s, max 0.73 s) and the paced arm has the single n=1
breach recorded in §3.3.

Throughput and writer-friendliness improved together — **10.0×** more tokens
per pass with **12–34×** lower wait p95 — which was the open question. The
prior work explicitly recorded that hold and wait are different metrics and
could not be assumed to move proportionally. Measured here, directly, in a
second process, with an arrival process that does not sample itself: they did.

What the gate does **not** license: the worst-case hold is unchanged in
character (§3.2), so any future change that raises `RECONCILE_BATCH_SIZE` or
lengthens `RECONCILE_MAX_DURATION_SECONDS` re-opens the tail question and needs
its own measurement. This gate says the read fix is safe to plan against; it
does not pre-approve the next knob.

---

## 4. B4 — arrivals and safe capacity, re-measured

Measured 2026-08-11 07:50 UTC from `crypto_tokens.first_seen_at`, chain
`solana`, on a fresh online copy.

| window | tokens | per day |
|---|---|---|
| 24 h | 523 | **523.0** |
| 3 d | 1 367 | 455.7 |
| 7 d | 2 940 | **420.0** |
| 14 d | 5 546 | 396.1 |
| 30 d | 10 237 | 341.2 |

The task's baseline is confirmed (524/454/421/396 vs 523/456/420/396) and so is
its observation that the rate **rises as the window shortens** — the series is
non-stationary, so a p95 over all history understates the current regime.

Per UTC calendar day, complete days only — the harness drops both the first
and last buckets, so this is **2026-07-05 → 2026-08-10, 37 days**:

| | min | p50 | p90 | **p95** | max | mean |
|---|---|---|---|---|---|---|
| all 37 days | 181 | 324 | 409 | **418** | 529 | 333.8 |
| **most recent 14** | 309 | 388 | — | **425** | **529** | 392.9 |

**Defensible p95 daily arrival estimate: 425/day**, from the recent 14 complete
days rather than the full history, because the full-history p95 (418) is
computed over a distribution the process has already left. For planning I use
**530/day** — the single highest complete day observed (2026-08-10, 529) rounded
up — so that no conclusion below depends on the trend flattening.

### 4.1 Safe capacity, from measured coexistence only

**One caveat that belongs at the top of this table, not in a footnote.** Every
trial restored the same pristine copy, so all five measured the reconciler
working on the **same head of the queue** — and by §5.1 that head is
overwhelmingly RETENTION_LOST tokens, whose source load is not the same as a
token carrying real tick evidence. Multiplying tokens/pass by 4 passes/day
therefore assumes each of the day's four passes achieves the same rate on a
*different* slice of the queue. That was not measured. The direction of the
error is knowable but not its size: write-offs are the cheapest tokens in the
backlog (no ticks to load), so **4 720/day is more likely optimistic than
pessimistic**, and §6.2's margins are large enough (7.5×) to absorb a
substantial haircut — but "4 720/day" is an extrapolation wearing a
measurement's clothes and is labelled as one here.

Never an arithmetic maximum: these are the numbers the B3 probe-mode trials
actually produced, with a real competing writer running, at the shipped
`RECONCILE_MAX_DURATION_SECONDS = 20.0`, `RECONCILE_BATCH_SIZE = 5`, and the
shipped 6-hourly cadence (`RECONCILER_CADENCE_HOURS = 6` → 4 passes/day).

| | before `ANALYZE` | after `ANALYZE` |
|---|---|---|
| tokens/pass, measured minimum across 5 trials | 120 | **1 180** |
| tokens/pass, measured median | 125 | 1 305 |
| **safe tokens/pass (conservative = measured min)** | **120** | **1 180** |
| safe tokens/hour (amortised over the 6 h interval) | 20 | **197** |
| **safe tokens/day (× 4 passes)** | **480** | **4 720** |
| vs p95 arrivals 425/day | **1.13×** | **11.1×** |
| vs planning arrivals 530/day | **0.91× — deficit** | **8.9×** |
| write-lock hold p95 as share of the 2.0 s SLO | 31–39 % | **1.5–1.6 %** |
| worst single hold observed (10 trials/arm) | 2.025 s — **over the SLO** | 1.768 s — 88 % of the SLO |

**Read the pre-fix row carefully, because the obvious reading is wrong.** At
480 tokens/day against a p95 of 425/day the pre-fix reconciler had a **1.13×
surplus, not a deficit** — it went under 1 only against the deliberately
inflated 530/day planning rate. A capacity claim that rests on which arrival
estimate you pick is not a capacity claim, so the real argument is the one in
§6.2: a 1.13× surplus is 55 tokens/day of headroom against an 11 926-token
backlog (**217 days to drain**), and once the frontier arithmetic is applied it
leaves a covered band **0.13 passes wide**, i.e. tokens routinely age past
their evidence without ever being visited. That — not the ratio — is why
`INSUFFICIENT_RECONCILIATION_CAPACITY` was the correct verdict.

After the fix there is **8.9× margin over the planning rate and 11.1× over
p95**, i.e. ~4 200 tokens/day of excess available for backlog drain.

---

## 5. B5 — the backlog, partitioned exactly

Backlog predicate as the recorder defines it: `chain='solana'`, outside the
48 h window, outcome row missing **or** `final = 0`. Every token gets exactly
one class, and the classes are read off `compute_survival`
(`app/services/crypto_tape.py:1138-1300`) — what a pass can actually learn.

| | |
|---|---|
| tokens total | 12 874 |
| outcome rows | 7 447 |
| outcome rows with `final = 1` | **2** |
| birth events | 7 447 |
| **backlog size** | **11 926** |
| oldest backlog `first_seen_at` | 2026-07-04 01:36 UTC (38.3 days) |
| backlog tokens with any retained tick at all | 2 263 (19.0 %) |

### 5.1 The partition

| class | tokens | share | what a pass can do with it |
|---|---|---|---|
| **RETENTION_LOST** | **9 320** | **78.15 %** | write it off — the latest tick that could still have qualified is older than `crypto_retention_days` and `retention.py` deleted it |
| **MISSING_REQUIRED_INITIAL_STATE** | **1 581** | **13.26 %** | nothing, ever — no `initial_liquidity_usd` to compare against (or no first-evidence anchor), so every survival label is structurally `None` and the token can never reach `final` until it decays into RETENTION_LOST |
| **PARTIALLY_RECOVERABLE** | **792** | **6.64 %** | real 15 m / 1 h / 6 h labels; the 24 h answer is permanently unavailable |
| **NO_EVIDENCE_IN_ANY_HORIZON** (typed "other") | **186** | **1.56 %** | nothing — anchored, due, inside retention, but no tick lands in any horizon window |
| **RECOVERABLE_NOW** | **47** | **0.39 %** | a genuine `survived_24h`, and `final = True` |
| NOT_YET_DUE | 0 | 0 % | — (the 48 h backlog cutoff already excludes them) |
| ALREADY_FINAL | 0 in backlog (2 in the database) | — | excluded by the predicate |
| **sum** | **11 926** | 100 % | reconciles to the backlog size exactly |

**The prior estimate was wrong in the direction that matters.** It said ~79.6 %
retention-lost (confirmed: 78.15 %) and "only ~2 400 potentially recoverable".
2 606 tokens are indeed not retention-lost — but of those, **47** can produce a
24 h answer today. The recoverable denominator is **0.39 % of the backlog**, not
20 %.

### 5.2 How each class is cheaply identified

The reconciler must not spend budget repeatedly on permanently unrecoverable
rows. Each class has a cost:

| class | identification | cost | monotone? |
|---|---|---|---|
| RETENTION_LOST | one timestamp range on `crypto_tokens.first_seen_at`: `anchor < now − (retention_days + 36 h)` | one indexable predicate, no join, no source load | **yes** — a token never leaves this class, so it can be written off once and excluded from selection forever |
| NOT_YET_DUE | `first_seen_at > now − 36 h` | already implemented as `min_age_minutes`/`max_first_seen_at` on `_universe` | no (they mature) |
| MISSING_REQUIRED_INITIAL_STATE | `crypto_token_birth_events.initial_liquidity_usd IS NULL`, or no birth row and no first-evidence timestamp | one indexed lookup on the birth table (`ix_crypto_token_birth_events_token_address`, `stat = "7420 1"`), no tick scan | **yes in practice** — the initial state describes a moment that has already passed and no future pass can recover it |
| RECOVERABLE_NOW / PARTIALLY_RECOVERABLE / NO_EVIDENCE_IN_ANY_HORIZON | cannot be separated without touching `crypto_price_ticks` | a horizon-window probe on `ix_crypto_price_ticks_token_address` — **0.006 ms after `ANALYZE`, 14.6 ms before** | no |

The last row is the point of this whole milestone in miniature: the cheap test
that separates recoverable from unrecoverable work is only cheap **because**
the query plan was fixed. Before `ANALYZE`, probing 11 926 backlog tokens for
recoverability would have cost ~174 s of pure read; after, ~0.07 s.

Those two per-token figures are the V0 and V2 medians for the full
`crypto_price_ticks` lookup that `_load_sources` issues (fetch included), which
is an **upper bound** on an `EXISTS`-shaped probe, not a measurement of one —
an `EXISTS` stops at the first matching row. The 2 400× ratio between them is
the load-bearing part and is a property of the plan, not of the projection.

**Recommended selection consequence (proposed, not implemented):** the two
monotone classes — 9 320 + 1 581 = **10 901 tokens, 91.4 % of the backlog** —
can be excluded from selection by two indexable predicates with no source load
at all. That leaves 1 025 tokens worth a tick probe, of which 839 (47
RECOVERABLE_NOW + 792 PARTIALLY_RECOVERABLE) actually have a label to give and
186 turn out to have none. That is the difference between a reconciler that
spends its budget re-deriving write-offs and one that spends it where a
measurement is still possible.

### 5.3 The real denominator bottleneck — measured, and it is neither capacity nor retention

Take the cohort where **no pruning is possible at all**: tokens born between 36 h
and 3 days ago, so their entire 24 h evidence window (anchor + 12 h … + 36 h) is
comfortably inside the 7-day retention. If evidence exists, it is still there.

| cohort | n | tick in 15 m window | 1 h | **6 h** | **24 h** | `initial_liquidity_usd` known |
|---|---|---|---|---|---|---|
| **36 h – 3 d (zero pruning possible)** | 622 | 615 (98.9 %) | 607 (97.6 %) | **81 (13.0 %)** | **25 (4.0 %)** | 245 (39.4 %) |
| 3 d – 7 d | 1 573 | 1 559 (99.1 %) | 1 530 (97.3 %) | 227 (14.4 %) | 54 (3.4 %) | 620 (39.4 %) |
| 7 d – 8.5 d (24 h window partly pruned) | 612 | 122 (19.9 %) | 139 (22.7 %) | 34 (5.6 %) | 13 (2.1 %) | 244 (39.9 %) |

And the observation span itself — first evidence to the **last** tick this lane
ever recorded for a token, over the 2 364 tokens aged 36 h–8.5 d that have any:

| p50 | p90 | max |
|---|---|---|
| **83.0 minutes** | 322.0 minutes (5.4 h) | 11 267.9 minutes |

**The crypto price-tick lane stops observing a token a median of 83 minutes
after its birth.** The 24 h horizon needs an observation between 12 h and 36 h.
Coverage therefore collapses from 98.9 % at 15 m to 13.0 % at 6 h to **4.0 % at
24 h — in a cohort where nothing has been pruned**. Separately,
`initial_liquidity_usd` is NULL on 4 453 of 7 447 birth events (**59.8 %**), so
even a token with a 24 h tick has only a **40.2 %** chance of having a baseline
to compare it against (39.4 % measured within these cohorts).

The third row is the control that makes the argument airtight rather than
suggestive. Once the window *is* partly pruned (7 d–8.5 d), 15 m coverage
collapses from 98.9 % to 19.9 % — retention is visibly doing exactly what
retention does, to the horizons whose evidence has aged out. It never touches
the 24 h figure, which is already 4 % before any pruning is possible.

This is decisive for §7: the 24 h evidence is not being deleted by retention,
**it is never being recorded**. The ceiling on 24 h-survival measurability at
current observation coverage is roughly **4 % of arrivals — about 17–21 tokens
per day out of 425–530**, and jointly with the liquidity baseline nearer
**1.6 %**. Fixing the query plan raises capacity 10×; it cannot raise a
denominator that the observation lane never filled.

Both tables come from `scripts/crypto_backlog_partition.py`'s
`b5_3_observation_coverage` block and are committed as
`docs/evidence/crypto-query-plan-and-denominator-recovery-001/backlog_partition_v2.json`.
They were originally produced by an ad-hoc query with no committed harness;
that was the single largest evidence gap in the first draft of this document
and it is closed here, with the re-run reproducing the ad-hoc figures to within
the sampling difference of a 50-minute-later snapshot (610 → 622 tokens,
4.1 % → 4.0 %, span p50 83.1 → 83.0 min).

---

## 6. B6 — recoverable-frontier-first allocation

### 6.1 The policy, stated before any outcome was evaluated

1. **Frontier first.** Evidence closest to retention expiry is served first.
2. **Then oldest due recoverable horizon.**
3. **Then current work** (the in-window head).
4. **Reserve enough capacity that new arrivals never create fresh backlog** —
   the reserve is sized against the p95 arrival rate, not the mean.

This is not a new mechanism. It is exactly what the shipped selection already
does, and it is worth naming which lines implement each clause, because the
milestone's job here is to prove the arithmetic works, not to invent a policy:

| clause | implementation |
|---|---|
| frontier first | `run_once` reserves `min(backlog_size, limit // 2)` **before** calling `_universe`, then places the backlog list **first**: `tokens = extra + tokens` (`crypto_tape.py:1420-1461`) |
| oldest due recoverable horizon | `unreconciled_backlog` orders `first_seen_at ASC, id ASC` (`:726`) |
| then current work | the in-window head is capped at `limit − reserved_backlog_budget` and appended after |
| arrivals never create fresh backlog | the reserve is `limit // 2 = 1 000` of `crypto_tape_reconciler_limit = 2000` |

Two clarifications the arithmetic depends on:

* A token does **not** need to be visited every pass. It needs **one** visit
  between the moment its 24 h horizon is due (age 36 h) and the moment its
  evidence is pruned (age `36 h + retention_days` = **8.5 days**). That is a
  **7.0-day-wide window of opportunity**, or 28 passes at the 6-hourly cadence.
  The narrower **6.5-day figure used in §6.2 is deliberately different**: it
  starts at the 48 h backlog cutoff rather than the 36 h due edge, because the
  in-window lane is not what the arithmetic relies on. The conservative one is
  the one that carries the argument.
* Oldest-first ordering means the pass always spends its budget at the expiring
  edge of that window, which is precisely clause 1.

### 6.2 The arithmetic

Inputs, all measured, none assumed:

| symbol | value | source |
|---|---|---|
| `C` safe tokens/pass | **1 180** | B3, conservative minimum of 5 trials with a live competing writer |
| passes/day | **4** | `RECONCILER_CADENCE_HOURS = 6` |
| `C_day` safe tokens/day | **4 720** | 1 180 × 4 |
| `A` planning arrivals/day | **530** | B4 (p95 = 425; 530 = highest complete day, rounded up) |
| `B_res` backlog slots reserved per pass | **1 000** | `limit // 2`, `limit = 2000` |
| `W` recoverable window width | **6.5 days** | 8.5 d expiry − 2 d backlog entry |
| `L` current backlog | **11 926** | B5 |

**Step 1 — how the pass splits.** Backlog-first with a 1 000-slot reserve and
1 180 tokens actually processed:
```
backlog lane   = min(1000, 1180)            = 1 000 tokens/pass  = 4 000/day
in-window lane = 1180 − 1000                =   180 tokens/pass  =   720/day
```
The backlog lane is capped by the **reserve**, not by throughput, which is why
every step below is unchanged by the 1 280 → 1 180 revision. Throughput would
have to fall below 1 000 tokens/pass before this lane felt it — a 15 % margin
on top of the 8.9× arrival margin.

**Step 2 — does the backlog lane service arrivals?** Tokens enter the backlog
lane at the arrival rate, one pass-slot each per pass they remain selectable:
```
arrivals per pass          = A / 4 = 530 / 4 = 132.5 tokens
backlog slots per pass     = 1 000
margin                     = 1000 / 132.5   = 7.5×
```
**Yes, with 7.5× margin.** Even at the single worst day ever observed (529) the
lane is 7.5× oversubscribed.

**Step 3 — is the frontier band wide enough that nothing expires unvisited?**
The oldest-first selection covers, per pass, the oldest 1 000 non-final backlog
tokens. Tokens that have already passed 8.5 days are RETENTION_LOST, finalise
on their single visit and leave the queue, consuming `A/4 = 132.5` slots per
pass. The remaining slots sit on the still-recoverable frontier:
```
frontier slots per pass    = 1000 − 132.5   = 867.5
frontier band width        = 867.5 / 132.5  = 6.5 passes of arrivals
                           = 6.5 / 4        = 1.64 days of age
```
A token enters that band at age `8.5 − 1.64 = 6.86 days` and leaves it at
8.5 days, i.e. it sits inside the covered band for **1.64 days = 6.5
consecutive passes**. It only needs one. **Nothing expires unvisited.**

**Step 4 — does the backlog shrink at the same time?** The lane runs 4 000
visits/day against a standing backlog of 11 926:
```
first full sweep of the existing backlog = 11 926 / 4 000 = 2.98 days
```
After that sweep: 9 320 RETENTION_LOST and 47 RECOVERABLE_NOW have `final = 1`
and leave selection permanently; 2 559 remain non-final and age out on their own
schedule. Steady-state standing backlog afterwards:
```
6.5 days of recoverable window × 530 arrivals/day = 3 445 tokens
```
served by 4 000 visits/day — still above the standing population, so the queue
never grows.

**Both conditions hold**, with the binding constraint being step 3 at 6.5
passes of slack. **The answer is not INSUFFICIENT.**

Three honesty notes on the arithmetic itself:

* Steps 2 and 3 are **not independent checks**. Both are `1000 / 132.5`, once
  with and once without a `− 132.5` term. There is one constraint here, looked
  at twice; treating them as two corroborating results would overstate the
  robustness.
* Step 4 compares 4 000 *visits*/day to a standing population of 3 445
  *tokens*. Those units only reconcile because ~78 % of visits terminate their
  token (RETENTION_LOST finalises on contact). The conclusion survives, but the
  sentence as written does not establish it — the load-bearing fact is the
  termination rate, not the ratio.
* "First full sweep = 11 926 / 4 000" assumes every visit lands on a distinct
  token. Under strict oldest-first with a 0.39 % finalisation rate that holds
  for the 9 367 tokens that finalise on contact and **not** for the 2 559 that
  do not — those are re-visited until they age out. 2.98 days is therefore the
  time to first-touch the terminating majority, not a clean queue drain.

**One consequence worth naming rather than discovering later.** Because the
ordering is strictly oldest-first, a token is not visited when its 24 h horizon
becomes due (age 36 h) — it is visited when it reaches the frontier band at age
**6.86 days**. Nothing is lost (its evidence is retained to 8.5 days), but every
outcome is recorded roughly **five days later than it could have been**, and the
frontier-first policy is the direct cause. That is the correct trade while
evidence expiry is the risk being managed; if measurement latency ever becomes
the thing being optimised, this is the line to revisit, and the fix is a split
budget (a small always-fresh lane at 36 h plus the frontier lane) rather than
abandoning oldest-first.

For completeness, the same arithmetic **before** the read fix: `C_day = 480`,
`backlog lane = min(1000, 120) = 120/pass = 480/day`.

* Against the 530/day planning rate (132.5/pass) step 2 gives **0.91×** — below
  1 — and the frontier band width in step 3 is negative: tokens expire unvisited
  every single day.
* Against the p95 rate of 425/day (106.25/pass) step 2 gives **1.13×**, a
  surplus — but step 3 gives `(120 − 106.25) / 106.25 = 0.13 passes` of covered
  band. A token needs to be inside the band for at least one pass to be visited
  at all, and 0.13 < 1, so **most tokens still age past their evidence without
  ever being reached**, and the 13.75 tokens/pass of surplus would need 217 days
  to drain the 11 926 already queued.

Both readings give the same verdict for different reasons, which is why the
conclusion does not depend on which arrival estimate is chosen. That is
`INSUFFICIENT_RECONCILIATION_CAPACITY`, re-derived from first principles, and it
is what the query plan fixed.

### 6.3 The honest caveat this arithmetic cannot hide

Capacity is 4 720 visits/day. **Yield is 0.39 %** (B5). The allocation policy
above provably services arrivals and drains backlog — of *write-offs*. Of the
11 926 backlog tokens it will visit in 2.98 days, **47** produce a measurement
and 11 879 produce a label meaning "we could not tell". Sizing the reconciler
is now solved; filling the denominator is not, and §7 shows retention is not
the lever either.

**Recommended (not implemented):** because RETENTION_LOST and
MISSING_REQUIRED_INITIAL_STATE are **monotone** and identifiable by two
indexable predicates (B5.2), excluding them from selection would drop the
standing population from 3 445 to the ~1 025 tokens §5.2 says still merit a
tick probe, and the frontier band from 6.5 passes to well over 20. That is a selection-predicate change, and it belongs to a design pass,
not to this measurement.

---

## 7. B7 — retention decision package (analysis only; nothing was changed)

### 7.1 Cost side, measured

| | |
|---|---|
| `crypto_price_ticks` rows | 143 107 |
| `crypto_price_ticks` bytes (from `dbstat`) | 38 813 696 (**0.85 % of the 4.55 GB file**) |
| bytes per tick row | **271.2** |
| tick rows per complete day | **19 375.8** (≈ 5.25 MB/day) |
| retained tick span | 2026-08-04 → 2026-08-11 |
| page size / page count | 4 096 / 1 110 992 |
| **freelist** | **419 080 pages = 1.717 GB = 37.72 % of the file** |

| window | recovery deadline (token age) | extra days | incremental tick rows | incremental disk | fits in the existing freelist? |
|---|---|---|---|---|---|
| **7 d** (current) | 8.5 d | 0 | 0 | 0 | — |
| **10 d** | 11.5 d | +3 | +58 128 | **+15.0 MiB** | yes |
| **14 d** | 15.5 d | +7 | +135 631 | **+35.1 MiB** | yes |
| **21 d** | 22.5 d | +14 | +271 262 | **+70.2 MiB** | yes |

**Daily DB growth is unchanged in every case.** The tick table grows 5.25 MB/day
regardless of the window; what the window changes is the steady-state *height*
of that table, and every option above (up to +70 MiB) is **absorbed by the
1.717 GB of free pages the file already carries**. SQLite `DELETE` never
shrinks the file, so shortening the window frees exactly **0 bytes** and
lengthening it adds exactly **0 bytes** of file size until the freelist is
exhausted — which +70 MiB does not come close to doing. The prior work's
finding holds and cuts both ways.

### 7.2 Benefit side, measured — and it is far smaller than it looks

The counterfactual ceiling: how many of today's 11 926 backlog tokens would
still have their evidence at each window.

| window | backlog tokens whose closing edge is inside the window |
|---|---|
| 7 d (current) | 2 612 |
| 10 d | 3 613 |
| 14 d | 5 176 |
| 21 d | 7 404 |

**Read that column correctly: it is a ceiling on what a longer window WOULD
HAVE SAVED, not a forecast of what extending it now recovers.** Retention is
not retroactive. The rows are already deleted. Extending the window today
recovers **zero** tokens from the existing backlog.

And the prospective benefit is bounded by §5.3, which is the number that
settles this: in the **36 h – 3 d cohort, where no pruning is possible at all**,
24 h tick coverage is **4.0 %** and 6 h coverage is **13.0 %**. The observation
lane's median last tick is **83 minutes** after birth. The 24 h evidence is not
being pruned — **it was never recorded**. A longer retention window preserves
absence.

Quantified: at 4.0 % coverage and 530 arrivals/day, extending 7 d → 14 d
protects the ~22 tokens/day that *do* have a 24 h observation, for an extra
7 days of reconciler downtime tolerance. It cannot reach the other 508.

### 7.3 Recommendation, with numbers

**Recommend 7 d → 14 d. Priority: LOW. It is insurance, not the denominator
lever.**

| criterion | value |
|---|---|
| incremental tick rows | +135 631 |
| incremental disk | **+35.1 MiB, entirely absorbed by the existing 1.717 GB freelist → file size change 0** |
| daily DB growth change | **none** (5.25 MB/day either way) |
| recovery time bought | recovery deadline **8.5 d → 15.5 d**; reconciler outage tolerance **7 d → 14 d** |
| expected additional recoverable 24 h evidence | ~22 tokens/day protected × the outage days actually used — **0 in normal operation**, because B6 step 3 already gives 6.5 passes of slack at 7 d |
| expected additional recoverable 6 h evidence | ~69 tokens/day protected, same conditional |
| risk | none identified: no schema change, one config value (`crypto_retention_days`), and `retention.py` already owns the predicate |

**Do NOT go to 21 d.** It doubles the cost again for the same conditional
benefit, and it pushes the recovery deadline (22.5 d) past the point where the
frontier-first arithmetic in §6.2 stays comfortable — a wider window means a
larger standing non-final population competing for the same 1 000 slots.

**Do NOT shorten below 7 d.** It frees no disk (freelist), and it directly
shrinks the only window in which the reconciler can do its job.

**The actual denominator recommendation, which is not a retention change:**
extend the crypto price-tick **observation** lane so a token is re-observed at
6 h and 24 h after birth, not only for its first ~83 minutes. That is a
scout/watcher scheduling change in a different lane, it is where 96 % of the
missing measurement lives, and no amount of retention, capacity or query
planning substitutes for it.

---

## 8. B8 — live `ANALYZE` decision

### 8.1 The gate, evaluated before running anything

| gate | required | measured | verdict |
|---|---|---|---|
| B2 shows no unresolved material regression | mandatory | 25 hot paths; 20 probes with byte-identical plans; 6 statements changed — 3 improved (4.9×–2 176×), 2 neutral (< 0.2 ms), 1 slower by 15 µs. No unresolved regression. | **PASS** |
| B3 passes | mandatory | 0 lock failures and 0 retries in every realistic arm; wait p95 0.018 s and max 0.078 s against a declared envelope of 1.0 s / 5.0 s | **PASS** |
| integrity green | mandatory | pre-flight: `PRAGMA quick_check` = `ok` on the **live** file, plus `PRAGMA integrity_check` = `ok` on the byte-faithful online copy (4.8 s, run off-host-path so production was not asked to read 4.55 GB before the decision). Post-action: full `PRAGMA integrity_check` = `ok` on the **live** file — that is the `integrity_check` recorded in `analyze_live.json` | **PASS** |
| backup fresh | mandatory | `backup-20260811T013803Z.db.gz`, **5.43 h** old, limit 36 h | **PASS** |
| no schema migration involved | mandatory | `ANALYZE` writes only `sqlite_stat1`; Alembic asserted `0027` before and after | **PASS** |

All five gates pass. **Decision: EXECUTE.**

### 8.2 How it was run — deliberate, never hidden in a timer

`scripts/sqlite_analyze_maintenance.py`, run by hand from the scratch checkout
with the existing venv interpreter. It requires the target path to be repeated
in `--yes-run-analyze-on`, an exact confirmation phrase, an explicit
`--expect-alembic`, and a backup directory, and it refuses and changes nothing
if any preflight fails. A `--dry-run` pass was executed first; it reported all
preflights green and did not touch the file.

**The script is imported by nothing, wired into no systemd unit, and called by
no service.** That is deliberate and load-bearing: `ANALYZE` re-plans every
query in the application, which is a decision a human makes with a regression
audit in front of them, not something a timer performs unattended. There is
consequently **no automatic statistics-refresh mechanism**, and §10 records that
as the explicit follow-up it is.

### 8.3 Evidence, live EVO database, 2026-08-11 07:04:43–07:04:51 UTC

| | before | after |
|---|---|---|
| `sqlite_stat*` tables | **none** | `sqlite_stat1` |
| `sqlite_stat1` rows | 0 | **130** |
| file bytes | 4 550 623 232 | **4 550 623 232 (Δ 0)** |
| page_count | 1 110 992 | **1 110 992 (Δ 0)** |
| freelist_count | 418 831 | 418 827 (**Δ −4 pages**) |
| freelist share | 37.70 % | 37.70 % |
| journal_mode | `delete` | `delete` |
| Alembic revision | `0027` | **`0027`** |
| lock-telemetry events / lock events | 6 806 / **8** | 6 806 / **8 (Δ 0)** |
| `PRAGMA integrity_check` | — | **`ok`** |

**`ANALYZE` duration on the live 4.55 GB database: 0.4597 s.**

The 130 statistics rows fit in **4 pages taken from the existing freelist**, so
the file did not grow by a single byte — which is the cleanest possible
demonstration that this is a maintenance action and not a data change.

### 8.4 Production health afterwards

The MarketOps timer fired **one second after** `ANALYZE` returned, which is a
better test than a scheduled one:

| run | started | finished | status |
|---|---|---|---|
| 9231 | 06:35:03 | 06:35:45 | ok (pre) |
| 9232 | 06:41:03 | 06:41:50 | ok (pre) |
| 9233 | 06:47:03 | 06:47:47 | ok (pre) |
| 9234 | 06:53:03 | 06:54:33 | ok (pre) |
| 9235 | 06:59:03 | 06:59:55 | ok (pre) |
| **9236** | **07:04:44** | **07:05:26** | **ok — first cycle after `ANALYZE`, 41.7 s** |
| **9237** | **07:10:04** | **07:10:44** | **ok, 40.4 s** |
| **9238** | **07:16:04** | **07:16:42** | **ok, 38.6 s** |

Three consecutive clean cycles after the change, at 41.7 / 40.4 / 38.6 s
against a pre-change range of 41.8–89.8 s. The watcher service stayed
`active (running)` throughout (uptime 1 week 0 days, never restarted) and its
60-second loop recorded `ok` on all 40 of its most recent iterations across the
change.

**Lock telemetry: 8 events before, 8 after (Δ 0).** One honest qualification on
that number rather than a bare "zero new lock events": the telemetry sink only
instruments specific writers, and every one of the 8 historical events is
`tick_aggregation` `commit_unit`. Tick aggregation is hourly and its last run
preceded `ANALYZE`, so the immediate post-change window contains no instrumented
run of the one writer that has ever produced a lock event. §8.5 records the
first post-`ANALYZE` tick-aggregation run separately.

This matters more than it sounds: tick aggregation is also the **only**
production timer whose query plan materially changed (§2.1, the
`market_price_ticks` skip-scan, 38.88 → 7.98 ms and 23.65 → 2.01 ms), so it is
simultaneously the largest expected benefit and the only historical lock-event
source.

### 8.5 The first post-`ANALYZE` tick-aggregation run — it FAILED, and the cause is not `ANALYZE`

§8.4 flagged this as the one observation the immediate post-change window could
not supply. Here it is, and it is not the clean result the rest of §8 is.

**2026-08-11 08:04:31 UTC — `probability-arena-tick-aggregation.service`
exited 1 with `sqlite3.OperationalError: database is locked`.** The unit is now
in `failed` state on EVO. It has been left that way deliberately: clearing it
would hide a production failure that occurred inside this milestone's change
window, and the operator should see it.

The failure is **not** in the aggregation. It is in the preamble:

```
app/cli.py:2332  aggregate_market_ticks -> run_migrations()
app/db.py:69     inspector.has_table("markets")
                 [SQL: PRAGMA main.table_info("markets")]
                 sqlite3.OperationalError: database is locked
```

Five lines of evidence, and they all point the same way:

1. **The collision is a schedule race, not a plan change.** MarketOps run 9246
   held the database from **08:04:03 to 08:04:55**; the tick-aggregation timer
   fired at **08:04:26**, inside that window, and gave up at **08:04:31** —
   after exactly **5 seconds**.
2. **Five seconds is the tell.** This host's declared busy timeout is 30 000 ms.
   But `run_migrations()` builds its own engine, and on EVO's deployed `main`
   that engine has **no `connect_args`**, so it runs at Python's incidental
   `sqlite3` default of 5 s. CRYPTO-COVERAGE-REPAIR-001 B11 is the fix for
   exactly this, and it is **on this branch, not deployed**. This failure is
   that milestone's theoretical defect happening in production.
3. **It has happened before `ANALYZE` existed.** The identical failure —
   same unit, same `database is locked` — occurred on **2026-08-08 14:35**,
   three days before this work. The journal shows exactly two such failures
   since 2026-07-20 and this is the second. The lock-telemetry file records the
   Aug-8 event as `retried_failed`, `lock_wait_ms = 126 170`.
4. **`ANALYZE` did not lengthen MarketOps.** Over 552 runs in the two days
   before the change, MarketOps p50 was 42.2 s, p90 55.7 s, max 89.5 s. Over
   the 11 runs after, p50 **40.2 s**, p90 49.1 s, max **51.9 s**. Run 9246,
   the one that won the race, took 51.9 s — comfortably inside the pre-change
   p90. If `ANALYZE` had made MarketOps hold longer, this is where it would
   show, and it does not.
5. **The benchmarks were not running.** The last coexistence-bench write
   completed at **07:57:27**, seven minutes before the failure; nothing of this
   milestone's was executing at 08:04. That was the strongest alternative
   explanation and it does not hold.

**Conclusion: pre-existing defect, independently reproduced.** The honest
statement is *not* "ANALYZE caused no problems" — it is "the one production
failure inside this window has a cause that predates `ANALYZE`, is already
diagnosed and already fixed on this branch, and the post-change telemetry shows
no mechanism by which `ANALYZE` could have caused it." A reader who wants to
attribute it to `ANALYZE` has to explain the Aug-8 occurrence and the unchanged
MarketOps distribution.

**It does raise the priority of deploying the B11 `connect_args` fix**, which
is a separate merge decision this milestone does not make. Until then this race
will recur whenever the hourly tick-aggregation timer drifts into a MarketOps
cycle — roughly, whenever their start times land within ~50 s of each other.

The run that matters for the original question — does tick aggregation behave
normally against the new statistics — is §8.6, and it does.

### 8.6 The first tick-aggregation run to complete against the new statistics

**2026-08-11 09:04:44 UTC, run 783 — `ok`.** The unit recovered on its next
scheduled firing with no intervention; its systemd state went from `failed`
back to `inactive`.

| | 09:04 (post) | the five preceding runs (pre) |
|---|---|---|
| status | **ok** | ok |
| rows read | 103 800 | 103 200 – 111 450 |
| buckets written | 21 927 | 21 848 – 23 442 |
| truncated | 0 | 0 |
| **write-lock hold, sum over 16 commit units** | **4 008 ms** | 3 952 / 3 995 / 4 043 / 4 270 / 4 351 ms |
| **write-lock hold, max** | **387 ms** | 378 / 402 / 422 / 392 / 431 ms |
| commit time, sum | 6 608 ms | 49 / 58 / 67 / 967 / 3 556 ms |
| **new lock events** | **0** | 0 |
| retries | 0 | 0 |

**The safety-relevant number is the write-lock hold, and it is unchanged.** The
post-`ANALYZE` run's hold sum (4 008 ms) and max (387 ms) sit in the middle of
the pre-change distribution on both measures. Sixteen commit units, every one
`outcome=success`, `retry_count=0`, `lock_wait_ms=null`. The lock-event tally
across the whole telemetry file is still **8**, all of them predating this work.

**What did NOT show up, stated because B2 predicted it would.** §2.1 measured
the read half of this loop getting 4.9× and 11.8× faster (38.88 → 7.98 ms per
sub-window fetch, 23.65 → 2.01 ms per count, over 13 sub-windows ≈ 0.7 s of
read saved per run). That saving is invisible here, for a reason that is
structural rather than disappointing: those reads happen **outside** the write
transaction, so they cannot move `transaction_hold_ms`, and the end-to-end
`duration_ms` is dominated by commit time, which ranged 49 ms – 6 608 ms across
six runs on the same data. A ~0.7 s read saving is below the noise floor of the
only end-to-end metric this unit records. The read improvement is real and
measured (§2.1); this run neither confirms nor contradicts it, and claiming it
did would be reading a number that is not there.

**B8's health question is answered:** the one production writer whose plan
`ANALYZE` materially changed, and the only writer that has ever produced a lock
event on this host, now runs against the new statistics with identical
write-lock behaviour and zero lock events.

---

## 9. What is NOT true, and what could not be measured

Recorded because several of these correct a claim that would otherwise be
inherited as fact.

* **The temp B-tree does not disappear after `ANALYZE`** (§1.4). It survives
  every variant. Only the number of rows fed to the sorter changes. Any future
  composite-index proposal that claims sorter elimination is claiming something
  different from what `ANALYZE` does — and would be the only way to actually
  get it.
* **The good index's statistic is not what fixes the plan** (§1.3). On the
  three large tables, publishing `ix_*_token_address = "N 31"` changes nothing;
  publishing `ix_*_chain = "N N"` changes everything. The mechanism is SQLite's
  ~10-row default for an un-analysed indexed equality. The table's own
  row-count row is **not** inert either — it alone fixes `crypto_price_ticks`,
  though not the other three.
* **`ANALYZE` is not uniformly good for the reconciler.** Three of its six
  selection queries got slower (§1.6), by a combined +15.9 ms per pass. The
  net is overwhelmingly positive, but "ANALYZE improved the reconciler" is
  false as stated; "ANALYZE improved the per-token source load 71× and cost the
  once-per-pass selection 15.9 ms" is true.
* **The prior estimate that ~2 400 backlog tokens were "potentially
  recoverable" is not wrong about the count but is wrong about the meaning.**
  2 606 are not retention-lost; **47** can produce a 24 h answer (§5.1).
* **Retention length is not the denominator bottleneck** (§5.3, §7.2). In a
  cohort with zero possible pruning, 24 h coverage is 4.0 %.
* **`ANALYZE` did not make the production run cleanly.** A production unit
  failed inside the change window (§8.5). The evidence says the cause predates
  `ANALYZE` and this branch already fixes it — but "no problems after the
  change" is not what happened, and this document does not say it.
* **The B2 no-regression verdict is about materiality, not about direction.**
  One statement's plan did get slower (§2.1). It costs 15 microseconds.
* **Per-statement timings from cursor hooks under-report streaming queries**
  (§1.2 †). Probe-level wall time is the authoritative latency in every table
  in this document.
* **The adversarial arm is not a capacity measurement** (§3.4) and is labelled
  as such wherever it appears.

Not measured, and why:

* **Long-run statistics staleness.** `sqlite_stat1` is a snapshot. Tables grow;
  the rows written on 2026-08-11 will drift. Nothing in this repo refreshes
  them, and B8 deliberately did not install anything that does. **This is the
  single most important follow-up** (§10, item 1).
* **Cold-disk behaviour.** EVO has 92 GB RAM and ~21 GB of page cache against a
  4.55 GB database; every figure here is warm-cache.
* **`run_scheduled_reconciliation` end to end.** The harnesses call `run_once`
  with exactly the kwargs that function passes, so the flag gate and the
  `_reconciliation_should_abort` MarketOps query are excluded — one indexed
  SELECT per pass.
* **Whether the 4.0 % / 13.0 % observation coverage is a scout scheduling
  choice, a provider budget limit, or a bug.** §5.3 measures the effect
  precisely and does not diagnose the cause; that belongs to the crypto scout
  lane.
* **Post-`ANALYZE` behaviour of the polymarket, tennis, cross-venue and
  horizon lanes.** They are not on a production timer on EVO, so they were out
  of B2's "hot path" scope. Their plans changed with everything else.
* **Statistics drift after the reconciler runs.** Every capacity figure was
  measured against statistics computed on a database where the reconciler has
  never run at scale. A pass that writes 1 300 lifecycle snapshots and actor
  observations per run changes the shape of tables `ANALYZE` has measured, and
  nothing re-measures them. This is the same gap as the staleness item above,
  seen from the reconciler's side.

---

## 10. Follow-ups (proposed; none implemented here)

1. **Statistics maintenance has no owner.** `ANALYZE` is now a one-off fact
   with a 2026-08-11 timestamp. Options, in increasing order of automation:
   re-run `scripts/sqlite_analyze_maintenance.py` by hand after any large data
   change; or add `PRAGMA optimize` to an existing maintenance path
   (coordinated with the backup timer, which already owns a serialised window).
   Either needs its own decision — the whole point of B8's design is that this
   does not get switched on quietly.
2. **Exclude the two monotone unrecoverable classes from selection** (§6.3).
   Two indexable predicates remove 91.3 % of the backlog from every future
   pass's budget.
3. **`crypto_scout.last_signal_at` had the identical `chain`-index defect**
   (§2.1) and nobody knew. A one-off sweep for "indexed column with one
   distinct value" across the schema would find any remaining instances; the
   `sqlite_stat1` rows now on the live database make that a single query
   (`WHERE stat LIKE '% ' || (rows) `, i.e. average-rows == total-rows).
4. **The 24 h observation gap** (§5.3, §7.3) is the denominator. Everything in
   this milestone is upstream plumbing for a measurement that is not being
   taken.
5. **`_universe`'s `chain = ?` predicate on a single-chain deployment** (§1.6)
   is what makes the selection query plan-sensitive at all.
6. **Deploy CRYPTO-COVERAGE-REPAIR-001's B11 `connect_args` fix.** §8.5 is that
   defect firing in production for the second time in four days: the engine
   `run_migrations()` builds runs at Python's incidental 5 s busy timeout
   instead of this application's declared 30 s, so any CLI entry point that
   calls it dies rather than waits whenever it lands inside a MarketOps cycle.
   The fix is already written and on this branch. Priority is now HIGH on
   evidence, not on argument.
7. **The tick-aggregation and MarketOps timers collide by construction.**
   Tick aggregation is `OnUnitActiveSec=1h` and drifts; MarketOps is a
   5-minute grid. They will keep intersecting. Item 6 makes the collision
   survivable; a phase offset would make it rarer.

---

## 11. Validation and safety

* **Suite:** `3 203 passed / 3 skipped` at `b0d4af5` before any change
  (215.5 s), re-confirmed after the five added files. One intermediate run
  under concurrent load (397.6 s vs 215.5 s) produced four failures —
  `test_crypto_horizon_obs_001.py::TestCohort::{test_window_filters_on_first_evidence_not_observed_at,
  test_hours_1_never_returns_token_older_than_60_minutes,
  test_timezone_naive_first_evidence_handled}` and
  `test_edge_precheck.py::TestApi::test_run_list_report_roundtrip`. **All four
  pass in isolation** and are the same set CRYPTO-RECONCILIATION-CAPACITY-001 §9
  already recorded as a suspected load/clock-sensitivity flake. Every file this
  milestone adds is a standalone script or a document, imported by no
  application code and collected by no test.
* **`git diff --check`:** clean.
* **Safety grep** over the five added scripts: no hits for
  `expected_value|kelly|position_siz|paper_trad|place_order|submit_order|create_order|wallet|recommended_side|trade_recommend|execute_trade`.
* **Boundary statement:** nothing here computes EV, recommends a side, sizes a
  position, places or simulates an order, or touches a wallet or a key. The
  reconciler remains a derived-tape assembler over already-persisted rows; it
  makes zero external calls by construction.
* **Copies:** every benchmark ran against `sqlite3.Connection.backup()` copies
  taken from a **read-only** source connection, under `/mnt/data/cqp-bench/`.
  **Four of the five scripts now share one guard** (`_refuse_non_scratch`) that
  refuses a deployment-looking fragment (`projects/probability-arena/data`,
  `/var/lib/`, `/srv/`), the production **basename** `probability_arena.db`
  anywhere, and any path inside the repo checkout. The basename and repo-root
  checks were added after review: the fragment blacklist alone is tuned to one
  host and would not have stopped a developer's own
  `<repo>/data/probability_arena.db`, which is exactly the file two of these
  scripts would have written `sqlite_stat1` into. The coexistence bench now
  guards `--pristine-db` as well as `--work-db`. `crypto_backlog_partition.py`
  had no guard at all and now has the same one (it opens `mode=ro`, so this is
  defence in depth). The fifth script, `sqlite_analyze_maintenance.py`, exists
  to touch the live file and is gated as described in §8.2.
* **Disk hygiene:** peak scratch usage 18 GB across four 4.55 GB copies;
  `/mnt/data` never fell below 696 GB free, and every copy was deleted —
  `/mnt/data` is back to **39 G used / 709 G free**, byte-identical to its
  pre-session state. The remaining 8.9 MB of scratch is the JSON evidence,
  also committed under
  `docs/evidence/crypto-query-plan-and-denominator-recovery-001/`. A prior
  session filled this disk; that did not recur.
* **Secrets:** none read, printed or committed. The evidence JSONs were
  scanned for credential-shaped keys before being added.
* **Production state changed:** exactly one thing — `sqlite_stat1` now exists
  on EVO's live database with 130 rows. No `.env`, no systemd unit, no
  migration, no flag, no schema, no application row. One unit
  (`probability-arena-tick-aggregation.service`) entered `failed` state during
  the window and **recovered unaided** on its next firing (§8.5, §8.6); its
  failed state was deliberately not cleared while it stood, so the operator
  would see it. EVO remains on `main` at
  `2c8f75b`; this branch was never installed there (its source was extracted to
  the scratch directory with `git archive` and run with the existing venv
  interpreter).
* **Corrections made after an independent review of the first draft, recorded
  because several of them changed a published number** (all of them are also
  described in the section they affect): an **inverted ratio** in §4.1 that
  read `425/480` and rendered a 1.13× surplus as a `0.88× DEFICIT`; §5.3 having
  **no committed harness or evidence** despite being the milestone's headline
  finding; the §1.3 `V1` variant **testing an empty row set** and a conclusion
  drawn from it; a **sixth changed plan** omitted from a table headed "the five
  plans that actually changed", together with a probe mislabelled IMPROVED that
  had got slower; the §1.6 mechanism query bound to `max(first_seen_at)`
  instead of the real 48 h cutoff; "+12.5 ms" where the table summed to
  +15.9 ms; a wait-improvement range quoted as 29–34× that was 12–34× when
  paired per trial; a path-guard claim describing four guarded scripts when
  three were guarded and the guard did not cover a developer's own database;
  and the arrival sampler in §3.2. Two published figures moved materially as a
  result — safe capacity 1 280 → **1 180 tokens/pass**, and the after-arm worst
  hold 0.261 s → **1.768 s** once ten trials were pooled. Neither changes a
  verdict; both changed the number.
* **A harness bug found and fixed mid-run, recorded because it changed a
  result:** the first coexistence build had no `try/finally` around the pass, so
  when the adversarial arm's reconciler raised `database is locked` the
  competitor child was never told to stop and the parent hung forever at
  interpreter exit. The adversarial arms in §3.4 are from the fixed harness,
  which treats a failing pass as a recorded result and always drains the child.
