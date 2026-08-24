"""MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001 — the SECONDARY family.

A separate module with a separate code path, a separate confirmation lock and
a separate Benjamini-Hochberg family. There is deliberately **no flag on the
primary evaluator that runs this**: heterogeneity must not be reachable from
the primary path, because a result reached there could be mistaken for one that
bears on the primary verdict. It does not.

**This analysis cannot rescue a failed EDGE-001 primary result.** If the
primary says order flow is non-additive, that conclusion stands, and a positive
bin here generates a new prospective hypothesis rather than reinstating the old
one. `evaluate()` returns no primary verdict and has no way to express one.

The split mechanics, embargo, clustering and design matrices are imported from
the primary evaluator rather than reimplemented -- the heterogeneity test must
use the *same* discipline, and a second copy would be free to drift.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass

from app.microstructure import features as F
from app.microstructure.authorization import require_tte_readable
from app.microstructure.evaluate import (
    CMP_M0_VS_MICROPRICE, CMP_M0_VS_RANDOM_WALK, CMP_M1_VS_M0, COMPARISONS,
    CorpusRefused, EMBARGO_S, HORIZONS_S, PRIMARY_HORIZON_S, RIDGE_LAMBDA,
    _micro_drift, benjamini_hochberg, cluster_of, design, feature_names,
    purge_and_embargo, walk_forward_folds,
)
from app.microstructure.linalg import mean, predict, ridge_fit
from app.microstructure.panel import (
    SESSION_OK, TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION, TTE_LIVE_EVENT,
    TTE_NEAR_EVENT, tte_bin,
)
from app.microstructure.rows import LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION

MILESTONE = "MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001"

#: All five, always, in fixed order. Never merged, split, reweighted or
#: dropped -- adaptively or otherwise (prereg §6).
FROZEN_BINS = (TTE_FAR, TTE_APPROACHING, TTE_NEAR_EVENT, TTE_LIVE_EVENT,
               TTE_LATE_RESOLUTION)

#: 3 comparisons x 4 horizons, mirroring the primary's structure -- twelve
#: OMNIBUS tests, not sixty cellwise ones.
CELLS = tuple((c, h) for c in COMPARISONS for h in HORIZONS_S)
PRIMARY_HETEROGENEITY_CELL = (CMP_M1_VS_M0, PRIMARY_HORIZON_S)

FDR_Q = 0.10
BOOTSTRAP_DRAWS = 1_000

#: Per-bin support floor (prereg §6), from S01's measured yield.
MIN_BIN_BLOCKS = 100
MIN_BIN_CLUSTERS = 20

UNDERPOWERED = "UNDERPOWERED"
VERDICT_NO_ELIGIBLE_DATA = "REFUSED_NO_ELIGIBLE_DATA"
VERDICT_NO_HETEROGENEITY = "NO_HETEROGENEITY_DETECTED"
VERDICT_HETEROGENEITY_PRESENT = "HETEROGENEITY_PRESENT_NEW_HYPOTHESIS_REQUIRED"

CANNOT_RESCUE = (
    "This is a SECONDARY analysis. It cannot rescue a failed EDGE-001 primary "
    "result. A surviving omnibus yields descriptive bin estimates and requires "
    "a NEW prospective preregistration under a new name.")


@dataclass(frozen=True)
class OmnibusResult:
    comparison: str
    horizon_s: int
    bin_means: dict            # bin -> mean delta-loss, or None
    bin_blocks: dict           # bin -> rows contributing
    bin_clusters: dict         # bin -> event/market clusters
    underpowered_bins: tuple
    statistic: float | None    # between-bin sum of squares
    p_value: float | None
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def verify_corpus(rows: list[dict]) -> dict:
    """Independent of the primary's verifier, and independently locked."""
    if not rows:
        raise CorpusRefused("corpus is empty")
    roles = {r["dataset_role"] for r in rows}
    for role in sorted(roles):
        require_tte_readable(role, context="TTE-HETEROGENEITY-001")
    if len(roles) != 1:
        raise CorpusRefused(f"corpus mixes dataset roles {sorted(roles)}")
    bad = {(r["feature_schema_version"], r["label_schema_version"])
           for r in rows} - {(ROW_SCHEMA_VERSION, LABEL_SCHEMA_VERSION)}
    if bad:
        raise CorpusRefused(f"corpus carries foreign schema versions {bad}")
    halted = {r["session_id"] for r in rows if r["session_status"] != SESSION_OK}
    if halted:
        raise CorpusRefused(f"sessions {sorted(halted)} are not {SESSION_OK}")
    return {"rows": len(rows), "role": roles.pop(),
            "sessions": len({r["session_id"] for r in rows}),
            "clusters": len({cluster_of(r) for r in rows})}


def _bin_of_row(row: dict) -> str:
    return tte_bin(float(row["TTE_seconds"]))


def _fold_deltas(rows: list[dict], comparison: str, horizon_s: int):
    """Per-observation loss differences, tagged with bin and cluster.

    Identical split, purge, embargo and fitting discipline as the primary --
    imported, not reimplemented.
    """
    names = feature_names(comparison)
    out = []
    for train_rows, test_rows in walk_forward_folds(rows):
        train_rows = purge_and_embargo(train_rows, test_rows, horizon_s)
        Xtr, ytr, _ = design(train_rows, names, horizon_s)
        Xte, yte, cte = design(test_rows, names, horizon_s)
        if len(Xtr) <= len(names) + 1 or not Xte:
            continue
        if comparison == CMP_M1_VS_M0:
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
        kept = [r for r in test_rows
                if r["labels"][str(horizon_s)]["available"]
                and all({**r["m0"], **r["m1_flow"]}.get(n) is not None
                        for n in names)]
        if len(kept) != len(Xte):
            continue
        for r, b, m, t, c in zip(kept, base, model, yte, cte):
            out.append(((b - t) ** 2 - (m - t) ** 2, _bin_of_row(r), c))
    return out


