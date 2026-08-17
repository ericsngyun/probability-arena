# Probability Arena — reorientation to an alpha-research appliance

**Adopted 2026-08-17.** Supersedes the framing that Probability Arena is "a
frontier agent that predicts markets and trades them."

---

## The thesis

> **A machine-driven quantitative research and execution system in which agents
> discover, contextualize and propose hypotheses, while deterministic
> statistical systems continuously attempt to falsify them, model execution and
> control capital.**

**The LLM is not the trader.** Agents operate where language, semantics and
heterogeneous information matter. The quantitative engine operates where
latency, arithmetic, market state and risk matter. **The LLM never sits in the
synchronous market-data path.**

```
SLOW INTELLIGENCE PLANE          news · semantics · hypothesis agents
            ↓
   HYPOTHESIS REGISTRY
            ↓
  FALSIFICATION / EVAL
            ↓
════════ EVIDENCE GATE ════════
            ↓
  FAST QUANT PLANE               state → flow → liquidity → volatility
            ↓
  expected executable EV → execution + portfolio risk → CAPITAL
```

## Where the project actually is

| capability | state |
|---|---|
| experimental rigor | 9/10 |
| safety / falsification | 9/10 |
| data provenance | 8/10 |
| replay / measurement architecture | 7/10, improving |
| execution modeling | 3/10 |
| market microstructure intelligence | 2/10 |
| **proven alpha** | **0/10** |
| **proven net-positive expectancy** | **0/10** |
| **ready for meaningful real capital** | **0/10** |

**Those zeroes are what the numbers should say.** We are not making progress
toward demonstrated profitability. We are making progress toward *the machinery
capable of determining, without fooling itself, whether a profitable opportunity
exists.* Those are different stages and must not be conflated.

The distinction matters because most of the visible agentic-quant field is
weaker experimentally than this: of 77 audited agentic-trading papers, 19 met a
minimal closed-loop bar, 2 reported time-consistent splits, 1 modelled
transaction costs, 1 documented survivorship, and none reached the top
reproducibility tier.

## The net-edge identity everything reduces to

```
Net Edge = Information Edge + Microstructure Edge
         − Spread − Fees − Impact − Adverse Selection − Inference Cost
```

`Inference Cost` is a real term: whether an agent's reasoning generates enough
incremental trading value to pay for the computation that produced it. It gets
measured, not assumed.

## Candidate alpha sources — four, ranked by agent-suitability

1. **Information latency.** The most naturally agentic. Predict
   `ΔP_{t→t+h} | new information`, not settlement. Plays to reading
   heterogeneous primary sources at scale.
2. **Semantic / structural arbitrage.** The agent supplies *semantics* (do two
   contracts encode equivalent / conditional / mutually exclusive propositions);
   a **deterministic solver** supplies probability theory. Far more defensible
   than asking an LLM for a probability.
3. **Microstructure.** Young electronic markets; short-horizon information in
   flow, depth, cancellations, microprice, adverse selection, time-to-resolution.
   Requires exactly what has been built: correct raw tape, provenance,
   authoritative direction semantics, generations, replay equality.
4. **Execution / market making.** Do **not** assume directional prediction is the
   profit centre. Prediction-market making is formulable as stochastic control
   with binary settlement, inventory risk and time-to-resolution.

## State first, then flow

Order flow adds value **after** conditioning on liquidity state, not instead of
it. So the fast loop is

```
S_t = f(LOB state)        then      P(S_{t+h} | S_t, OFI_t, trades_t, cancels_t, …)
```

**never** `LLM(order book) → BUY`.

## ALPHA-FACTORY-001 — the gate ladder

Every hypothesis `H_i : X_t → Y_{t+h}` enters a registry with dataset hash,
feature version, horizon, baseline, costs, discovery period and an **untouched**
confirmation period, then must survive in order:

| gate | question |
|---|---|
| **G0** data validity | can the signal even be measured correctly? |
| **G1** statistical information | does it predict anything prospectively? |
| **G2** incremental information | does it beat the strongest existing baseline? |
| **G3** economic magnitude | is gross edge > 0? |
| **G4** executable economics | is edge − execution cost > 0? |
| **G5** capacity | does it survive increasing notional? |
| **G6** risk | does it survive drawdown / tail / ruin constraints? |
| **G7** prospective replication | does it work after the hypothesis was frozen? |
| **G8** tiny real-money experiment | — |
| **G9** capital scaling | much later |

**Most hypotheses must die.** Start with linear/logistic, calibrated
classifiers, trees and state-conditioned statistics — complexity graduates only
on incremental *prospective* information. Thousands of cheap experiments beat a
handful of elaborate agent-generated strategies.

## Engineering sequence

**P0** fix `trade → false sequence fault`, snapshot semantics, silence-cap ·
**P1** CP6–CP9 rescoped to DEMO **functional-only** · **P2** generation-aware
`publishable_books()` · **P3** freeze the tape measurement contract ·
**P4** `KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001` (read-only) ·
**P5** Parquet/replay research substrate · **P6** `MARKET-STATE-FABRIC-001` ·
**P7** `ALPHA-FACTORY-001` · **P8** first prospective microstructure experiments.

DEMO proves **connection semantics, reconnects, generations, conservation,
replay equality, archive behaviour, fault handling**. It does **not** prove
traffic capacity or representative microstructure — measured: 98.3% of its
frames come from 194 venue test instruments.

Features are **versioned functions of the immutable tape**:
`feature_set_version + tape_hash → identical result`.

## EVO-X2 as a three-plane appliance

The failure mode is letting inference, backtests and the live collector compete
for the same unified-memory bandwidth. Enforce with real `systemd`/cgroup
slices, not conventions — the orphaned `while True: pass` process that pinned a
core for two hours is why.

| plane | owns | priority |
|---|---|---|
| **LIVE** (collector, archive, sequence state, metrics) | 2–3 pinned cores, highest I/O, **no GPU** | always-on, never starved |
| **QUANT** (replay, Parquet, features, bootstraps, walk-forward) | 8–10 burstable cores | preemptible |
| **INTELLIGENCE** (local LLMs, embeddings, semantics, hypothesis agents) | the GPU | lowest priority vs. data correctness |

**Host cleanliness gate before every benchmark:** load → orphan-process scan →
CPU frequency/temperature → memory pressure → I/O pressure → **noise floor**.

**Inference is benchmarked, not chosen ideologically** (`EVO-INFERENCE-BENCH-001`):
llama.cpp Vulkan (low-latency agent calls) vs llama.cpp ROCm/HIP (prefill-heavy)
vs vLLM ROCm (batched offline). Measure TTFT, decode tok/s, aggregate tok/s,
energy/temperature, memory, concurrency, **and impact on live collector p99**.
NPU is deferred.

## The discipline that follows

Once the production tape is trustworthy, cadence shifts to roughly
**70% edge experiments / 20% measurement improvements / 10% frontier-method
research.**

> Infrastructure work is intellectually addictive because every measurement
> improvement feels productive. After P4/P5, that is the failure mode to guard
> against.

If 20–30 well-preregistered hypotheses against clean production data produce no
economically meaningful prospective edge, **reconsider the venue and the thesis**
rather than building another architectural layer.

The bar that would change everything is modest, not spectacular: e.g.
`E[r_5m | signal] = 8 bps` against a `4 bps` all-in executable hurdle, with
robust prospective replication. Until then:

> **The best trade is still no trade.**
