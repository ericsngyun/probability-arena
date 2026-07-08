# XVENUE_OBSERVATION_RUNBOOK — cross-venue observation windows (XVENUE-OBS-001)

Read-only procedure for answering one question during a high-overlap window
(World Cup semifinal/final, a full MLB slate, an election night): **do Kalshi and
Polymarket list clean comparable markets — the same proposition on both venues —
after a targeted Polymarket scan?**

Everything here is observation and measurement for human review. It does **not**
compute EV, identify or label arbitrage, recommend trades, paper trade, size
positions, place orders, or use wallets/private keys/signing/swaps/execution.
A clean comparable is a *coverage* fact (the venues met), never an opportunity.
Manual only: `ENABLE_POLYMARKET_SCOUT` stays false and no timer exists.

## The sequence (run manually, in order)

```bash
# 1. Refresh Polymarket supply for the window (read-only public GETs, bounded).
#    --targeted derives queries from persisted Kalshi titles/tickers (no LLM);
#    add a resolution window matching the slate so futures don't crowd out games.
.venv/bin/python -m app.cli polymarket-scan-once --targeted --limit 400 --orderbook-limit 100 \
    --end-date-min <slate-start-ISO> --end-date-max <slate-end-plus-margin-ISO>

# 2. Census: does the sample have the structural prerequisites for comparison?
.venv/bin/python -m app.cli polymarket-coverage-report --top 20

# 3. Match (defaults are recency-aware + representative since XVENUE-OPS-001;
#    the run prints its own sample composition + domain overlap).
.venv/bin/python -m app.cli cross-venue-match-once
#    …or narrowed to the slate:
.venv/bin/python -m app.cli cross-venue-match-once --recent-hours 48 --domain sports

# 4. Full label/measurement report.
.venv/bin/python -m app.cli cross-venue-report

# 5. Inspect candidates by label.
.venv/bin/python -m app.cli cross-venue-candidates --label comparable_market_candidate
.venv/bin/python -m app.cli cross-venue-candidates --label low_confidence_match
.venv/bin/python -m app.cli cross-venue-candidates --label unresolved_semantic_match

# 6. One-screen window verdict: clean vs flagged comparables, side-uncertain
#    counts, mismatch reasons, and an overlap assessment.
.venv/bin/python -m app.cli xvenue-observation-report
```

Read the result in this order: `overlap assessment` first, then
`comparable clean/flagged`, then `mismatch reasons` (they say WHY candidates fell
short: different market types, unalignable sides, resolution gaps).

## Interpreting the overlap assessment

| assessment | meaning | next step |
|---|---|---|
| `clean_comparable_present` | ≥1 comparable with no review flag — the venues listed the same proposition | re-observe after the next scan; note the pair(s) in the window log |
| `overlap_no_clean_comparable` | venues met (≥10 candidates) but every comparable is flagged / sides unalignable | read `mismatch_reasons`; usually a market-type mismatch (props vs futures) — not fixable by scanning more |
| `insufficient_overlap` | <10 candidates — the venues barely met | widen the scan (`--limit`, `--targeted`, slate-matched `--end-date-*`) before concluding anything |
| `no_match_run` / `no_scan_data` | pipeline not run in order | run the missing step |

A comparable row flagged `large_observed_difference_requires_review` is a
**suspicious match or a stale quote** — evidence about match quality, never an
opportunity. Side-uncertain rows (`outcome_side_uncertain` /
`midpoint_side_uncertain`) are structurally honest: Kalshi encodes game-market
Yes-sides in the ticker suffix, so some pairs cannot be aligned from titles alone.

## Domain / window guidance (grounded in measured live data, 2026-07-08)

**World Cup (best near-term window: semifinals Jul 9–11, final ~Jul 19-20).**
Kalshi lists ~1,100 active `KXWC*` game markets (winners `KXWCGAME`, totals,
spreads, exact scores) but **no tournament-winner series**; Polymarket lists both
outcome futures ("Will France win the World Cup?") AND per-game markets
("France vs. Morocco", `["France","Morocco"]` outcomes) during game windows.
**Game-winner ↔ game-winner is the realistic clean-comparable shape.** Scan with
`--targeted` plus `--end-date-min/max` bracketing the game days; expect
side-uncertainty on Kalshi titles that name both teams ("X vs Y Winner?" — the
Yes side lives in the ticker suffix), while "Will \<team\> win …?" titles align.

**MLB (daily slates).** Kalshi's largest active family (~9,100 `KXMLB*`), but
heavily player props (HRR/TB/HIT/KS) which can never match; the comparable shape
is `KXMLBGAME` game-winner ↔ Polymarket moneyline ("Royals vs. Mets"). Most
Polymarket MLB search supply is season/draft futures — use a tight
`--end-date-max` (day + margin) to keep game markets in the sample. KXMLBGAME
titles name both teams, so expect `outcome_side_uncertain` unless the title names
the Yes team; these appear as unresolved, honestly.

**Politics / elections.** Polymarket supply is deep (150+ candidate-winner rows
after a targeted scan); Kalshi's active politics supply is currently thin
(~10–40 rows in a recency sample) and resolution dates rarely coincide
(`resolution_gap_days` dominates). Treat as a long-dated census domain — the
coverage report matters more than the matcher until Kalshi lists matching races.

**Crypto.** Currently **no comparable supply**: the coverage census shows ~0–8
Polymarket crypto rows and ~2 Kalshi crypto rows. Don't spend scan budget here;
re-check the census after Kalshi lists BTC/ETH series.

**Tennis (only if same market types exist).** Kalshi lists match-winner series
(`KXITF*`/`KXATP*`/`KXWTA*`, ~1,160 active) plus set-level series
(`KXATPSETWINNER`, `KXATPGSPREAD`); Polymarket tennis supply is sparse (single
Wimbledon markets). The one live comparable produced so far —
`KXATPSETWINNER ↔ Wimbledon ATP match market` — was correctly **flagged for
review** (set-winner vs match-market, gap 0.51). Only match-winner ↔
match-winner counts as clean; anything else should (and does) get rejected or
flagged.

## Boundaries (unchanged)

The full pipeline is read-only: public GETs on the scan, persisted-row matching,
derived reporting. No EV, no arbitrage/arb/opportunity labels, no paper trading,
no recommendations, no sizing, no orders, no wallets/keys/signing/swaps/
execution/autonomy. `ENABLE_POLYMARKET_SCOUT` stays false; no timer; the
scheduled MarketOps/EDGE-AUTO/MEME-NEWS lanes are untouched.
