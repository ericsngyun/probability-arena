"""The frozen MARKET-MICROSTRUCTURE-EDGE-001 evaluator. Twelve cells, once.

Developed against synthetic and VALIDATION data only. Every read of research
rows goes through `authorization.require_readable`, so touching
`dataset_role=CONFIRMATION` raises until the operator writes a signed
authorization -- the preregistration discipline is a code path, not a memory.

Nothing here is tunable. The cells, the horizons, the feature membership, the
split mechanics, the embargo, the FDR level and the verdict rules are all fixed
by the preregistration, and the tests hold each of them.

This module evaluates the PRIMARY question only. TTE heterogeneity lives in a
separate module with its own family, so that a heterogeneity result can never
be reached through this code path and can never rescue a primary failure.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, asdict, field

from app.microstructure import features as F
from app.microstructure.authorization import require_readable
from app.microstructure.linalg import mean, predict, ridge_fit
from app.microstructure.rows import LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION
from app.microstructure.panel import SESSION_OK

# ---------------------------------------------------------------------------
# The frozen family. Twelve cells: 3 comparisons x 4 horizons (prereg §3, §7).
# ---------------------------------------------------------------------------
CMP_M0_VS_RANDOM_WALK = "M0_vs_random_walk"      # positive control (§3.1)
CMP_M0_VS_MICROPRICE = "M0_vs_microprice"        # strong contemporaneous baseline
CMP_M1_VS_M0 = "M1_vs_M0"                        # THE research question
COMPARISONS = (CMP_M0_VS_RANDOM_WALK, CMP_M0_VS_MICROPRICE, CMP_M1_VS_M0)

HORIZONS_S = (1, 5, 30, 300)
PRIMARY_HORIZON_S = 30
CELLS = tuple((c, h) for c in COMPARISONS for h in HORIZONS_S)

FDR_Q = 0.10
EMBARGO_S = 300          # >= the maximum horizon (§4)
BOOTSTRAP_DRAWS = 1_000
RIDGE_LAMBDA = 1e-6

#: A cell without this much support is reported UNDERPOWERED -- never dropped,
#: never silently passed, and never counted as a null.
MIN_TEST_ROWS = 200
MIN_TEST_CLUSTERS = 20

VERDICT_VOID = "VOID_POSITIVE_CONTROL_FAILED"
VERDICT_FLOW_NOT_ADDITIVE = "FLOW_NOT_ADDITIVE_LANE_STOPS"
VERDICT_REAL_BUT_UNECONOMIC = "REAL_BUT_UNECONOMIC"
VERDICT_CANDIDATE = "CANDIDATE_FOR_PROSPECTIVE_TEST"
VERDICT_NO_ELIGIBLE_DATA = "REFUSED_NO_ELIGIBLE_DATA"
UNDERPOWERED = "UNDERPOWERED"


class CorpusRefused(RuntimeError):
    """The corpus cannot support the frozen evaluation. Never a soft pass."""


@dataclass(frozen=True)
class CellResult:
    comparison: str
    horizon_s: int
    test_rows: int
    test_clusters: int
    delta_loss: float | None          # baseline_loss - model_loss; >0 = better
    r2_vs_baseline: float | None
    p_value: float | None
    underpowered: bool
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Corpus verification
# ---------------------------------------------------------------------------

def verify_corpus(rows: list[dict]) -> dict:
    """Role, schema and integrity. Raises rather than degrading."""
    if not rows:
        raise CorpusRefused("corpus is empty; there is nothing to evaluate")
    roles = {r["dataset_role"] for r in rows}
    for role in sorted(roles):
        require_readable(role, context="EDGE-001 evaluation")
    if len(roles) != 1:
        raise CorpusRefused(f"corpus mixes dataset roles {sorted(roles)}; "
                            "an evaluation must not pool roles")
    bad_schema = {(r["feature_schema_version"], r["label_schema_version"])
                  for r in rows} - {(ROW_SCHEMA_VERSION, LABEL_SCHEMA_VERSION)}
    if bad_schema:
        raise CorpusRefused(f"corpus carries foreign schema versions {bad_schema}")
    halted = {r["session_id"] for r in rows if r["session_status"] != SESSION_OK}
    if halted:
        raise CorpusRefused(f"sessions {sorted(halted)} are not {SESSION_OK}; "
                            "a halted session can never be confirmation data")
    return {"rows": len(rows), "role": roles.pop(),
            "sessions": len({r["session_id"] for r in rows}),
            "clusters": len({cluster_of(r) for r in rows})}


def cluster_of(row: dict) -> tuple:
    """The inference unit: event/market, NOT the row (§4, Amendment 2 §F)."""
    return (row["event_id"], row["ticker"])


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def walk_forward_folds(rows: list[dict]) -> list[tuple[list[dict], list[dict]]]:
    """Train on session *k*, test on session *k+1*. Never the reverse (§4)."""
    by_session: dict = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    order = sorted(by_session, key=lambda s: min(
        x["sample_time_ms"] for x in by_session[s]))
    return [(by_session[order[i]], by_session[order[i + 1]])
            for i in range(len(order) - 1)]


def purge_and_embargo(train: list[dict], test: list[dict],
                      horizon_s: int) -> list[dict]:
    """Drop training rows whose LABEL could overlap the test window.

    The embargo is the maximum horizon, not this cell's horizon, so every cell
    uses the same gap and a short-horizon cell cannot quietly get more data
    than a long-horizon one.
    """
    if not test:
        return []
    test_start = min(r["sample_time_ms"] for r in test)
    cutoff = test_start - EMBARGO_S * 1000
    return [r for r in train if r["sample_time_ms"] + horizon_s * 1000 <= cutoff]


# ---------------------------------------------------------------------------
# Design matrices
# ---------------------------------------------------------------------------

def feature_names(comparison: str) -> tuple[str, ...]:
    if comparison == CMP_M1_VS_M0:
        return F.M1_FEATURES
    return F.M0_FEATURES


def assert_membership() -> None:
    """M0 must be a strict subset of M1, and the difference exactly the flow set."""
    if not F.m0_is_subset_of_m1():
        raise CorpusRefused(
            "M0 is not a strict subset of M1, or the difference is not exactly "
            "the preregistered flow set; the nesting the comparison depends on "
            "is broken")


def design(rows: list[dict], names: tuple[str, ...], horizon_s: int):
    """Rows with a usable label and complete features, as (X, y, clusters)."""
    X, y, cl = [], [], []
    for r in rows:
        lab = r["labels"][str(horizon_s)]
        if not lab["available"]:
            continue
        vals = []
        block = {**r["m0"], **r["m1_flow"]}
        ok = True
        for n in names:
            v = block.get(n)
            if v is None:
                ok = False
                break
            vals.append(float(v))
        if not ok:
            continue
        X.append([1.0] + vals)
        y.append(float(lab["value"]))
        cl.append(cluster_of(r))
    return X, y, cl


def _micro_drift(rows: list[dict], names: tuple[str, ...],
                 horizon_s: int) -> list[float]:
    """`micro_minus_mid` for exactly the rows `design` kept, in the same order."""
    out = []
    for r in rows:
        lab = r["labels"][str(horizon_s)]
        if not lab["available"]:
            continue
        block = {**r["m0"], **r["m1_flow"]}
        if any(block.get(n) is None for n in names):
            continue
        out.append(float(block["micro_minus_mid"]))
    return out


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def cluster_bootstrap_p(deltas: list[float], clusters: list[tuple],
                        draws: int = BOOTSTRAP_DRAWS, seed: int = 20260824) -> float:
    """One-sided p for `mean(delta) > 0`, resampling CLUSTERS not rows.

    Resampling rows would treat thousands of 1 Hz observations inside one
    market as independent and shrink the interval by roughly the square root of
    the rows-per-cluster ratio -- which is how a null becomes significant.
    """
    groups: dict = {}
    for d, c in zip(deltas, clusters):
        groups.setdefault(c, []).append(d)
    keys = list(groups)
    if len(keys) < 2:
        return 1.0
    observed = mean(deltas)
    rng = random.Random(seed)
    ge = 0
    for _ in range(draws):
        pick = [groups[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [v for g in pick for v in g]
        # centred: how often does a null-shifted resample reach the observation?
        if mean(flat) - observed >= observed:
            ge += 1
    return (ge + 1) / (draws + 1)


def benjamini_hochberg(pvals: dict, q: float = FDR_Q) -> dict:
    """BH over the WHOLE frozen family, computed once (§7)."""
    items = [(k, p) for k, p in pvals.items() if p is not None]
    m = len(items)
    if not m:
        return {k: False for k in pvals}
    items.sort(key=lambda kv: kv[1])
    thresh, k_max = None, -1
    for i, (_k, p) in enumerate(items, start=1):
        if p <= q * i / m:
            k_max, thresh = i, p
    passing = {k for k, _ in items[:k_max]} if k_max > 0 else set()
    return {k: (k in passing) for k in pvals}


def _sse(pred: list[float], y: list[float]) -> float:
    return sum((p - t) ** 2 for p, t in zip(pred, y))


def evaluate_cell(rows: list[dict], comparison: str, horizon_s: int) -> CellResult:
    """One frozen cell, walk-forward, purged and embargoed."""
    names = feature_names(comparison)
    d_all, c_all = [], []
    sse_base = sse_model = 0.0

    for train_rows, test_rows in walk_forward_folds(rows):
        train_rows = purge_and_embargo(train_rows, test_rows, horizon_s)
        Xtr, ytr, _ = design(train_rows, names, horizon_s)
        Xte, yte, cte = design(test_rows, names, horizon_s)
        if len(Xtr) <= len(names) + 1 or not Xte:
            continue

        if comparison == CMP_M1_VS_M0:
            # nested: the baseline is M0 fitted on the SAME rows, so the only
            # difference between arms is the flow block
            m0 = F.M0_FEATURES
            X0tr, y0tr, _ = design(train_rows, m0, horizon_s)
            X0te, _, _ = design(test_rows, m0, horizon_s)
            if len(X0tr) != len(Xtr) or len(X0te) != len(Xte):
                continue
            base = predict(X0te, ridge_fit(X0tr, y0tr, RIDGE_LAMBDA))
        elif comparison == CMP_M0_VS_RANDOM_WALK:
            base = [0.0] * len(Xte)
        else:
            base = _micro_drift(test_rows, names, horizon_s)
            if len(base) != len(Xte):
                continue

        model = predict(Xte, ridge_fit(Xtr, ytr, RIDGE_LAMBDA))
        # per-observation loss difference: positive means the model is better
        d_all += [(b - t) ** 2 - (m - t) ** 2
                  for b, m, t in zip(base, model, yte)]
        c_all += cte
        sse_base += _sse(base, yte)
        sse_model += _sse(model, yte)

    n, k = len(d_all), len(set(c_all))
    if n == 0:
        return CellResult(comparison, horizon_s, 0, 0, None, None, None, True,
                          "no fold produced testable rows")
    r2 = (1.0 - sse_model / sse_base) if sse_base > 0 else None
    under = n < MIN_TEST_ROWS or k < MIN_TEST_CLUSTERS
    # An underpowered cell gets NO p-value, so it can neither pass nor be
    # counted as a null -- it is reported as underpowered and nothing else.
    p = None if under else cluster_bootstrap_p(d_all, c_all)
    return CellResult(comparison, horizon_s, n, k, mean(d_all), r2, p, under,
                      UNDERPOWERED if under else None)


def evaluate(rows: list[dict]) -> dict:
    """The whole frozen evaluation. Twelve cells, one BH, one verdict."""
    assert_membership()
    corpus = verify_corpus(rows)
    if corpus["clusters"] == 0:
        raise CorpusRefused("no event/market clusters survive; refusing")

    results = {}
    for comparison, horizon in CELLS:
        results[(comparison, horizon)] = evaluate_cell(rows, comparison, horizon)

    # BH ONCE over the whole family -- never per horizon, never per comparison
    pvals = {k: r.p_value for k, r in results.items()}
    passing = benjamini_hochberg(pvals, FDR_Q)

    def _report(verdict: str, reason: str) -> dict:
        """One shape for every outcome.

        The refusal path previously returned a dict missing `underpowered_cells`
        and `passes_fdr`, so a caller that read either got a KeyError exactly
        when the evaluation had refused -- the moment it most needs to be
        readable.
        """
        return {
            "milestone": "MARKET-MICROSTRUCTURE-EDGE-001",
            "corpus": corpus, "family_size": len(CELLS), "fdr_q": FDR_Q,
            "cells": {f"{c}@{h}s": {**r.to_dict(), "passes_fdr": passing[(c, h)]}
                      for (c, h), r in results.items()},
            "underpowered_cells": [f"{c}@{h}s" for (c, h), r in results.items()
                                   if r.underpowered],
            "verdict": verdict, "reason": reason,
        }

    if all(r.test_rows == 0 for r in results.values()):
        return _report(VERDICT_NO_ELIGIBLE_DATA,
                       "no cell produced a single testable row")

    control = results[(CMP_M0_VS_RANDOM_WALK, PRIMARY_HORIZON_S)]
    primary = results[(CMP_M1_VS_M0, PRIMARY_HORIZON_S)]

    if control.underpowered or not passing[(CMP_M0_VS_RANDOM_WALK,
                                            PRIMARY_HORIZON_S)]:
        verdict, reason = VERDICT_VOID, (
            "positive control failed at the primary horizon: M0 did not beat a "
            "mid random walk, so the pipeline is suspect and no other result "
            "may be read")
    elif primary.underpowered:
        verdict, reason = VERDICT_NO_ELIGIBLE_DATA, (
            "the primary cell is UNDERPOWERED; reported as such rather than "
            "as a null")
    elif not passing[(CMP_M1_VS_M0, PRIMARY_HORIZON_S)]:
        verdict, reason = VERDICT_FLOW_NOT_ADDITIVE, (
            "M1 does not beat M0 out-of-sample at FDR 10% on the primary "
            "horizon; order flow is declared non-additive and this lane stops")
    else:
        verdict, reason = VERDICT_CANDIDATE, (
            "M1 beats M0 at FDR 10% on the primary horizon; the §6 cost floor "
            "and §5 noise floors gate anything further")

    return _report(verdict, reason)
