"""Crypto risk engine (CRYPTO-002): read-only Solana memecoin risk scoring.

Combines deterministic heuristics over data Crypto Arena already collects
(liquidity, volume, pair age, price moves, boosts, metadata, pair count)
with optional provider facts (GoPlus / SolanaTracker holder, sniper,
insider, bundler, and authority data) into normalized sub-scores, a
composite score, and a composite level (low|medium|high|severe|unknown),
persisted on crypto_token_risk_assessments with the reasons that drove it.

Hard boundary, stated plainly: a risk score is **risk intelligence, not a
trade recommendation**. "Severe" means avoid/flag for human review — never
short/sell/buy. This module contains no wallet, key, swap, transaction,
order, or execution code; see docs/SAFETY_BOUNDARIES.md.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenRiskAssessment,
)
from app.services.crypto_risk import (
    BirdeyeRiskAdapter,
    GoPlusSolanaRiskAdapter,
    RiskAssessment,
    SolanaTrackerRiskAdapter,
)

logger = logging.getLogger(__name__)

ENGINE_PROVIDER_NAME = "risk-engine"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_SEVERE = "severe"
RISK_UNKNOWN = "unknown"

# Risk categories (deterministic reason codes persisted in risk_reasons)
CAT_LOW_LIQUIDITY = "low_liquidity"
CAT_LIQUIDITY_REMOVED = "liquidity_removed"
CAT_EXTREME_PRICE = "extreme_price_movement"
CAT_SUSPICIOUS_VOLUME = "suspicious_volume_spike"
CAT_NEW_PAIR = "new_pair_too_young"
CAT_BOOSTED = "boosted_token"
CAT_MISSING_METADATA = "missing_metadata"
CAT_HOLDER_CONCENTRATION = "high_holder_concentration"
CAT_SNIPER = "sniper_concentration"
CAT_INSIDER = "insider_concentration"
CAT_BUNDLER = "bundler_concentration"
CAT_CREATOR_CONCENTRATION = "creator_concentration"  # MEME-RISK-003: creator/deployer holdings
CAT_MINT_AUTHORITY = "mint_authority_enabled"
CAT_FREEZE_AUTHORITY = "freeze_authority_enabled"
CAT_FAKE_VOLUME = "fake_volume_suspected"
CAT_PROVIDER_RUG = "provider_rug_flag"
CAT_PROVIDER_HONEYPOT = "provider_honeypot_flag"
CAT_PROVIDER_UNKNOWN = "provider_unknown"

# Categories that force a severe composite regardless of the weighted score
SEVERE_CATEGORIES = frozenset(
    {CAT_PROVIDER_RUG, CAT_PROVIDER_HONEYPOT, CAT_LIQUIDITY_REMOVED}
)
# Categories the signal layer treats as holder / supply-control evidence
HOLDER_CATEGORIES = frozenset(
    {CAT_HOLDER_CONCENTRATION, CAT_SNIPER, CAT_INSIDER, CAT_BUNDLER, CAT_CREATOR_CONCENTRATION}
)
SUPPLY_CATEGORIES = frozenset({CAT_MINT_AUTHORITY, CAT_FREEZE_AUTHORITY})

# Heuristic deltas (extremes relative to the configured thresholds)
EXTREME_PRICE_CHANGE_5M_PCT = 50.0
FAKE_VOLUME_LIQUIDITY_MULTIPLE = 20.0  # 24h volume >= 20x liquidity smells painted
VOLUME_SPIKE_LIQUIDITY_MULTIPLE = 2.0  # 5m volume >= 2x liquidity in one window

# Composite weights per sub-score (renormalized over available sub-scores)
COMPOSITE_WEIGHTS = {
    "liquidity": 0.20,
    "holder": 0.25,
    "authority": 0.20,
    "market_structure": 0.15,
    "manipulation": 0.10,
    "provider": 0.10,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def level_for(score: float | None) -> str:
    if score is None:
        return RISK_UNKNOWN
    if score < 0.25:
        return RISK_LOW
    if score < 0.50:
        return RISK_MEDIUM
    if score < 0.75:
        return RISK_HIGH
    return RISK_SEVERE


@dataclass
class RiskEngineConfig:
    min_liquidity_usd: float = 5000.0
    max_top_holder_pct: float = 20.0
    max_sniper_pct: float = 20.0
    max_insider_pct: float = 15.0
    max_bundler_pct: float = 25.0
    max_creator_pct: float = 15.0  # MEME-RISK-003: creator/deployer concentration
    min_pair_age_seconds: int = 300
    version: str = "v1"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RiskEngineConfig":
        s = settings or get_settings()
        return cls(
            min_liquidity_usd=s.crypto_risk_min_liquidity_usd,
            max_top_holder_pct=s.crypto_risk_max_top_holder_pct,
            max_sniper_pct=s.crypto_risk_max_sniper_pct,
            max_insider_pct=s.crypto_risk_max_insider_pct,
            max_bundler_pct=s.crypto_risk_max_bundler_pct,
            max_creator_pct=s.crypto_risk_max_creator_pct,
            min_pair_age_seconds=s.crypto_risk_min_pair_age_seconds,
            version=s.crypto_risk_engine_version,
        )


@dataclass
class RiskEvaluation:
    """Structured engine output. Intelligence only — never a trade signal."""

    token_address: str
    composite_risk_score: float | None
    composite_risk_level: str
    sub_scores: dict = field(default_factory=dict)  # name -> float|None
    reasons: list = field(default_factory=list)  # ordered category codes
    provider_names: list = field(default_factory=list)
    provider_flags: dict = field(default_factory=dict)  # merged provider facts
    provider_errors: dict = field(default_factory=dict)  # provider -> error note

    def as_signal_view(self) -> RiskAssessment:
        """The RiskAssessment shape CryptoSignalService consumes."""
        return RiskAssessment(
            provider=ENGINE_PROVIDER_NAME,
            token_address=self.token_address,
            risk_score=self.composite_risk_score,
            risk_level=self.composite_risk_level,
            flags={**self.provider_flags, "categories": list(self.reasons)},
            raw={"sub_scores": self.sub_scores},
        )


class CryptoRiskProviderRegistry:
    """Selects enabled real provider adapters. No providers enabled is a
    fully-supported mode (heuristics only); a failing provider is isolated
    and reported, never fatal to a scan."""

    def __init__(self, adapters: list | None = None, settings: Settings | None = None):
        if adapters is not None:
            self.adapters = adapters
        else:
            settings = settings or get_settings()
            timeout = settings.crypto_risk_provider_timeout_seconds
            self.adapters = []
            if settings.enable_goplus_risk:
                self.adapters.append(
                    GoPlusSolanaRiskAdapter(api_key=settings.goplus_api_key, timeout=timeout)
                )
            if settings.enable_solana_tracker_risk:
                self.adapters.append(
                    SolanaTrackerRiskAdapter(
                        api_key=settings.solana_tracker_api_key, timeout=timeout
                    )
                )
            if settings.enable_birdeye_risk:
                self.adapters.append(
                    BirdeyeRiskAdapter(api_key=settings.birdeye_api_key, timeout=timeout)
                )
            # enable_rugcheck_risk / enable_helius are reserved: no adapter yet

    @property
    def provider_backed(self) -> bool:
        return bool(self.adapters)

    async def gather(self, token_address: str) -> tuple[list[RiskAssessment], dict]:
        """(successful provider reads, {provider: error-note}) — one bad
        provider never blocks the others or the heuristics."""
        results: list[RiskAssessment] = []
        errors: dict = {}
        for adapter in self.adapters:
            try:
                assessment = await adapter.assess(token_address)
                if assessment is not None:
                    results.append(assessment)
                else:
                    errors[adapter.name] = "no usable data (error, rate limit, or drift)"
            except Exception as exc:  # defense in depth: adapters shouldn't raise
                logger.exception("Risk provider %s failed for %s", adapter.name, token_address)
                errors[adapter.name] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return results, errors


def merge_provider_flags(results: list[RiskAssessment]) -> dict:
    """Union of provider facts; on conflicts the more pessimistic value wins
    (max for percentages, True for booleans)."""
    merged: dict = {}
    for result in results:
        for key, value in (result.flags or {}).items():
            if isinstance(value, bool):
                merged[key] = merged.get(key, False) or value
            elif isinstance(value, (int, float)) and key.endswith("_pct"):
                merged[key] = max(merged.get(key, 0), value)
            elif key not in merged:
                merged[key] = value
    return merged


class HeuristicRiskEngine:
    """Deterministic sub-scores from data CRYPTO-001 already persists, plus
    merged provider facts when available. Always available — needs no
    providers, no credentials, no network."""

    def __init__(self, config: RiskEngineConfig | None = None):
        self.config = config or RiskEngineConfig.from_settings()

    def evaluate(
        self,
        token: CryptoToken | None,
        pair: CryptoPair | None,
        tick: CryptoPriceTick | None,
        previous: CryptoPriceTick | None,
        provider_flags: dict,
        pair_count: int = 0,
        provider_backed: bool = False,
        now: datetime | None = None,
    ) -> tuple[dict, list]:
        """(sub_scores, ordered reason categories)."""
        cfg = self.config
        now = now or _now()
        reasons: list[str] = []

        def hit(category: str) -> None:
            if category not in reasons:
                reasons.append(category)

        # -- liquidity
        liquidity_score = None
        if tick is not None:
            liquidity_score = 0.0
            liquidity = tick.liquidity_usd
            if liquidity is None or liquidity < cfg.min_liquidity_usd:
                hit(CAT_LOW_LIQUIDITY)
                liquidity_score += 0.6 if liquidity is None or liquidity <= 0 else 0.5
            prev_liquidity = previous.liquidity_usd if previous is not None else None
            if (
                prev_liquidity is not None
                and liquidity is not None
                and prev_liquidity >= cfg.min_liquidity_usd
                and liquidity < prev_liquidity * 0.5
            ):
                hit(CAT_LIQUIDITY_REMOVED)
                liquidity_score += 0.9
            liquidity_score = round(min(liquidity_score, 1.0), 4)

        # -- market structure
        structure_score = 0.0
        pair_created = _aware(pair.pair_created_at) if pair is not None else None
        if pair_created is not None and (
            (now - pair_created).total_seconds() < cfg.min_pair_age_seconds
        ):
            hit(CAT_NEW_PAIR)
            structure_score += 0.5
        if token is not None and not (token.symbol and token.name):
            hit(CAT_MISSING_METADATA)
            structure_score += 0.3
        if pair_count == 1:
            structure_score += 0.1  # single venue: mild structural fragility
        structure_score = round(min(structure_score, 1.0), 4)

        # -- manipulation
        manipulation_score = None
        if tick is not None:
            manipulation_score = 0.0
            change = tick.price_change_5m
            if change is not None and abs(change) >= EXTREME_PRICE_CHANGE_5M_PCT:
                hit(CAT_EXTREME_PRICE)
                manipulation_score += 0.5
            liquidity = tick.liquidity_usd or 0.0
            if (
                tick.volume_5m_usd is not None
                and liquidity > 0
                and tick.volume_5m_usd >= liquidity * VOLUME_SPIKE_LIQUIDITY_MULTIPLE
            ):
                hit(CAT_SUSPICIOUS_VOLUME)
                manipulation_score += 0.4
            if (
                tick.volume_24h_usd is not None
                and liquidity > 0
                and tick.volume_24h_usd >= liquidity * FAKE_VOLUME_LIQUIDITY_MULTIPLE
            ):
                hit(CAT_FAKE_VOLUME)
                manipulation_score += 0.5
            boosted = bool((tick.raw_payload or {}).get("boosts_active")) or bool(
                (token.token_metadata or {}).get("boosted") if token is not None else False
            )
            if boosted:
                # paid promotion is risk CONTEXT, not automatic severity
                hit(CAT_BOOSTED)
                manipulation_score += 0.15
            manipulation_score = round(min(manipulation_score, 1.0), 4)

        # -- holder concentration (provider facts required)
        holder_score = None
        holder_checks = (
            ("top10_holder_pct", cfg.max_top_holder_pct, CAT_HOLDER_CONCENTRATION, 0.5),
            ("sniper_pct", cfg.max_sniper_pct, CAT_SNIPER, 0.4),
            ("insider_pct", cfg.max_insider_pct, CAT_INSIDER, 0.4),
            ("bundler_pct", cfg.max_bundler_pct, CAT_BUNDLER, 0.4),
            ("creator_pct", cfg.max_creator_pct, CAT_CREATOR_CONCENTRATION, 0.4),
        )
        if any(key in provider_flags for key, *_ in holder_checks):
            holder_score = 0.0
            for key, threshold, category, weight in holder_checks:
                value = provider_flags.get(key)
                if isinstance(value, (int, float)) and value >= threshold:
                    hit(category)
                    # scale by how far past the threshold the value sits
                    overshoot = min((value - threshold) / max(threshold, 1e-9), 1.0)
                    holder_score += weight * (0.6 + 0.4 * overshoot)
            holder_score = round(min(holder_score, 1.0), 4)

        # -- authority / supply control (provider facts required)
        authority_score = None
        if any(
            key in provider_flags
            for key in ("mint_authority_enabled", "freeze_authority_enabled")
        ):
            authority_score = 0.0
            if provider_flags.get("mint_authority_enabled"):
                hit(CAT_MINT_AUTHORITY)
                authority_score += 0.6
            if provider_flags.get("freeze_authority_enabled"):
                hit(CAT_FREEZE_AUTHORITY)
                authority_score += 0.6
            authority_score = round(min(authority_score, 1.0), 4)

        # -- provider verdicts
        provider_score = None
        if provider_backed:
            provider_score = 0.0
            if provider_flags.get("rug_risk"):
                hit(CAT_PROVIDER_RUG)
                provider_score += 1.0
            if provider_flags.get("honeypot"):
                hit(CAT_PROVIDER_HONEYPOT)
                provider_score += 1.0
            raw_provider_score = provider_flags.get("provider_score")
            if isinstance(raw_provider_score, (int, float)):
                provider_score = max(provider_score, min(float(raw_provider_score), 1.0))
            provider_score = round(min(provider_score, 1.0), 4)
        else:
            hit(CAT_PROVIDER_UNKNOWN)  # honest: no provider corroboration exists

        return (
            {
                "liquidity": liquidity_score,
                "holder": holder_score,
                "authority": authority_score,
                "market_structure": structure_score,
                "manipulation": manipulation_score,
                "provider": provider_score,
            },
            reasons,
        )


def composite_from(sub_scores: dict, reasons: list) -> tuple[float | None, str]:
    """Weighted mean over available sub-scores; severe categories floor the
    composite at 0.75. (None, unknown) when nothing was measurable."""
    available = {
        name: score for name, score in sub_scores.items() if score is not None
    }
    if not available:
        return None, RISK_UNKNOWN
    total_weight = sum(COMPOSITE_WEIGHTS[name] for name in available)
    composite = sum(
        COMPOSITE_WEIGHTS[name] * score for name, score in available.items()
    ) / total_weight
    if any(category in SEVERE_CATEGORIES for category in reasons):
        composite = max(composite, 0.75)
    composite = round(min(composite, 1.0), 4)
    return composite, level_for(composite)


class CryptoRiskEngine:
    """Evaluates one token (best pair + latest ticks + optional providers),
    persists the assessment, and returns the structured evaluation."""

    def __init__(
        self,
        registry: CryptoRiskProviderRegistry | None = None,
        heuristics: HeuristicRiskEngine | None = None,
        config: RiskEngineConfig | None = None,
        chain: str | None = None,
    ):
        settings = get_settings()
        self.config = config or RiskEngineConfig.from_settings(settings)
        self.registry = registry if registry is not None else CryptoRiskProviderRegistry()
        self.heuristics = heuristics or HeuristicRiskEngine(self.config)
        self.chain = chain or settings.crypto_chain

    async def evaluate(
        self,
        session: Session,
        token: CryptoToken | None,
        pair: CryptoPair | None,
        tick: CryptoPriceTick | None,
        previous: CryptoPriceTick | None,
        pair_count: int = 0,
        token_address: str | None = None,
        persist: bool = True,
    ) -> RiskEvaluation:
        address = token_address or (token.token_address if token else None) or (
            tick.token_address if tick else None
        )
        if address is None:
            raise ValueError("evaluate() needs a token, tick, or explicit token_address")

        provider_results, provider_errors = await self.registry.gather(address)
        provider_flags = merge_provider_flags(provider_results)
        for result in provider_results:
            if result.risk_score is not None:
                provider_flags["provider_score"] = max(
                    provider_flags.get("provider_score", 0.0), result.risk_score
                )

        sub_scores, reasons = self.heuristics.evaluate(
            token=token,
            pair=pair,
            tick=tick,
            previous=previous,
            provider_flags=provider_flags,
            pair_count=pair_count,
            provider_backed=bool(provider_results),
        )
        composite, level = composite_from(sub_scores, reasons)

        evaluation = RiskEvaluation(
            token_address=address,
            composite_risk_score=composite,
            composite_risk_level=level,
            sub_scores=sub_scores,
            reasons=reasons,
            provider_names=[result.provider for result in provider_results],
            provider_flags=provider_flags,
            provider_errors=provider_errors,
        )
        if persist:
            self.persist(session, evaluation)
        return evaluation

    def persist(self, session: Session, evaluation: RiskEvaluation) -> CryptoTokenRiskAssessment:
        row = CryptoTokenRiskAssessment(
            chain=self.chain,
            token_address=evaluation.token_address,
            provider=ENGINE_PROVIDER_NAME,
            risk_score=evaluation.composite_risk_score,
            risk_level=evaluation.composite_risk_level,
            flags=evaluation.provider_flags,
            raw_payload={
                "sub_scores": evaluation.sub_scores,
                "provider_errors": evaluation.provider_errors,
            },
            liquidity_risk_score=evaluation.sub_scores.get("liquidity"),
            holder_risk_score=evaluation.sub_scores.get("holder"),
            authority_risk_score=evaluation.sub_scores.get("authority"),
            market_structure_risk_score=evaluation.sub_scores.get("market_structure"),
            manipulation_risk_score=evaluation.sub_scores.get("manipulation"),
            provider_risk_score=evaluation.sub_scores.get("provider"),
            composite_risk_score=evaluation.composite_risk_score,
            composite_risk_level=evaluation.composite_risk_level,
            risk_reasons=list(evaluation.reasons),
            provider_names=list(evaluation.provider_names),
            heuristic_version=self.config.version,
            created_at=_now(),
        )
        session.add(row)
        session.flush()
        return row


def get_risk_engine(settings: Settings | None = None) -> CryptoRiskEngine | None:
    settings = settings or get_settings()
    if not settings.enable_crypto_risk_engine:
        return None
    return CryptoRiskEngine()


RISK_SIGNAL_TYPES = ("holder_risk", "rug_risk", "suspicious_supply_control")


class CryptoRiskReportService:
    """Aggregate risk view: level breakdown, worst tokens, common reasons,
    provider health, and engine mode. Informational only."""

    def build(self, session: Session, recent_limit: int = 10):
        from app.schemas import CryptoRiskAssessmentOut, CryptoRiskReport

        settings = get_settings()
        rows = session.execute(
            select(CryptoTokenRiskAssessment)
            .order_by(CryptoTokenRiskAssessment.id.desc())
            .limit(500)
        ).scalars().all()

        # latest assessment per token (rows are newest-first)
        latest_by_token: dict[str, CryptoTokenRiskAssessment] = {}
        for row in rows:
            latest_by_token.setdefault(row.token_address, row)

        by_level: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        provider_use: dict[str, int] = {}
        provider_errors: dict[str, int] = {}
        for row in latest_by_token.values():
            level = row.composite_risk_level or row.risk_level or RISK_UNKNOWN
            by_level[level] = by_level.get(level, 0) + 1
            for reason in row.risk_reasons or []:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            for name in row.provider_names or []:
                provider_use[name] = provider_use.get(name, 0) + 1
            for name in ((row.raw_payload or {}).get("provider_errors") or {}):
                provider_errors[name] = provider_errors.get(name, 0) + 1

        worst = sorted(
            (
                row
                for row in latest_by_token.values()
                if (row.composite_risk_level or row.risk_level) in (RISK_HIGH, RISK_SEVERE)
            ),
            key=lambda row: -(row.composite_risk_score or row.risk_score or 0.0),
        )[:recent_limit]

        risk_signals = session.execute(
            select(CryptoOpportunitySignal.signal_type, func.count())
            .where(CryptoOpportunitySignal.signal_type.in_(RISK_SIGNAL_TYPES))
            .group_by(CryptoOpportunitySignal.signal_type)
        ).all()

        enabled_providers = [
            name
            for name, enabled in (
                ("goplus", settings.enable_goplus_risk),
                ("solana-tracker", settings.enable_solana_tracker_risk),
            )
            if enabled
        ]
        mode = "disabled"
        if settings.enable_crypto_risk_engine:
            mode = "provider-backed" if enabled_providers else "heuristic-only"

        recent = [
            CryptoRiskAssessmentOut.model_validate(row) for row in rows[:recent_limit]
        ]
        return CryptoRiskReport(
            engine_mode=mode,
            engine_version=settings.crypto_risk_engine_version,
            enabled_providers=enabled_providers,
            assessments_total=session.execute(
                select(func.count()).select_from(CryptoTokenRiskAssessment)
            ).scalar() or 0,
            tokens_assessed=len(latest_by_token),
            by_level=by_level,
            top_risky_tokens=[
                CryptoRiskAssessmentOut.model_validate(row) for row in worst
            ],
            common_reasons=dict(
                sorted(reason_counts.items(), key=lambda item: -item[1])[:10]
            ),
            provider_use=provider_use,
            provider_error_counts=provider_errors,
            risk_signals_created={name: count for name, count in risk_signals},
            recent_assessments=recent,
        )
