"""CRYPTO-TAPE-001 — read-only Solana memecoin lifecycle tape.

Moves the crypto lane from point-in-time scoring to REPLAYABLE TOKEN
LIFECYCLE TAPES: token birth, early holder/actor structure, risk-provider
enrichment, liquidity path, social metadata, and deterministic survival
outcomes over 15m/1h/6h/24h horizons.

The tape is DERIVED: one assembly pass consolidates rows the existing lanes
already persist (crypto_tokens/pairs/price_ticks/discovery_events/
risk_assessments + meme attention snapshots/catalyst events) into lifecycle
rows. It makes ZERO external calls and has ZERO provider-budget impact — the
scheduled scan lanes remain the only data collectors. Fields no source ever
provided stay NULL and are named in missing_info; nothing is fabricated.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): research infrastructure
only. A survival label is measured token behavior — never PnL, EV, a return,
a side, a size, or a recommendation. Actor observations hold public-chain
addresses already persisted by providers; no deanonymization. No wallets,
keys, swaps, signing, orders, execution, or autonomy anywhere. `--dry-run`
persists nothing; a real run persists ONLY lifecycle tape rows — never
signals, never MarketOps state.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenActorObservation,
    CryptoTokenBirthEvent,
    CryptoTokenDiscoveryEvent,
    CryptoTokenLifecycleRun,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenRiskAssessment,
    CryptoTokenSurvivalOutcome,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)

logger = logging.getLogger(__name__)

TAPE_NOTE = (
    "Read-only token lifecycle tape: birth, holder/actor structure, risk "
    "enrichment, liquidity path, social metadata, and survival outcomes, "
    "assembled from already-persisted surveillance rows (no external call, "
    "no provider-budget impact). Research infrastructure — a survival label "
    "is measured token behavior, never PnL, EV, a side, a size, or a "
    "recommendation. No wallets, keys, swaps, signing, orders, or execution."
)

# survival horizons: (label, minutes) — mirrors the MEME-SHADOW yardsticks
HORIZONS: tuple[tuple[str, int], ...] = (
    ("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440)
)
# an observation counts for a horizon if within +/- this fraction of it
HORIZON_TOLERANCE = 0.5
# liquidity below this fraction of the initial value => not survived / removed
SURVIVAL_LIQUIDITY_FRACTION = 0.3
# 24h volume below this at >=6h after birth => dead_volume
DEAD_VOLUME_24H_USD = 500.0
# bonding-curve launchpads; a later non-launchpad pair = graduated/migrated
LAUNCHPAD_DEXES = frozenset({"pumpfun", "moonshot", "launchlab"})

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"

BONDING_LAUNCHPAD = "launchpad_curve"
BONDING_AMM = "amm_pool"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 4)


@dataclass
class CryptoTapeConfig:
    chain: str = "solana"
    default_limit: int = 25
    default_window_hours: int = 48

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "CryptoTapeConfig":
        s = settings or get_settings()
        return cls(chain=s.crypto_chain)


@dataclass
class TokenSources:
    """Everything already persisted about one token, loaded once."""

    token: CryptoToken
    pairs: list[CryptoPair]
    ticks: list[CryptoPriceTick]  # ordered by observed_at asc
    assessments: list[CryptoTokenRiskAssessment]  # ordered by created_at asc
    discovery_events: list[CryptoTokenDiscoveryEvent]  # ordered by observed_at asc
    attention: MemeAttentionSnapshot | None  # latest, if the meme lane saw it
    catalyst_count_24h: int


def merged_assessment_flags(assessments: list[CryptoTokenRiskAssessment]) -> dict:
    """Latest-wins merge of persisted risk flags across assessment rows (the
    engine row usually carries the merged provider facts already)."""
    merged: dict = {}
    for row in assessments:
        merged.update(row.flags or {})
    return merged


def extract_creator_address(assessments: list[CryptoTokenRiskAssessment]) -> str | None:
    """Creator/deployer PUBLIC address if any persisted provider payload named
    one. Current providers rarely do — honest absence is the norm."""
    for row in reversed(assessments):
        for source in (row.flags or {}, row.raw_payload or {}):
            for key in ("creator_address", "creator", "deployer"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def extract_cohort_counts(assessments: list[CryptoTokenRiskAssessment]) -> dict:
    """{sniper|insider|bundler}_address_count from persisted raw provider
    payloads (SolanaTracker risk shape: {"snipers": {"count": N, ...}})."""
    counts: dict = {}
    for row in assessments:
        raw = row.raw_payload or {}
        for key, field_name in (
            ("snipers", "sniper_address_count"),
            ("insiders", "insider_address_count"),
            ("bundlers", "bundler_address_count"),
        ):
            entry = raw.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("count"), int):
                counts[field_name] = entry["count"]
    return counts


class CryptoLifecycleTapeRecorder:
    """One derived tape assembly pass over already-persisted rows. Session-only
    (no adapter, no HTTP client); dry-run persists nothing."""

    def __init__(self, config: CryptoTapeConfig | None = None):
        self.config = config or CryptoTapeConfig.from_settings()

    # --- source loading (read-only) ------------------------------------------

    def _universe(self, session: Session, limit: int, cutoff: datetime) -> list[CryptoToken]:
        return list(session.execute(
            select(CryptoToken)
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at >= cutoff,
            )
            .order_by(CryptoToken.first_seen_at.desc(), CryptoToken.id.desc())
            .limit(limit)
        ).scalars().all())

    def _load_sources(self, session: Session, token: CryptoToken, now: datetime) -> TokenSources:
        address = token.token_address
        pairs = list(session.execute(
            select(CryptoPair).where(
                CryptoPair.chain == self.config.chain,
                CryptoPair.base_token_address == address,
            ).order_by(CryptoPair.id)
        ).scalars().all())
        ticks = list(session.execute(
            select(CryptoPriceTick).where(
                CryptoPriceTick.chain == self.config.chain,
                CryptoPriceTick.token_address == address,
            ).order_by(CryptoPriceTick.observed_at, CryptoPriceTick.id)
        ).scalars().all())
        assessments = list(session.execute(
            select(CryptoTokenRiskAssessment).where(
                CryptoTokenRiskAssessment.chain == self.config.chain,
                CryptoTokenRiskAssessment.token_address == address,
            ).order_by(CryptoTokenRiskAssessment.created_at, CryptoTokenRiskAssessment.id)
        ).scalars().all())
        events = list(session.execute(
            select(CryptoTokenDiscoveryEvent).where(
                CryptoTokenDiscoveryEvent.chain == self.config.chain,
                CryptoTokenDiscoveryEvent.token_address == address,
            ).order_by(CryptoTokenDiscoveryEvent.observed_at, CryptoTokenDiscoveryEvent.id)
        ).scalars().all())
        attention = session.execute(
            select(MemeAttentionSnapshot)
            .where(MemeAttentionSnapshot.token_address == address)
            .order_by(MemeAttentionSnapshot.id.desc())
        ).scalars().first()
        catalyst_count = session.execute(
            select(func.count()).select_from(MemeCatalystEvent).where(
                MemeCatalystEvent.subject_ref == address,
                MemeCatalystEvent.observed_at >= now - timedelta(hours=24),
            )
        ).scalar() or 0
        return TokenSources(
            token=token, pairs=pairs, ticks=ticks, assessments=assessments,
            discovery_events=events, attention=attention,
            catalyst_count_24h=catalyst_count,
        )

    # --- birth event ----------------------------------------------------------

    def build_birth_event(self, sources: TokenSources, now: datetime) -> CryptoTokenBirthEvent:
        token = sources.token
        missing: list[str] = []
        first_event = sources.discovery_events[0] if sources.discovery_events else None
        first_tick = sources.ticks[0] if sources.ticks else None

        evidence_times = [
            t for t in (
                _aware(token.first_seen_at),
                _aware(first_event.observed_at) if first_event else None,
                _aware(first_tick.observed_at) if first_tick else None,
            ) if t is not None
        ]
        first_evidence_at = min(evidence_times) if evidence_times else None

        launch_source = None
        if first_event is not None:
            launch_source = f"{first_event.source}:{first_event.event_type}"
        else:
            missing.append("launch_source")

        # earliest pair by chain-side creation time, falling back to first seen
        first_pair = None
        if sources.pairs:
            first_pair = min(
                sources.pairs,
                key=lambda p: _aware(p.pair_created_at) or _aware(p.first_seen_at) or now,
            )
        else:
            missing.append("pair")

        creator = extract_creator_address(sources.assessments)
        if creator is None:
            missing.append("creator_address")
        flags = merged_assessment_flags(sources.assessments)
        mint_auth = flags.get("mint_authority_enabled")
        freeze_auth = flags.get("freeze_authority_enabled")
        if mint_auth is None:
            missing.append("mint_authority")
        if freeze_auth is None:
            missing.append("freeze_authority")

        metadata = dict(token.token_metadata or {})
        if sources.attention is not None:
            metadata.setdefault("has_social", sources.attention.has_social)
            metadata.setdefault("social_links_count", sources.attention.social_links_count)
        if not metadata:
            missing.append("metadata_links")

        if first_tick is None:
            missing.append("initial_market_state")
        bonding = None
        if first_pair is not None and first_pair.dex_id:
            bonding = (
                BONDING_LAUNCHPAD
                if first_pair.dex_id.lower() in LAUNCHPAD_DEXES
                else BONDING_AMM
            )
        else:
            missing.append("bonding_curve_state")

        provenance = {
            "derived_from": "persisted surveillance rows only (no external call)",
            "crypto_token_id": token.id,
            "discovery_event_ids": [e.id for e in sources.discovery_events[:10]],
            "first_tick_id": first_tick.id if first_tick else None,
            "risk_assessment_ids": [a.id for a in sources.assessments[-3:]],
            "attention_snapshot_id": sources.attention.id if sources.attention else None,
        }
        return CryptoTokenBirthEvent(
            chain=self.config.chain,
            token_address=token.token_address,
            symbol=token.symbol,
            name=token.name,
            observed_at=now,
            first_evidence_at=first_evidence_at,
            launch_source=launch_source,
            first_pair_address=first_pair.pair_address if first_pair else None,
            first_dex_id=first_pair.dex_id if first_pair else None,
            pair_created_at=_aware(first_pair.pair_created_at) if first_pair else None,
            creator_address=creator,
            mint_authority_enabled=mint_auth,
            freeze_authority_enabled=freeze_auth,
            metadata_links=metadata or None,
            initial_price_usd=first_tick.price_usd if first_tick else None,
            initial_liquidity_usd=first_tick.liquidity_usd if first_tick else None,
            initial_volume_24h_usd=first_tick.volume_24h_usd if first_tick else None,
            initial_market_cap=first_tick.market_cap if first_tick else None,
            initial_fdv=first_tick.fdv if first_tick else None,
            bonding_curve_state=bonding,
            provenance=provenance,
            missing_info=missing or None,
            raw_payload=(first_event.raw_payload if first_event else None),
            created_at=now,
        )

    # --- lifecycle snapshot ---------------------------------------------------

    def build_snapshot(
        self, sources: TokenSources, birth: CryptoTokenBirthEvent | None, now: datetime
    ) -> CryptoTokenLifecycleSnapshot:
        missing: list[str] = []
        coverage: list[str] = []

        # latest tick per pair; best pair = deepest liquidity
        latest_by_pair: dict[str, CryptoPriceTick] = {}
        for tick in sources.ticks:
            if tick.pair_address:
                latest_by_pair[tick.pair_address] = tick
        best_tick = max(
            latest_by_pair.values(), key=lambda t: t.liquidity_usd or 0, default=None
        ) or (sources.ticks[-1] if sources.ticks else None)
        if best_tick is not None:
            coverage.append("price_tick")
        else:
            missing.append("market_state")

        latest_assessment = sources.assessments[-1] if sources.assessments else None
        flags = merged_assessment_flags(sources.assessments)
        if latest_assessment is not None:
            coverage.append(f"risk:{latest_assessment.provider}")
            for provider_name in latest_assessment.provider_names or []:
                coverage.append(f"risk:{provider_name}")
        else:
            missing.append("risk_assessment")
        for key in ("top10_holder_pct", "sniper_pct", "insider_pct", "bundler_pct"):
            if key not in flags:
                missing.append(key)

        if sources.attention is not None:
            coverage.append("attention")
        else:
            missing.append("attention_snapshot")

        first_evidence = _aware(birth.first_evidence_at) if birth is not None else None
        age_seconds = (
            max(0, int((now - first_evidence).total_seconds()))
            if first_evidence is not None else None
        )
        tick_age = (
            max(0, int((now - _aware(best_tick.observed_at)).total_seconds()))
            if best_tick is not None else None
        )
        risk_score = risk_level = risk_reasons = None
        if latest_assessment is not None:
            risk_score = (
                latest_assessment.composite_risk_score
                if latest_assessment.composite_risk_score is not None
                else latest_assessment.risk_score
            )
            risk_level = (
                latest_assessment.composite_risk_level or latest_assessment.risk_level
            )
            risk_reasons = latest_assessment.risk_reasons

        return CryptoTokenLifecycleSnapshot(
            birth_event_id=birth.id if birth is not None else None,
            chain=self.config.chain,
            token_address=sources.token.token_address,
            observed_at=now,
            token_age_seconds=age_seconds,
            price_usd=best_tick.price_usd if best_tick else None,
            liquidity_usd=best_tick.liquidity_usd if best_tick else None,
            volume_5m_usd=best_tick.volume_5m_usd if best_tick else None,
            volume_1h_usd=best_tick.volume_1h_usd if best_tick else None,
            volume_24h_usd=best_tick.volume_24h_usd if best_tick else None,
            market_cap=best_tick.market_cap if best_tick else None,
            fdv=best_tick.fdv if best_tick else None,
            holder_count=flags.get("holder_count"),
            top10_holder_pct=flags.get("top10_holder_pct"),
            sniper_pct=flags.get("sniper_pct"),
            insider_pct=flags.get("insider_pct"),
            bundler_pct=flags.get("bundler_pct"),
            creator_pct=flags.get("creator_pct"),
            risk_score=risk_score,
            risk_level=risk_level,
            risk_reasons=risk_reasons,
            boost_amount=(
                sources.attention.boost_amount if sources.attention else None
            ),
            attention_score=(
                sources.attention.attention_score if sources.attention else None
            ),
            has_social=(sources.attention.has_social if sources.attention else None),
            social_links_count=(
                sources.attention.social_links_count if sources.attention else None
            ),
            catalyst_count_24h=sources.catalyst_count_24h,
            pair_count=len(sources.pairs),
            best_pair_address=best_tick.pair_address if best_tick else None,
            best_dex_id=(
                (best_tick.raw_payload or {}).get("dex_id") if best_tick else None
            ),
            volume_to_liquidity_24h=_ratio(
                best_tick.volume_24h_usd if best_tick else None,
                best_tick.liquidity_usd if best_tick else None,
            ),
            single_venue=(len(sources.pairs) == 1) if sources.pairs else None,
            source_tick_id=best_tick.id if best_tick else None,
            source_risk_assessment_id=(
                latest_assessment.id if latest_assessment else None
            ),
            source_attention_snapshot_id=(
                sources.attention.id if sources.attention else None
            ),
            source_tick_age_seconds=tick_age,
            provider_coverage=sorted(set(coverage)) or None,
            missing_info=missing or None,
            created_at=now,
        )

    # --- actor observation ----------------------------------------------------

    def build_actor_observation(
        self, sources: TokenSources, birth: CryptoTokenBirthEvent | None, now: datetime
    ) -> CryptoTokenActorObservation:
        missing: list[str] = []
        obs_sources: list[str] = []
        flags = merged_assessment_flags(sources.assessments)
        creator = extract_creator_address(sources.assessments)
        if creator is None:
            missing.append("creator_address")
        counts = extract_cohort_counts(sources.assessments)
        for field_name in (
            "sniper_address_count", "insider_address_count", "bundler_address_count"
        ):
            if field_name not in counts:
                missing.append(field_name)
        # no configured source exposes an ordered first-buyer list today —
        # the column is an honest placeholder until one legitimately does
        missing.append("first_buyer_addresses")
        if sources.assessments:
            obs_sources.append("crypto_token_risk_assessments")
        holder_distribution = {
            key: flags[key]
            for key in (
                "holder_count", "top10_holder_pct", "sniper_pct",
                "insider_pct", "bundler_pct", "creator_pct",
            )
            if key in flags
        }
        if not holder_distribution:
            missing.append("holder_distribution")
        return CryptoTokenActorObservation(
            birth_event_id=birth.id if birth is not None else None,
            chain=self.config.chain,
            token_address=sources.token.token_address,
            observed_at=now,
            creator_address=creator,
            creator_holding_pct=flags.get("creator_pct"),
            first_buyer_addresses=None,
            sniper_address_count=counts.get("sniper_address_count"),
            insider_address_count=counts.get("insider_address_count"),
            bundler_address_count=counts.get("bundler_address_count"),
            repeated_cohort_ref=None,          # cross-token cohorting: later milestone
            known_creator_cluster_ref=None,    # creator clustering: later milestone
            holder_distribution=holder_distribution or None,
            observation_sources=obs_sources or None,
            missing_info=missing or None,
            created_at=now,
        )

    # --- survival outcome -----------------------------------------------------

    def compute_survival(
        self, birth: CryptoTokenBirthEvent, sources: TokenSources, now: datetime
    ) -> dict:
        """Deterministic survival labels from the token's persisted trajectory.
        Pure computation; None = not yet measurable or source gap."""
        anchor = _aware(birth.first_evidence_at)
        initial_liquidity = birth.initial_liquidity_usd
        details: dict = {"horizons": {}, "anchor": anchor.isoformat() if anchor else None}
        labels: dict = {label: None for label in (
            "survived_15m", "survived_1h", "survived_6h", "survived_24h",
            "liquidity_removed", "dead_volume", "severe_risk",
            "graduated_or_migrated", "provider_gap",
        )}
        if anchor is None:
            details["reason"] = "no first-evidence timestamp"
            labels["provider_gap"] = True
            return {"labels": labels, "details": details, "final": False}

        later = [
            t for t in sources.ticks
            if _aware(t.observed_at) is not None and _aware(t.observed_at) > anchor
        ]
        gap_reasons: list[str] = []

        # per-horizon survival: nearest later observation inside tolerance
        for label, minutes in HORIZONS:
            key = f"survived_{label}"
            target = anchor + timedelta(minutes=minutes)
            tolerance = timedelta(minutes=minutes * HORIZON_TOLERANCE)
            if now < target - tolerance:
                details["horizons"][label] = "not_yet_mature"
                continue
            candidates = [
                t for t in later if abs(_aware(t.observed_at) - target) <= tolerance
            ]
            if not candidates:
                details["horizons"][label] = "no_observation_in_window"
                gap_reasons.append(f"no_tick_at_{label}")
                continue
            nearest = min(candidates, key=lambda t: abs(_aware(t.observed_at) - target))
            if initial_liquidity and nearest.liquidity_usd is not None:
                survived = (
                    nearest.liquidity_usd
                    >= SURVIVAL_LIQUIDITY_FRACTION * initial_liquidity
                )
                labels[key] = bool(survived)
                details["horizons"][label] = {
                    "tick_id": nearest.id,
                    "liquidity_usd": nearest.liquidity_usd,
                    "initial_liquidity_usd": initial_liquidity,
                }
            else:
                details["horizons"][label] = "liquidity_unmeasurable"
                gap_reasons.append(f"liquidity_unmeasurable_at_{label}")

        # liquidity_removed: any later observation below the survival fraction
        if initial_liquidity and later:
            removed_tick = next(
                (
                    t for t in later
                    if t.liquidity_usd is not None
                    and t.liquidity_usd < SURVIVAL_LIQUIDITY_FRACTION * initial_liquidity
                ),
                None,
            )
            labels["liquidity_removed"] = removed_tick is not None
            if removed_tick is not None:
                details["liquidity_removed_tick_id"] = removed_tick.id
        elif not initial_liquidity:
            gap_reasons.append("no_initial_liquidity")

        # dead_volume: latest observation at >=6h after birth with negligible 24h volume
        matured = [t for t in later if _aware(t.observed_at) >= anchor + timedelta(hours=6)]
        if matured:
            last = matured[-1]
            if last.volume_24h_usd is not None:
                labels["dead_volume"] = last.volume_24h_usd < DEAD_VOLUME_24H_USD
                details["dead_volume_basis"] = {
                    "tick_id": last.id, "volume_24h_usd": last.volume_24h_usd,
                }
        elif now >= anchor + timedelta(hours=6):
            gap_reasons.append("no_tick_after_6h")

        # severe_risk: any assessment after birth landed severe
        post_birth = [
            a for a in sources.assessments
            if _aware(a.created_at) is not None and _aware(a.created_at) >= anchor
        ]
        if post_birth:
            labels["severe_risk"] = any(
                (a.composite_risk_level or a.risk_level or "").lower() == "severe"
                for a in post_birth
            )
        else:
            gap_reasons.append("no_risk_assessment")

        # graduated_or_migrated: launchpad-born token later seen on a non-launchpad venue
        if birth.first_dex_id and birth.first_dex_id.lower() in LAUNCHPAD_DEXES:
            labels["graduated_or_migrated"] = any(
                (p.dex_id or "").lower() not in LAUNCHPAD_DEXES and p.dex_id
                for p in sources.pairs
            )
        elif not birth.first_dex_id:
            gap_reasons.append("launch_venue_unknown")
        # else: born on an AMM — graduation does not apply; stays None

        provider_backed = any(
            a.provider_names for a in sources.assessments
        ) or any(a.provider not in ("risk-engine", "mock") for a in sources.assessments)
        if not provider_backed:
            gap_reasons.append("no_provider_backed_risk_read")
        labels["provider_gap"] = bool(gap_reasons)
        if gap_reasons:
            details["gap_reasons"] = sorted(set(gap_reasons))

        # final once the 24h window (plus tolerance) has fully closed
        final = now >= anchor + timedelta(minutes=1440 * (1 + HORIZON_TOLERANCE))
        return {"labels": labels, "details": details, "final": final}

    # --- one assembly pass ----------------------------------------------------

    def run_once(
        self,
        session: Session,
        limit: int | None = None,
        hours: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        started = _now()
        limit = limit if limit is not None else self.config.default_limit
        hours = hours if hours is not None else self.config.default_window_hours
        cutoff = started - timedelta(hours=hours)

        tokens = self._universe(session, limit, cutoff)
        existing_births = {
            b.token_address: b
            for b in session.execute(
                select(CryptoTokenBirthEvent).where(
                    CryptoTokenBirthEvent.chain == self.config.chain,
                    CryptoTokenBirthEvent.token_address.in_(
                        [t.token_address for t in tokens]
                    ),
                )
            ).scalars().all()
        } if tokens else {}

        new_births = 0
        snapshots = 0
        actors = 0
        outcomes = 0
        coverage_summary = {
            "tokens_with_ticks": 0,
            "tokens_with_risk": 0,
            "tokens_with_provider_backed_risk": 0,
            "tokens_with_attention": 0,
            "tokens_without_any_source": 0,
        }
        survival_mix: dict[str, int] = {}
        examples: list[dict] = []

        run: CryptoTokenLifecycleRun | None = None
        if not dry_run:
            run = CryptoTokenLifecycleRun(
                status="running", started_at=started, window_hours=hours,
                config={"limit": limit, "hours": hours, "chain": self.config.chain},
                created_at=started,
            )
            session.add(run)
            session.flush()

        try:
            for token in tokens:
                sources = self._load_sources(session, token, started)
                if sources.ticks:
                    coverage_summary["tokens_with_ticks"] += 1
                if sources.assessments:
                    coverage_summary["tokens_with_risk"] += 1
                    if any(a.provider_names for a in sources.assessments):
                        coverage_summary["tokens_with_provider_backed_risk"] += 1
                if sources.attention is not None:
                    coverage_summary["tokens_with_attention"] += 1
                if not (sources.ticks or sources.assessments or sources.attention):
                    coverage_summary["tokens_without_any_source"] += 1

                birth = existing_births.get(token.token_address)
                if birth is None:
                    birth = self.build_birth_event(sources, started)
                    new_births += 1
                    if not dry_run:
                        birth.run_id = run.id
                        session.add(birth)
                        session.flush()
                        existing_births[token.token_address] = birth

                snapshot = self.build_snapshot(
                    sources, birth if birth.id is not None else None, started
                )
                snapshots += 1
                actor = self.build_actor_observation(
                    sources, birth if birth.id is not None else None, started
                )
                actors += 1
                if not dry_run:
                    snapshot.run_id = run.id
                    actor.run_id = run.id
                    session.add(snapshot)
                    session.add(actor)

                survival = self.compute_survival(birth, sources, started)
                for label, value in survival["labels"].items():
                    if value is True:
                        survival_mix[label] = survival_mix.get(label, 0) + 1
                if not dry_run and birth.id is not None:
                    outcome = session.execute(
                        select(CryptoTokenSurvivalOutcome).where(
                            CryptoTokenSurvivalOutcome.birth_event_id == birth.id
                        )
                    ).scalar_one_or_none()
                    if outcome is None:
                        outcome = CryptoTokenSurvivalOutcome(
                            birth_event_id=birth.id,
                            chain=self.config.chain,
                            token_address=token.token_address,
                            created_at=started,
                        )
                        session.add(outcome)
                    if not outcome.final:
                        for label, value in survival["labels"].items():
                            setattr(outcome, label, value)
                        outcome.details = survival["details"]
                        outcome.final = survival["final"]
                        outcome.last_run_id = run.id
                        outcome.computed_at = started
                        outcomes += 1
                elif dry_run:
                    outcomes += 1

                if len(examples) < 5:
                    examples.append({
                        "token": token.token_address[:16],
                        "symbol": token.symbol,
                        "launch_source": birth.launch_source,
                        "risk_level": snapshot.risk_level,
                        "top10_holder_pct": snapshot.top10_holder_pct,
                        "labels": {
                            k: v for k, v in survival["labels"].items() if v is not None
                        },
                    })

            summary = {
                "status": STATUS_DRY_RUN if dry_run else STATUS_OK,
                "note": TAPE_NOTE,
                "external_calls": 0,
                "window_hours": hours,
                "tokens_considered": len(tokens),
                "birth_events_created": new_births,
                "snapshots_created": snapshots,
                "actor_observations_created": actors,
                "outcomes_updated": outcomes,
                "provider_coverage": coverage_summary,
                "survival_label_mix": dict(sorted(survival_mix.items())),
                "examples": examples,
            }
            if dry_run:
                return summary

            finished = _now()
            run.status = STATUS_OK
            run.finished_at = finished
            run.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
            run.tokens_considered = len(tokens)
            run.birth_events_created = new_births
            run.snapshots_created = snapshots
            run.actor_observations_created = actors
            run.outcomes_updated = outcomes
            run.provider_coverage = coverage_summary
            session.commit()
            summary["tape_run_id"] = run.id
            return summary
        except Exception as exc:
            if dry_run:
                raise
            session.rollback()
            logger.exception("crypto lifecycle tape pass failed")
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
            run.finished_at = _now()
            run.duration_ms = max(
                0, int((run.finished_at - started).total_seconds() * 1000)
            )
            session.commit()
            raise


# --- report -------------------------------------------------------------------


def build_tape_report(session: Session, hours: int = 24, top: int = 5) -> dict:
    """DB-only lifecycle tape report: volumes, provider coverage, survival
    label distribution, risk distribution, actor-pattern examples, missing
    data. Read-only; no external call; never advice."""
    now = _now()
    cutoff = now - timedelta(hours=hours)
    runs = list(session.execute(
        select(CryptoTokenLifecycleRun)
        .where(CryptoTokenLifecycleRun.started_at >= cutoff)
        .order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().all())
    run_ids = [r.id for r in runs]
    births = list(session.execute(
        select(CryptoTokenBirthEvent)
        .where(CryptoTokenBirthEvent.observed_at >= cutoff)
    ).scalars().all())
    snaps = list(session.execute(
        select(CryptoTokenLifecycleSnapshot)
        .where(CryptoTokenLifecycleSnapshot.run_id.in_(run_ids))
    ).scalars().all()) if run_ids else []
    actor_rows = list(session.execute(
        select(CryptoTokenActorObservation)
        .where(CryptoTokenActorObservation.run_id.in_(run_ids))
    ).scalars().all()) if run_ids else []
    outcomes = list(session.execute(
        select(CryptoTokenSurvivalOutcome)
        .where(CryptoTokenSurvivalOutcome.computed_at >= cutoff)
    ).scalars().all())

    coverage_mix: dict[str, int] = {}
    risk_mix: dict[str, int] = {}
    missing_mix: dict[str, int] = {}
    for snap in snaps:
        for item in snap.provider_coverage or []:
            coverage_mix[item] = coverage_mix.get(item, 0) + 1
        risk_mix[snap.risk_level or "unknown"] = risk_mix.get(
            snap.risk_level or "unknown", 0
        ) + 1
        for item in snap.missing_info or []:
            missing_mix[item] = missing_mix.get(item, 0) + 1

    label_mix: dict[str, dict] = {}
    for label in (
        "survived_15m", "survived_1h", "survived_6h", "survived_24h",
        "liquidity_removed", "dead_volume", "severe_risk",
        "graduated_or_migrated", "provider_gap",
    ):
        values = [getattr(o, label) for o in outcomes]
        label_mix[label] = {
            "true": sum(1 for v in values if v is True),
            "false": sum(1 for v in values if v is False),
            "unknown": sum(1 for v in values if v is None),
        }

    # actor-pattern examples: most concentrated holder structures observed
    concentrated = sorted(
        (a for a in actor_rows if (a.holder_distribution or {}).get("top10_holder_pct")),
        key=lambda a: -(a.holder_distribution or {}).get("top10_holder_pct", 0),
    )
    actor_examples = [
        {
            "token": a.token_address[:16],
            "top10_holder_pct": (a.holder_distribution or {}).get("top10_holder_pct"),
            "sniper_pct": (a.holder_distribution or {}).get("sniper_pct"),
            "insider_pct": (a.holder_distribution or {}).get("insider_pct"),
            "bundler_pct": (a.holder_distribution or {}).get("bundler_pct"),
            "creator_address_known": a.creator_address is not None,
        }
        for a in concentrated[:top]
    ]

    return {
        "note": TAPE_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "tape_runs": len(runs),
        "tokens_observed": len({s.token_address for s in snaps}),
        "birth_events_in_window": len(births),
        "snapshots_recorded": len(snaps),
        "actor_observations_recorded": len(actor_rows),
        "outcomes_computed": len(outcomes),
        "outcomes_final": sum(1 for o in outcomes if o.final),
        "provider_coverage_mix": dict(
            sorted(coverage_mix.items(), key=lambda kv: -kv[1])
        ),
        "risk_level_mix": dict(sorted(risk_mix.items(), key=lambda kv: -kv[1])),
        "survival_labels": label_mix,
        "actor_pattern_examples": actor_examples,
        "missing_data_mix": dict(
            sorted(missing_mix.items(), key=lambda kv: -kv[1])[:10]
        ),
        "db_impact_rows": len(runs) + len(births) + len(snaps)
        + len(actor_rows) + len(outcomes),
        "disclaimer": (
            "research tape only — replayable lifecycle observation; labels are "
            "measured token behavior, never advice; no EV, no recommendation, "
            "no sizing, no orders, no wallets, no execution"
        ),
    }
