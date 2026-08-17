# KALSHI-DEMO-TRAFFIC-CAPACITY-001 — preregistration

**Status: PREREGISTERED. Committed BEFORE the probe runs.** The decision rule
and the market pool are frozen here; neither may be revised after seeing rates.

**Read-only. No orders, no portfolio channels, no venue writes, no capital, no
archive qualification run.**

---

## 1. The one question

**Is `100,000 frames in 4 hours` realistically attainable on DEMO?**

This determines whether CP6–CP9 *as currently parameterized* are feasible at
all, so it runs before any further manifest work. `KALSHI-TAPE-MANIFEST-001`
refused on strata separation and could not settle this: its
`top_of_book_change_rate` **saturates at a 150 s interval** (433 of 582 markets
changed at every read), so it bounds message rate only **from below**, uselessly.

## 2. Scope — deliberately narrow

- read-only
- **no archive qualification run**
- no orders
- **fixed small market pool, chosen BEFORE probing**
- **2–5 second** observation cadence
- direct measurement of **actual message/frame arrival**, not the saturated
  150 s proxy
- estimate expected frames over 4 hours
- **report uncertainty, not just a point estimate**
- **stop after answering the question**

## 3. The decision rule — FROZEN

$$N_{4h} = 14{,}400 \sum_i \hat\lambda_i$$

where $\hat\lambda_i$ is the measured frame arrival rate (frames/second) for
market $i$ in the pool, and 14,400 is 4 hours in seconds.

| verdict | condition |
|---|---|
| **REACHABLE** | the **conservative lower bound** is **≥ 125,000** |
| **BORDERLINE** | the **point estimate** clears 100,000 but the lower bound does not |
| **UNREACHABLE** | the **point estimate itself** is below 100,000 |

**The 125,000 threshold is deliberate**, not a rounding of 100,000: it prevents
calling a projected 102k session viable and then spending four hours
discovering that variance pushed it under.

The lower bound must be a stated interval on the *sum*, with its method named
(the rate is a count process; the interval must reflect that, and per-market
rates are not independent within an event).

## 4. The pool — frozen before probing

Chosen to include **both observed regimes**, per
`KALSHI-TAPE-MANIFEST-001-FINDING.md`:

- **all four genuinely high-activity markets** (≥ 1,000 c/min; ranks 1–4)
- **several markets from the plateau** (the 15–17 c/min band holding 30.9% of
  the eligible set)

**The pool must NOT be optimized after seeing rates.** If a chosen market turns
out to be quiet, it stays in and its rate is reported.

Venue test instruments (`KXMAXSHARDINGTEST`, `KXTESTMATCH`) are **excluded**,
per the amendment recorded in
`KALSHI-TAPE-MANIFEST-001-AMENDMENT-TEST-INSTRUMENTS.md`.

## 5. Field-semantics verification — required before measuring

Per doctrine 8, and because this milestone exists partly *because* a field
name lied: **whatever field or event is counted as a "frame" must first be
verified empirically** — re-read across a known interval, observe what moves it,
record the result. `updated_time` looked authoritative and was a definition
timestamp. Do not assume; measure.

## 6. Honesty requirements

- If the answer is **UNREACHABLE**, say so. That is a finding about DEMO and it
  reshapes the qualification design — it is not a failure of the probe.
- If the pool's rates are too variable to bound usefully, **report the interval
  and say the question is unsettled**, rather than presenting a point estimate
  as an answer.
- Record what could not be determined.
- A DEMO rate is **not** a production rate. Nothing here licenses a claim about
  production capacity.

## 7. Deviations

Any departure must be recorded here with reason and timestamp **before** the
affected result is reported.

Each entry says whether it was decided **before or after any rate was seen**,
because that is the only property that determines whether it could have been
chosen to produce an outcome.

### D1 — channel set: three channels, not the shipped default of two
**Decided before any rate was seen** (2026-08-17, in the probe's first commit,
`74bc4cd`). `collector.DEFAULT_CHANNELS` is `("orderbook_delta", "ticker")`;
the probe subscribes to `("orderbook_delta", "ticker", "trade")`. `trade` is
inside `kalshi.ALLOWED_CHANNELS` and is read-only market data. **Reason:** the
preregistration does not name a channel set, and adding a channel can only
*raise* the measured rate — so the maximal read-only market-data configuration
is the conservative choice against a false `UNREACHABLE`. The per-channel
breakdown is reported so the two-channel subtotal is recoverable.

### D2 — plateau sample size fixed at 8
**Decided before any rate was seen** (`ee5ee75`, before the first connection).
§4 says "several markets from the plateau" without a number. Eight was chosen
so the pool totals 12, matching the qualification session's `universe_size`,
which makes `N_4h` directly comparable to the session this probe exists to
size.

### D3 — `read_timeout_s` raised above the session length for measurement runs
**Decided before any rate was seen**, after a 60-second single-market pilot
(`74bc4cd`). At the transport's shipped 60 s default, one quiet minute trips
`TransportReadTimeout`, the collector reconnects, and the resubscribe replays
one `orderbook_snapshot` per market — so a silent venue would be measured as a
mildly active one and the frames would be our own reconnect ladder. **Reason:**
silence must stay silent for the count to mean anything. The primary run has
`reconnects = 0`.

### D4 — the replication run uses a 90 s read timeout and 200 reconnects
**Decided AFTER the primary rates were seen** (2026-08-17T07:58Z), and stated
as such. A 3,600-second-capped replication launched at 06:34:43Z was still
running frameless at 07:57Z, because `_cap_check()` is reached only from
`_handle_frame` and therefore no cap can fire while the venue is quiet. It was
killed and relaunched with a bounded read timeout so that it would terminate.
**This reintroduces exactly the artefact D3 removed**, which is why the
replication is reported in both arms and its conclusion rests on the
`continuous_frames_only` arm — snapshots and acks excluded, so the reconnect
ladder cannot contribute to it. It changes no primary number.

### D5 — supplementary non-pool control runs
**The controls' composition was decided before their rates were seen**; the
decision to run controls at all was made after the pool's first window came
back near-zero (2026-08-17T06:07Z). Three subscriptions outside the frozen pool
were observed: the venue test instruments (194), the top 200 eligible non-test
markets, and all 388 eligible non-test markets. **None of them enters the
decision rule** — `N_4h` and the verdict are computed on the frozen pool alone.
They exist to answer two questions the pool cannot: whether the counter moves
when frames exist (doctrine 7), and whether a quiet pool means a quiet venue.
Their ticker lists are mechanical filters over the same committed manifest
artifact (`scripts/kalshi_demo_capacity_control_lists.py`), not selections.

### D6 — the pool was probed ~26 hours after the snapshot it was frozen from
**Not a choice.** The manifest snapshot is `2026-08-16T04:01Z`; the probe ran
`2026-08-17T05:51Z`. The pool could not be re-frozen against a fresher snapshot
without choosing markets after seeing the venue move, which §4 forbids. The
confound is recorded in the finding (§7.2) rather than fixed.