def _between_bin_ss(groups: dict) -> float | None:
    """Between-bin sum of squares -- the omnibus statistic."""
    present = {b: v for b, v in groups.items() if v}
    if len(present) < 2:
        return None
    allv = [x for v in present.values() for x in v]
    grand = mean(allv)
    return sum(len(v) * (mean(v) - grand) ** 2 for v in present.values())


def omnibus_p(tagged, draws: int = BOOTSTRAP_DRAWS, seed: int = 20260824):
    """Cluster bootstrap under the null that every bin shares one mean.

    Each observation is recentred by its OWN bin mean before resampling, so the
    null of equality holds by construction in the bootstrap world; the observed
    statistic is then compared against that distribution. Clusters, not rows,
    are resampled -- resampling rows would treat thousands of 1 Hz observations
    inside one market as independent.
    """
    groups: dict = {b: [] for b in FROZEN_BINS}
    by_cluster: dict = {}
    for d, b, c in tagged:
        groups[b].append(d)
        by_cluster.setdefault(c, []).append((d, b))
    observed = _between_bin_ss(groups)
    if observed is None:
        return None, groups
    bin_mean = {b: mean(v) for b, v in groups.items() if v}
    keys = list(by_cluster)
    if len(keys) < 2:
        return None, groups
    rng = random.Random(seed)
    ge = 0
    for _ in range(draws):
        g: dict = {b: [] for b in FROZEN_BINS}
        for _ in keys:
            for d, b in by_cluster[keys[rng.randrange(len(keys))]]:
                g[b].append(d - bin_mean[b])          # recentred: null is true
        s = _between_bin_ss(g)
        if s is not None and s >= observed:
            ge += 1
    return (ge + 1) / (draws + 1), groups


def evaluate_omnibus(rows: list[dict], comparison: str,
                     horizon_s: int) -> OmnibusResult:
    tagged = _fold_deltas(rows, comparison, horizon_s)
    if not tagged:
        empty = {b: None for b in FROZEN_BINS}
        return OmnibusResult(comparison, horizon_s, empty,
                             {b: 0 for b in FROZEN_BINS},
                             {b: 0 for b in FROZEN_BINS}, FROZEN_BINS,
                             None, None, "no fold produced testable rows")
    p, groups = omnibus_p(tagged)
    clusters: dict = {b: set() for b in FROZEN_BINS}
    for _d, b, c in tagged:
        clusters[b].add(c)
    blocks = {b: len(groups[b]) for b in FROZEN_BINS}
    ncl = {b: len(clusters[b]) for b in FROZEN_BINS}
    # Underpowered bins are REPORTED. They are never merged away, never
    # dropped, and they still enter the omnibus.
    under = tuple(b for b in FROZEN_BINS
                  if blocks[b] < MIN_BIN_BLOCKS or ncl[b] < MIN_BIN_CLUSTERS)
    means = {b: (mean(groups[b]) if groups[b] else None) for b in FROZEN_BINS}
    return OmnibusResult(comparison, horizon_s, means, blocks, ncl, under,
                         _between_bin_ss(groups), p,
                         UNDERPOWERED if under else None)


def evaluate(rows: list[dict]) -> dict:
    """Twelve omnibus tests, one BH family, no primary verdict of any kind."""
    corpus = verify_corpus(rows)
    results = {c: evaluate_omnibus(rows, *c) for c in CELLS}
    pvals = {k: r.p_value for k, r in results.items()}
    passing = benjamini_hochberg(pvals, FDR_Q)

    if all(r.statistic is None for r in results.values()):
        verdict, reason = VERDICT_NO_ELIGIBLE_DATA, (
            "no omnibus could be formed; fewer than two bins carried data")
    elif passing[PRIMARY_HETEROGENEITY_CELL]:
        verdict, reason = VERDICT_HETEROGENEITY_PRESENT, (
            "the primary heterogeneity cell survives BH at FDR 10%; bin "
            "estimates are DESCRIPTIVE ONLY and a new prospective "
            "preregistration under a new name is required")
    else:
        verdict, reason = VERDICT_NO_HETEROGENEITY, (
            "this tranche does not show regime dependence -- which is NOT the "
            "same as regime dependence being absent")

    return {
        "milestone": MILESTONE,
        "family": "SECONDARY", "cannot_rescue_primary": True,
        "statement": CANNOT_RESCUE,
        "corpus": corpus, "family_size": len(CELLS), "fdr_q": FDR_Q,
        "bins": list(FROZEN_BINS),
        "primary_heterogeneity_cell":
            f"{PRIMARY_HETEROGENEITY_CELL[0]}@{PRIMARY_HETEROGENEITY_CELL[1]}s",
        "omnibus": {f"{c}@{h}s": {**r.to_dict(), "passes_fdr": passing[(c, h)]}
                    for (c, h), r in results.items()},
        "underpowered": {f"{c}@{h}s": list(r.underpowered_bins)
                         for (c, h), r in results.items() if r.underpowered_bins},
        "verdict": verdict, "reason": reason,
    }
