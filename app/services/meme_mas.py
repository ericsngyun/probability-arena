"""MEME-MAS-001: read-only multi-agent DIAGNOSTIC scoring layer for memecoins.

Five deterministic "agents" (pure functions — NO LLM, NO external calls, NO new
providers) turn already-persisted data (meme_attention_snapshots +
crypto_token_risk_assessments + meme_catalyst_events) into diagnostic sub-scores
and a `review_priority` that triages how much HUMAN REVIEW a token warrants:

    Coin Structure  → liquidity/volume quality, holder/sniper/insider/bundler
                      concentration, authority/rug/honeypot, provider coverage
    Catalyst Velocity → attention score + jump, boosts, social, catalyst freq
    Timing          → token age, momentum, boost recency, attention persistence
    Risk Auditor    → severe/high risk, concentration red flags, fake-volume /
                      liquidity-removed, missing/unknown provider coverage
    Composite Review → review_priority: low | monitor | elevated_review |
                      high_review | reject_risk

`review_priority` is a REVIEW-ATTENTION label for a human, NOT a trade signal.
This layer computes NO dollar EV, does NO paper trading, sizes NO positions,
places NO orders, recommends NO trade/side, and uses NO wallets/keys/swaps/
signing/execution. Everything is derived read-only from persisted rows and
recomputed on demand (no new table, no external request, no provider budget
impact). See docs/SAFETY_BOUNDARIES.md.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CryptoTokenRiskAssessment,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)

# review-attention triage labels (NOT trade signals, NOT ordered by "value")
REVIEW_PRIORITIES = ("low", "monitor", "elevated_review", "high_review", "reject_risk")

# concentration thresholds mirror the crypto risk engine (CRYPTO_RISK_MAX_*)
TOP10_MAX = 40.0
SNIPER_MAX = 20.0
INSIDER_MAX = 15.0
BUNDLER_MAX = 25.0

DISCLAIMER = (
    "Read-only diagnostic intelligence. `review_priority` triages HUMAN REVIEW "
    "attention only — it is not a trade recommendation, not EV, not a position "
    "size, not an instruction. No paper trading, orders, wallets, keys, swaps, "
    "signing, or execution. Derived on demand from persisted rows."
)

CONCENTRATION_REASONS = (
    "high_top10_concentration", "sniper_concentration",
    "insider_concentration", "bundler_concentration",
)


@dataclass(frozen=True)
class CalibrationProfile:
    """Weights + gates that turn sub-scores into a review_priority.

    `v2` is the MEME-MAS-002 recalibration (risk-aware, gated high_review),
    informed by MEME-SHADOW-001: missing provider coverage and concentration
    flags predicted worse survival/rug, yet the old velocity-heavy composite
    diluted them. `v1` reproduces MEME-MAS-001 exactly so MEME-SHADOW can run a
    before/after comparison. Calibration only — no trade behavior."""

    name: str
    missing_coverage_penalty: float
    concentration_penalty: float
    reason_penalty: float          # fake_volume / liquidity_removed / suspicious
    risk_dampen: float             # review *= (1 - risk_dampen * risk_penalty)
    band_high: float
    band_elevated: float
    band_monitor: float
    gate_high_review: bool
    gate_momentum_min: float
    gate_structure_min: float
    gate_coverage_min: float
    gate_risk_max: float


PROFILE_V1 = CalibrationProfile(
    name="v1", missing_coverage_penalty=0.3, concentration_penalty=0.6, reason_penalty=0.5,
    risk_dampen=0.7, band_high=0.62, band_elevated=0.45, band_monitor=0.25,
    gate_high_review=False, gate_momentum_min=0.0, gate_structure_min=0.0,
    gate_coverage_min=0.0, gate_risk_max=1.0,
)
PROFILE_V2 = CalibrationProfile(
    name="v2", missing_coverage_penalty=0.55, concentration_penalty=0.7, reason_penalty=0.6,
    # band_high=0.68 tuned on live EVO-X2 data (2026-07-08): the gates alone did
    # not tighten high_review because the live high_review population is already
    # clean/covered; the raised bar makes high_review genuinely selective
    # (token-share ~0.40 -> ~0.16) while keeping the strongest clean tokens.
    risk_dampen=1.0, band_high=0.68, band_elevated=0.38, band_monitor=0.20,
    gate_high_review=True, gate_momentum_min=0.6, gate_structure_min=0.6,
    gate_coverage_min=0.5, gate_risk_max=0.4,
)
DEFAULT_PROFILE = PROFILE_V2
PROFILES = {"v1": PROFILE_V1, "v2": PROFILE_V2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _pctile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 4)


@dataclass
class AgentScore:
    score: float
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class TokenInputs:
    token_address: str
    symbol: str | None
    snapshot: MemeAttentionSnapshot
    previous: MemeAttentionSnapshot | None
    assessment: CryptoTokenRiskAssessment | None
    catalyst_count: int
    snapshot_count: int
    source_snapshot_ids: list[int]

    @property
    def flags(self) -> dict:
        return (self.assessment.flags if self.assessment else None) or {}

    @property
    def risk_reason_list(self) -> list[str]:
        return list((self.assessment.risk_reasons if self.assessment else None) or [])

    @property
    def provider_names(self) -> list[str]:
        names = (self.assessment.provider_names if self.assessment else None) or []
        return [n for n in names if n not in ("heuristic", "heuristics")]


@dataclass
class MemeMasAssessment:
    token_address: str
    symbol: str | None
    structure_score: float
    velocity_score: float
    timing_score: float
    risk_penalty: float
    review_score: float
    review_priority: str
    reasoning_trace: list[str]
    missing_evidence: list[str]
    risk_reasons: list[str]
    source_snapshot_ids: list[int]
    # MEME-MAS-002 first-class quality outputs (0..1)
    momentum_quality: float = 0.0
    structure_quality: float = 0.0
    coverage_quality: float = 0.0
    profile: str = "v2"

    def scores(self) -> dict:
        return {
            "structure": self.structure_score,
            "velocity": self.velocity_score,
            "timing": self.timing_score,
            "risk_penalty": self.risk_penalty,
            "momentum_quality": self.momentum_quality,
            "structure_quality": self.structure_quality,
            "coverage_quality": self.coverage_quality,
            "review": self.review_score,
        }


# --- the five agents (pure, deterministic) ----------------------------------


def coin_structure_agent(inp: TokenInputs) -> AgentScore:
    s, flags = inp.snapshot, inp.flags
    reasons: list[str] = []
    missing: list[str] = []
    parts: list[float] = []

    if s.liquidity_usd is None:
        missing.append("liquidity")
    else:
        q = 1.0 if s.liquidity_usd >= 50_000 else 0.6 if s.liquidity_usd >= 5_000 else 0.25
        parts.append(q)
        reasons.append("healthy_liquidity" if q >= 0.6 else "thin_liquidity")

    if s.volume_24h_usd is None:
        missing.append("volume")
    else:
        q = 1.0 if s.volume_24h_usd >= 100_000 else 0.6 if s.volume_24h_usd >= 10_000 else 0.3
        parts.append(q)
        if q < 0.6:
            reasons.append("low_volume")

    top10 = flags.get("top10_holder_pct")
    if top10 is None:
        missing.append("top10_holder")
    else:
        parts.append(1.0 if top10 <= 20 else 0.55 if top10 <= TOP10_MAX else 0.2)
        if top10 > TOP10_MAX:
            reasons.append("high_top10_concentration")

    for key, thr, label in (
        ("sniper_pct", SNIPER_MAX, "sniper_concentration_flagged"),
        ("insider_pct", INSIDER_MAX, "insider_concentration_flagged"),
        ("bundler_pct", BUNDLER_MAX, "bundler_concentration_flagged"),
    ):
        v = flags.get(key)
        if v is None:
            missing.append(key.replace("_pct", ""))
        elif v > thr:
            parts.append(0.2)
            reasons.append(label)
        else:
            parts.append(0.9)

    if flags.get("rug_risk"):
        parts.append(0.0)
        reasons.append("rug_flag")
    if flags.get("honeypot"):
        parts.append(0.0)
        reasons.append("honeypot_flag")
    if flags.get("mint_authority_enabled"):
        parts.append(0.4)
        reasons.append("mint_authority_active")
    if flags.get("freeze_authority_enabled"):
        parts.append(0.4)
        reasons.append("freeze_authority_active")

    if s.provider_confidence is not None and s.provider_confidence < 0.3:
        reasons.append("weak_provider_coverage")

    score = _clamp01(sum(parts) / len(parts)) if parts else 0.3
    return AgentScore(round(score, 4), reasons, missing)


def catalyst_velocity_agent(inp: TokenInputs) -> AgentScore:
    s = inp.snapshot
    reasons: list[str] = []
    missing: list[str] = []
    parts: list[float] = []

    if s.attention_score is None:
        missing.append("attention")
    else:
        parts.append(_clamp01(s.attention_score))
        if s.attention_score >= 0.6:
            reasons.append("strong_attention")
        elif s.attention_score < 0.3:
            reasons.append("weak_attention")

    if (
        inp.previous is not None
        and inp.previous.attention_score is not None
        and s.attention_score is not None
    ):
        jump = round(s.attention_score - inp.previous.attention_score, 4)
        if jump >= 0.15:
            parts.append(min(1.0, 0.5 + jump))
            reasons.append("attention_rising")
        elif jump <= -0.15:
            parts.append(0.3)
            reasons.append("attention_fading")

    if (s.boost_amount or 0) > 0:
        parts.append(0.7)
        reasons.append("boosted")
    if (s.boost_velocity or 0) > 0:
        parts.append(0.7)

    if s.has_social:
        parts.append(0.7)
    else:
        reasons.append("no_social_metadata")

    if inp.catalyst_count >= 3:
        parts.append(0.8)
        reasons.append("frequent_catalysts")
    elif inp.catalyst_count == 0:
        parts.append(0.3)
        reasons.append("no_catalysts")

    if s.profile_completeness is not None:
        parts.append(_clamp01(s.profile_completeness))

    score = _clamp01(sum(parts) / len(parts)) if parts else 0.2
    return AgentScore(round(score, 4), reasons, missing)


def timing_agent(inp: TokenInputs) -> AgentScore:
    s = inp.snapshot
    reasons: list[str] = []
    missing: list[str] = []
    parts: list[float] = []

    if s.token_age_seconds is None:
        missing.append("token_age")
    elif s.token_age_seconds < 6 * 3600:
        parts.append(0.9)
        reasons.append("fresh_token")
    elif s.token_age_seconds < 48 * 3600:
        parts.append(0.6)
    else:
        parts.append(0.35)
        reasons.append("mature_token")

    if s.liquidity_growth is not None:
        parts.append(_clamp01(0.5 + s.liquidity_growth))
        if s.liquidity_growth > 0.1:
            reasons.append("liquidity_momentum")
        elif s.liquidity_growth < -0.1:
            reasons.append("liquidity_declining")
    if s.volume_growth is not None:
        parts.append(_clamp01(0.5 + s.volume_growth))
        if s.volume_growth > 0.1:
            reasons.append("volume_momentum")

    if (s.boost_velocity or 0) > 0:
        parts.append(0.7)
        reasons.append("recent_boost")

    if inp.snapshot_count >= 3:
        parts.append(0.8)
        reasons.append("sustained_attention")
    elif inp.snapshot_count <= 1:
        parts.append(0.4)

    score = _clamp01(sum(parts) / len(parts)) if parts else 0.4
    return AgentScore(round(score, 4), reasons, missing)


def coverage_quality_agent(inp: TokenInputs) -> AgentScore:
    """How complete is the provider RISK coverage for this token (0..1).
    MEME-MAS-002 first-class output — MEME-SHADOW showed missing coverage
    predicts worse survival, so it must gate/penalize, not hide inside structure."""
    flags = inp.flags
    reasons: list[str] = []
    missing: list[str] = []
    parts: list[float] = []

    dims = ("top10_holder_pct", "sniper_pct", "insider_pct", "bundler_pct")
    present = sum(1 for d in dims if flags.get(d) is not None)
    parts.append(present / len(dims))
    if present == 0:
        reasons.append("no_holder_dimensions")

    if inp.provider_names:
        parts.append(1.0)
    else:
        parts.append(0.0)
        reasons.append("missing_provider_coverage")
        missing.append("provider_risk_data")

    conf = inp.snapshot.provider_confidence
    if conf is not None:
        parts.append(_clamp01(conf))

    score = _clamp01(sum(parts) / len(parts)) if parts else 0.0
    return AgentScore(round(score, 4), reasons, missing)


def risk_auditor_agent(inp: TokenInputs, profile: CalibrationProfile = DEFAULT_PROFILE) -> AgentScore:
    s, flags = inp.snapshot, inp.flags
    rr = inp.risk_reason_list
    risk_reasons: list[str] = []
    missing: list[str] = []
    penalties: list[float] = [0.0]

    level = (
        (s.risk_level or (inp.assessment.composite_risk_level if inp.assessment else None) or "")
        .lower()
    )
    if level == "severe":
        risk_reasons.append("severe_risk_level")
        penalties.append(1.0)
    elif level == "high":
        risk_reasons.append("high_risk_level")
        penalties.append(0.8)
    elif level == "medium":
        penalties.append(0.4)

    for key, thr, label in (
        ("top10_holder_pct", TOP10_MAX, "high_top10_concentration"),
        ("sniper_pct", SNIPER_MAX, "sniper_concentration"),
        ("insider_pct", INSIDER_MAX, "insider_concentration"),
        ("bundler_pct", BUNDLER_MAX, "bundler_concentration"),
    ):
        v = flags.get(key)
        if v is not None and v > thr:
            risk_reasons.append(label)
            penalties.append(profile.concentration_penalty)

    if flags.get("rug_risk"):
        risk_reasons.append("rug_flag")
        penalties.append(1.0)
    if flags.get("honeypot"):
        risk_reasons.append("honeypot_flag")
        penalties.append(1.0)

    for reason, label in (
        ("fake_volume_suspected", "fake_volume"),
        ("liquidity_removed", "liquidity_removed"),
        ("suspicious_volume_spike", "suspicious_volume"),
    ):
        if reason in rr:
            risk_reasons.append(label)
            penalties.append(profile.reason_penalty)

    if not inp.provider_names:
        missing.append("provider_risk_data")
        risk_reasons.append("missing_provider_coverage")
        penalties.append(profile.missing_coverage_penalty)
    if "provider_unknown" in rr:
        risk_reasons.append("provider_unknown")
        penalties.append(0.2)

    # the single worst flag dominates the penalty (avoid diluting a severe flag)
    return AgentScore(round(_clamp01(max(penalties)), 4), risk_reasons, missing)


def composite_review_agent(
    profile: CalibrationProfile,
    velocity: AgentScore,
    structure: AgentScore,
    timing: AgentScore,
    coverage: AgentScore,
    risk: AgentScore,
    momentum_quality: float,
) -> tuple[str, float]:
    """Turn sub-scores into a review-attention priority under `profile`.
    Hard-rejects on severe/rug/honeypot (both profiles). v2 additionally
    risk-dampens more heavily and GATES high_review behind clean structure,
    non-missing coverage, and no concentration flags."""
    if (
        risk.score >= 0.8
        or "severe_risk_level" in risk.reasons
        or "rug_flag" in risk.reasons
        or "honeypot_flag" in risk.reasons
    ):
        return "reject_risk", 0.0

    if profile.name == "v1":
        review = (
            0.40 * velocity.score + 0.35 * structure.score + 0.25 * timing.score
        ) * (1 - profile.risk_dampen * risk.score)
    else:  # v2: momentum + structure only (coverage GATES high_review and, when
        # missing, penalizes via risk — it must not PROMOTE a covered-but-weak
        # token), heavier risk dampening
        review = (
            0.55 * momentum_quality + 0.45 * structure.score
        ) * (1 - profile.risk_dampen * risk.score)
    review = _clamp01(review)

    if review >= profile.band_high:
        priority = "high_review"
    elif review >= profile.band_elevated:
        priority = "elevated_review"
    elif review >= profile.band_monitor:
        priority = "monitor"
    else:
        priority = "low"

    # v2 gate: high_review must ALSO be structurally clean, covered, un-flagged
    if priority == "high_review" and profile.gate_high_review:
        concentration_flagged = any(r in CONCENTRATION_REASONS for r in risk.reasons)
        missing_coverage = "missing_provider_coverage" in risk.reasons
        gates_ok = (
            momentum_quality >= profile.gate_momentum_min
            and structure.score >= profile.gate_structure_min
            and coverage.score >= profile.gate_coverage_min
            and risk.score <= profile.gate_risk_max
            and not concentration_flagged
            and not missing_coverage
        )
        if not gates_ok:
            priority = "elevated_review"  # demote: attractive but not clean/covered
    return priority, round(review, 4)


class MemeMasDiagnosticService:
    """Runs the agents for one token and combines them under a calibration
    profile (v2 default). Pure computation — no session mutation, no external
    call."""

    def __init__(self, profile: CalibrationProfile = DEFAULT_PROFILE):
        self.profile = profile

    def assess(self, inp: TokenInputs) -> MemeMasAssessment:
        structure = coin_structure_agent(inp)
        velocity = catalyst_velocity_agent(inp)
        timing = timing_agent(inp)
        coverage = coverage_quality_agent(inp)
        risk = risk_auditor_agent(inp, self.profile)

        momentum_quality = round(0.6 * velocity.score + 0.4 * timing.score, 4)
        priority, review_score = composite_review_agent(
            self.profile, velocity, structure, timing, coverage, risk, momentum_quality
        )

        trace = (
            [f"structure:{r}" for r in structure.reasons]
            + [f"velocity:{r}" for r in velocity.reasons]
            + [f"timing:{r}" for r in timing.reasons]
            + [f"coverage:{r}" for r in coverage.reasons]
        )
        missing = sorted(set(
            structure.missing + velocity.missing + timing.missing + coverage.missing + risk.missing
        ))
        return MemeMasAssessment(
            token_address=inp.token_address,
            symbol=inp.symbol,
            structure_score=structure.score,
            velocity_score=velocity.score,
            timing_score=timing.score,
            risk_penalty=risk.score,
            review_score=review_score,
            review_priority=priority,
            reasoning_trace=trace,
            missing_evidence=missing,
            risk_reasons=risk.reasons,
            source_snapshot_ids=inp.source_snapshot_ids,
            momentum_quality=momentum_quality,
            structure_quality=structure.score,
            coverage_quality=coverage.score,
            profile=self.profile.name,
        )


# --- windowed report --------------------------------------------------------


@dataclass
class MemeMasReport:
    note: str
    window_hours: int
    tokens_assessed: int
    by_priority: dict
    top_candidates: list[dict] = field(default_factory=list)
    risk_rejects: list[dict] = field(default_factory=list)
    missing_coverage_tokens: int = 0
    subscore_distributions: dict = field(default_factory=dict)
    provider_coverage: dict = field(default_factory=dict)


class MemeMasReportService:
    """Builds the diagnostic review from the recent attention window, joined to
    each token's latest risk assessment. On demand, read-only — persists
    nothing, calls nothing external."""

    def __init__(self, diagnostic: MemeMasDiagnosticService | None = None):
        self.diagnostic = diagnostic or MemeMasDiagnosticService()

    def _gather(self, session: Session, hours: int) -> list[TokenInputs]:
        start = _now() - timedelta(hours=hours)
        snaps = session.execute(
            select(MemeAttentionSnapshot)
            .where(MemeAttentionSnapshot.observed_at >= start)
            .order_by(MemeAttentionSnapshot.id)
        ).scalars().all()

        by_token: dict[str, list[MemeAttentionSnapshot]] = {}
        for s in snaps:
            by_token.setdefault(s.token_address, []).append(s)

        tokens = list(by_token.keys())
        # latest risk assessment per token
        assessments: dict[str, CryptoTokenRiskAssessment] = {}
        if tokens:
            rows = session.execute(
                select(CryptoTokenRiskAssessment)
                .where(CryptoTokenRiskAssessment.token_address.in_(tokens))
                .order_by(CryptoTokenRiskAssessment.id.desc())
            ).scalars().all()
            for r in rows:
                assessments.setdefault(r.token_address, r)

        # catalyst counts per token in window
        catalyst_rows = session.execute(
            select(MemeCatalystEvent.subject_ref, func.count())
            .where(MemeCatalystEvent.observed_at >= start)
            .group_by(MemeCatalystEvent.subject_ref)
        ).all()
        catalyst_counts = {ref: n for ref, n in catalyst_rows}

        inputs: list[TokenInputs] = []
        for token, rows in by_token.items():
            rows_sorted = sorted(rows, key=lambda x: x.id)
            latest = rows_sorted[-1]
            previous = rows_sorted[-2] if len(rows_sorted) >= 2 else None
            inputs.append(
                TokenInputs(
                    token_address=token,
                    symbol=latest.symbol,
                    snapshot=latest,
                    previous=previous,
                    assessment=assessments.get(token),
                    catalyst_count=catalyst_counts.get(token, 0),
                    snapshot_count=len(rows_sorted),
                    source_snapshot_ids=[latest.id]
                    + ([previous.id] if previous else [])
                    + ([assessments[token].id] if token in assessments else []),
                )
            )
        return inputs

    def assess_all(self, session: Session, hours: int = 24) -> list[MemeMasAssessment]:
        return [self.diagnostic.assess(inp) for inp in self._gather(session, hours)]

    def build(self, session: Session, hours: int = 24, top: int = 10) -> MemeMasReport:
        results = self.assess_all(session, hours)

        by_priority = {p: 0 for p in REVIEW_PRIORITIES}
        for r in results:
            by_priority[r.review_priority] = by_priority.get(r.review_priority, 0) + 1

        def row(r: MemeMasAssessment) -> dict:
            return {
                "token": r.token_address[:16],
                "symbol": r.symbol,
                "review_priority": r.review_priority,
                "review_score": r.review_score,
                "structure": r.structure_score,
                "velocity": r.velocity_score,
                "timing": r.timing_score,
                "risk_penalty": r.risk_penalty,
                "top_reasons": r.reasoning_trace[:4],
                "risk_reasons": r.risk_reasons[:4],
                "missing_evidence": r.missing_evidence,
            }

        non_reject = [r for r in results if r.review_priority != "reject_risk"]
        rejects = [r for r in results if r.review_priority == "reject_risk"]
        top_candidates = sorted(non_reject, key=lambda x: -x.review_score)[:top]

        return MemeMasReport(
            note=DISCLAIMER,
            window_hours=hours,
            tokens_assessed=len(results),
            by_priority=by_priority,
            top_candidates=[row(r) for r in top_candidates],
            risk_rejects=[row(r) for r in rejects[:top]],
            missing_coverage_tokens=sum(1 for r in results if "provider_risk_data" in r.missing_evidence),
            subscore_distributions={
                "structure_p50": _pctile([r.structure_score for r in results], 50),
                "structure_p90": _pctile([r.structure_score for r in results], 90),
                "velocity_p50": _pctile([r.velocity_score for r in results], 50),
                "velocity_p90": _pctile([r.velocity_score for r in results], 90),
                "timing_p50": _pctile([r.timing_score for r in results], 50),
                "risk_penalty_p50": _pctile([r.risk_penalty for r in results], 50),
                "risk_penalty_p90": _pctile([r.risk_penalty for r in results], 90),
            },
            provider_coverage={
                "with_provider_data": sum(1 for r in results if "provider_risk_data" not in r.missing_evidence),
                "total": len(results),
            },
        )
