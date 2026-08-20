# REALIZED-FILL-CORPUS-001 — measurement contract

**Branch `REALIZED-FILL-CORPUS-001`. Not merged. No capital, no execution.**

> **STATUS: PLAN COMMITTED, IMPLEMENTATION IN PROGRESS.**
> This header block is the plan required before coding. Sections below are
> filled in as each unit lands.

---

## 0. Why this milestone exists

Three independently written specs each concluded they cannot function without
the same missing artifact, and each says so in its own words:

| spec | where | what it says it needs |
|---|---|---|
| `RISK-GOVERNOR-001` | §10 | three missing models — **fill**, **markout**, **verified cost** — and names them "one dependency, not three caveats: a **realized-fill corpus**". Until it exists the governor is `UNCALIBRATED` and its only trustworthy output is `NO_TRADE`. §11 makes "a fill corpus begins to exist" the entry evidence for the TINY CAPITAL rung. |
| `ALPHA-FACTORY-001` | §5.3 (G4) | *"Fee schedule not verified against venue documentation **and realized fills** → the gate is `UNEVALUATED`, never `PASSED`."* The economic gate bottoms out in human attestation with no machine-checkable truth source. |
| `VOLATILITY-STATE-ENGINE-001` | §3 (R4), §9 | R4 *toxic flow* is `NOT_COMPUTABLE:no_fill_history` — it requires *"a markout model over our own fills, and we have never traded"*. |

The scale is set by `ALPHA-FACTORY-001`: EDGE-DISCOVERY-001's E2 was a real,
out-of-sample-replicating lead at **70% of its cost floor**. A cost model 30%
optimistic converts our one genuine historical finding into a false graduate.
That is the error this corpus exists to bound.

**What this milestone builds is the machinery and its verification, not the
data.** See §9 — populating `eps_fill` with our own realized fills requires
capital-funded calibration trades that are not authorized.

---

## 1. Plan (committed before implementation)

**Objective.** A typed canonical fill record, a Solana confirmed-transaction
decoder that derives actual amounts from **balance deltas** rather than logs, a
fee/tip separation, quote→fill linkage, markout labeling, the two calibration
quantities, real pinned historical fixtures with provenance and a drift
detector, positive **and** negative controls, and this contract.

**Affected files (all new unless stated).**

```
app/fills/__init__.py           package
app/fills/absence.py            typed absence (doctrine 10)
app/fills/schema.py             canonical fill record + enums
app/fills/decoder.py            balance-delta transaction decoder
app/fills/fees.py               network / priority / tip separation
app/fills/linkage.py            quote -> fill linkage
app/fills/markout.py            markout labeling + price sources
app/fills/calibration.py        eps_fill, AS_h
app/fills/corpus.py             corpus assembly + integrity
app/adapters/solana_rpc.py      READ-ONLY getTransaction adapter
app/cli.py                      (edit) reachability: CLI verbs
tests/fixtures/solana_fills/    pinned real transactions + provenance
tests/test_realized_fill_*.py   controls
docs/milestones/REALIZED-FILL-CORPUS-001.md   this contract
scripts/fetch_realized_fill_fixtures.py       provenance-recording fetcher
```

**Risks.**

1. *Identifier collision with the safety audit.* `app/services/frontier_eval.py`
   bans the fragments `wallet`, `swap`, `jupiter`, `sign_transaction`,
   `send_transaction`, `keypair`, `private_key`, … as identifiers anywhere in
   `app/`. This milestone deliberately uses none of them — the domain words are
   *route*, *leg*, *fee payer*, *owner account*, *venue program*. No allowlist
   entry is requested and none should be needed.
2. *Reachability (doctrine 5).* A decoder with green unit tests and no caller is
   the CP4 failure. Mitigated by CLI verbs plus a seam test that drives the real
   collaborator.
3. *Fixture drift (doctrine 9).* Pinned mainnet transactions are immutable once
   finalized, but the RPC *representation* of them is not — encodings, field
   presence and `maxSupportedTransactionVersion` behaviour move. Mitigated by a
   content hash over a canonicalized subset plus a drift-detector test.
4. *Absence encoding (doctrine 10).* An unobserved tip is not a zero tip. All
   absence is structural, never `0.0`.
5. *Wrong cost basis.* The dominant risk. See §12.

**Validation plan.** Positive controls (force the condition, prove the metric
moves), negative controls (corrupt a field, feed a failed transaction, feed a
multi-hop route where naive log parsing is wrong — the decoder must FAIL), a
drift detector, and a reachability seam test. Counts reported in §11.

**Boundary.** Nothing in this milestone can construct, simulate, sign, submit or
relay a transaction. The only network verb is `getTransaction` against a free
public RPC endpoint, over already-confirmed history. See §9 and §10.
