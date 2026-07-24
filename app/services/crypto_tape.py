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
from sqlalchemy.exc import OperationalError
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
    CryptoWatcherRun,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)

def _completeness_reason(birth, min_liquidity: float) -> str | None:
    """None when the birth is a COMPLETE lifecycle anchor; else the rejection
    reason. Mirrors the --require-complete filter, per token. Canonical home
    (ANCHOR-FEED-MEASUREMENT-001): lives in this provider-free module so the
    exact-cycle anchor feed can classify births without importing anything
    network-capable; `crypto_horizon` re-exports it unchanged."""
    if not birth.first_pair_address:
        return "invalid_pair"
    if birth.initial_price_usd is None:
        return "missing_initial_price"
    if birth.initial_liquidity_usd is None:
        return "liquidity_or_initial_state_missing"
    if birth.initial_liquidity_usd <= 0:
        return "null_initial_liquidity"
    if birth.initial_liquidity_usd <= min_liquidity:
        return "below_min_liquidity"
    return None


# CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001: hard cap on tokens the
# exact-cycle anchor feed will process from one natural discovery cycle.
# Internal safety constant (never an environment knob); comfortably above the
# marketops crypto scan's own bounded per-cycle output. An over-cap cycle is
# skipped loudly (`skipped_cap`) — never silently truncated.
MAX_ANCHOR_FEED_TOKENS_PER_CYCLE = 40

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
        summary = self._assemble_pass(
            session, tokens, started=started, dry_run=dry_run,
            window_hours=hours,
            run_config={"limit": limit, "hours": hours, "chain": self.config.chain},
        )
        summary.pop("_births", None)  # internal accounting, not part of the contract
        return summary

    def record_discovery_run(
        self,
        session: Session,
        crypto_run_id: int,
        token_ids,
        *,
        dry_run: bool = False,
    ) -> dict:
        """CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 — exact-cycle anchor
        materialization. Consolidates EXACTLY the given canonical token ids —
        which must all have been first persisted by crypto discovery run
        `crypto_run_id` — into lifecycle rows via the same `_assemble_pass`
        the manual tape uses (no second lifecycle-anchor implementation).

        Guarantees: exact canonical ids only (no symbol/partial/freshest
        fallback, no substitution); membership validated against the exact
        originating run BEFORE any write (fail-closed — validation failure
        persists nothing); input order preserved; existing anchors
        deduplicated idempotently; one bounded transaction; zero provider
        access; hard-capped at MAX_ANCHOR_FEED_TOKENS_PER_CYCLE (an over-cap
        cycle is skipped loudly, never truncated silently). Measurement
        only — never advice."""
        started = _now()
        received = list(token_ids)  # materialize once (generator-safe)

        def _result(status: str, **extra) -> dict:
            base = {
                "status": status,
                "note": TAPE_NOTE,
                "mode": "exact_cycle",
                "source_crypto_run_id": crypto_run_id,
                "external_calls": 0,
                "tokens_received": len(received),
                "tokens_validated": 0,
                "anchors_attempted": 0,
                "anchors_created": 0,
                "anchors_existing": 0,
                "complete_anchors": 0,
                "incomplete_anchors": 0,
                "skipped_cap": 0,
                "error": None,
                "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
            }
            base.update(extra)
            return base

        run = session.get(CryptoWatcherRun, crypto_run_id)
        if run is None:
            return _result("unknown_run", error="crypto discovery run not found")

        ordered = list(dict.fromkeys(received))  # dedupe, preserve input order
        if not ordered:
            return _result("no_new_tokens")
        if len(ordered) > MAX_ANCHOR_FEED_TOKENS_PER_CYCLE:
            return _result(
                "skipped_cap", skipped_cap=len(ordered),
                error=(
                    f"cycle produced {len(ordered)} tokens > cap "
                    f"{MAX_ANCHOR_FEED_TOKENS_PER_CYCLE}; no anchors created"
                ),
            )

        window_start = _aware(run.started_at)
        window_end = _aware(run.finished_at) or _now()
        tokens: list[CryptoToken] = []
        for token_id in ordered:
            if not isinstance(token_id, str) or not token_id.strip() or len(token_id) > 64:
                return _result("invalid_token", error="malformed canonical token id")
            token = session.execute(
                select(CryptoToken).where(
                    CryptoToken.chain == self.config.chain,
                    CryptoToken.token_address == token_id,
                )
            ).scalars().first()
            if token is None:
                return _result(
                    "invalid_token",
                    error="canonical token id not persisted for this chain",
                )
            if not (window_start <= _aware(token.first_seen_at) <= window_end):
                return _result(
                    "membership_mismatch",
                    error="token was not first persisted by the given discovery run",
                )
            tokens.append(token)

        summary = self._assemble_pass(
            session, tokens, started=started, dry_run=dry_run,
            window_hours=None,
            run_config={
                "mode": "exact_cycle",
                "source_crypto_run_id": crypto_run_id,
                "chain": self.config.chain,
            },
        )
        births = summary.pop("_births", [])
        complete = incomplete = 0
        for birth in births:
            if _completeness_reason(birth, 0.0) is None:
                complete += 1
            else:
                incomplete += 1
        created = summary["birth_events_created"]
        return _result(
            "dry_run" if dry_run else "ok",
            tokens_validated=len(tokens),
            anchors_attempted=len(tokens),
            anchors_created=created,
            anchors_existing=len(tokens) - created,
            complete_anchors=complete,
            incomplete_anchors=incomplete,
            tape_run_id=summary.get("tape_run_id"),
            tokens_considered=summary["tokens_considered"],
            snapshots_created=summary["snapshots_created"],
            outcomes_updated=summary["outcomes_updated"],
        )

    def _assemble_pass(
        self,
        session: Session,
        tokens: list,
        *,
        started: datetime,
        dry_run: bool,
        window_hours: int | None,
        run_config: dict,
    ) -> dict:
        hours = window_hours
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
        births_seen: list[CryptoTokenBirthEvent] = []

        run: CryptoTokenLifecycleRun | None = None
        if not dry_run:
            run = CryptoTokenLifecycleRun(
                status="running", started_at=started, window_hours=hours,
                config=run_config,
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
                births_seen.append(birth)

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
                "_births": births_seen,
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
            # Best-effort error-row record. Under DB-lock contention the
            # error-recording commit can ITSELF fail; that must never mask the
            # original exception or leave the session in a pending-rollback
            # state (the CRYPTO-TAPE-CADENCE-002 crash). After the rollback the
            # run row is detached, so re-add before committing; swallow a
            # second failure and always re-raise the ORIGINAL error so the
            # caller can classify it (e.g. as database_locked).
            try:
                run.status = "error"
                run.error_type = type(exc).__name__
                run.error_message = str(exc)[:2000]
                run.finished_at = _now()
                run.duration_ms = max(
                    0, int((run.finished_at - started).total_seconds() * 1000)
                )
                session.add(run)
                session.commit()
            except Exception:
                session.rollback()
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


# --- CRYPTO-TAPE-CADENCE-001: bounded manual tape session ----------------------
# A convenience wrapper over run_once so repeated passes can MATURE the
# 15m/1h/6h/24h survival horizons (CRYPTO-RETROSPECT-001 found provider_gap
# dominating precisely because horizons lacked observations). NOT a timer,
# NOT a daemon, NOT autonomous: one invocation runs a fixed, hard-capped
# number of derived (zero-external-call) passes with a sleep between, then
# exits. Aborts on abnormal capture status or a detectable MarketOps error.
# Dry-run persists nothing: it prints the planned schedule and runs exactly
# ONE dry probe pass — it never sleeps and never loops.

SESSION_MAX_DURATION_HOURS = 36
SESSION_INTERVAL_MIN_MINUTES = 15
SESSION_INTERVAL_MAX_MINUTES = 120
SESSION_MAX_CAPTURES = 144  # 36h at the 15-minute floor

SESSION_OK = "ok"
SESSION_DRY_RUN = "dry_run"
SESSION_ABORTED = "aborted"

# CRYPTO-TAPE-CADENCE-002: SQLite write-lock resilience. On a shared host the
# baseline/watcher/MarketOps writers can hold the write lock past the DB busy
# timeout, so a capture's run-row INSERT raises "database is locked". A bounded
# app-level retry (mirrors the OPS-013 tick-aggregation idiom) recovers from
# transient contention; a persistent lock aborts loudly and CLEANLY (session
# rolled back first) so the summary path never hits PendingRollbackError.
DB_LOCKED_MAX_ATTEMPTS = 3       # total tries per capture (1 + 2 retries)
DB_LOCKED_RETRY_SECONDS = 3.0    # short wait between attempts
ABORT_DB_LOCKED = "database_locked"

SESSION_NOTE = (
    "Bounded manual tape session: repeated derived lifecycle passes so the "
    "15m/1h/6h/24h survival horizons can mature. Zero external calls, zero "
    "provider-budget impact (each pass reads persisted rows only). Not a "
    "timer, not a daemon, never autonomous; measurement only, never advice."
)


def new_token_ids_for_run(
    session: Session, crypto_run_id: int, chain: str = "solana"
) -> list[str]:
    """Canonical token ids FIRST persisted by exactly the given crypto
    discovery run (first_seen_at within the run's own start/finish window),
    in persistence order. Read-only; zero provider access. Returns [] for an
    unknown run — `record_discovery_run` re-validates and fails closed."""
    run = session.get(CryptoWatcherRun, crypto_run_id)
    if run is None:
        return []
    window_end = run.finished_at or _now().replace(tzinfo=None)
    return list(session.execute(
        select(CryptoToken.token_address).where(
            CryptoToken.chain == chain,
            CryptoToken.first_seen_at >= run.started_at,
            CryptoToken.first_seen_at <= window_end,
        ).order_by(CryptoToken.first_seen_at, CryptoToken.id)
    ).scalars().all())


def _is_db_locked(exc: BaseException | None) -> bool:
    """True when `exc` is (or wraps) a SQLite 'database is locked' error.
    Handles both SQLAlchemy OperationalError (via .orig) and raw
    sqlite3.OperationalError."""
    if exc is None:
        return False
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    text = " ".join(parts).lower()
    return "database is locked" in text or "database table is locked" in text


def _marketops_degraded(session: Session) -> bool:
    """Cheap detectable health check: latest MarketOps run errored. Mirrors
    the tennis session helper; kept local so the crypto lane imports no
    tennis/adapter modules."""
    try:
        from app.models import MarketOpsRun

        latest = session.execute(
            select(MarketOpsRun).order_by(MarketOpsRun.id.desc()).limit(1)
        ).scalars().first()
        return bool(latest is not None and latest.status == "error")
    except Exception:  # pragma: no cover - defensive
        return False


def summarize_tape_session(session: Session, run_ids: list[int]) -> dict:
    """Post-session maturity view over the runs this session persisted.
    Read-only; empty when no run committed (dry-run or abort before the first
    commit). Defensive: if the session is in a bad state after an abort, it
    rolls back and returns gracefully — it NEVER raises (the CADENCE-002
    PendingRollbackError-from-summary crash)."""
    if not run_ids:
        return {
            "available": False,
            "reason": "no runs committed (dry-run, or aborted before first capture)",
        }
    try:
        runs = list(session.execute(
            select(CryptoTokenLifecycleRun).where(
                CryptoTokenLifecycleRun.id.in_(run_ids)
            )
        ).scalars().all())
        outcomes = list(session.execute(
            select(CryptoTokenSurvivalOutcome).where(
                CryptoTokenSurvivalOutcome.last_run_id.in_(run_ids)
            )
        ).scalars().all())
    except Exception:  # a poisoned session must not crash the summary
        try:
            session.rollback()
        except Exception:
            pass
        return {
            "available": False,
            "reason": "summary unavailable (session error after abort; rolled back)",
        }
    totals = {
        "birth_events": sum(r.birth_events_created for r in runs),
        "snapshots": sum(r.snapshots_created for r in runs),
        "actor_observations": sum(r.actor_observations_created for r in runs),
        "outcomes_updated": sum(r.outcomes_updated for r in runs),
    }
    horizon_maturity = {}
    for label, _ in HORIZONS:
        key = f"survived_{label}"
        known = sum(1 for o in outcomes if getattr(o, key) is not None)
        horizon_maturity[key] = {"known": known, "unknown": len(outcomes) - known}
    return {
        "available": True,
        "runs": len(runs),
        "totals": totals,
        "outcomes_tracked": len(outcomes),
        "outcomes_final": sum(1 for o in outcomes if o.final),
        "horizon_maturity": horizon_maturity,
        "provider_gap_true": sum(1 for o in outcomes if o.provider_gap is True),
        "db_impact_rows": len(runs) + totals["birth_events"] + totals["snapshots"]
        + totals["actor_observations"] + totals["outcomes_updated"],
    }


async def run_tape_session(
    session: Session,
    recorder: CryptoLifecycleTapeRecorder | None = None,
    duration_hours: int = 6,
    interval_min: int = 30,
    limit: int | None = None,
    hours: int | None = None,
    dry_run: bool = False,
    sleeper=None,
    max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
    lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
) -> dict:
    """Bounded manual tape session: a fixed, hard-capped number of derived
    run_once passes with a sleep between, then exit. Aborts on abnormal pass
    status or a detectable MarketOps error. Lock-safe: a capture that hits a
    locked DB is rolled back and retried up to `max_lock_attempts`; a
    persistent lock aborts cleanly (reason=database_locked) with the session
    already rolled back. Measurement only — never advice."""
    import asyncio

    sleeper = sleeper or asyncio.sleep
    duration_hours = max(1, min(duration_hours, SESSION_MAX_DURATION_HOURS))
    interval_min = max(
        SESSION_INTERVAL_MIN_MINUTES, min(interval_min, SESSION_INTERVAL_MAX_MINUTES)
    )
    captures_planned = min(
        max(1, (duration_hours * 60) // interval_min), SESSION_MAX_CAPTURES
    )
    recorder = recorder or CryptoLifecycleTapeRecorder()
    started = _now()
    planned_schedule_min = [i * interval_min for i in range(captures_planned)]

    if dry_run:
        # one dry probe proves the pass works; nothing persisted, no sleeping
        probe = recorder.run_once(session, limit=limit, hours=hours, dry_run=True)
        return {
            "note": SESSION_NOTE,
            "status": SESSION_DRY_RUN,
            "aborted": False,
            "abort_reason": None,
            "failed_capture_index": None,
            "started_at": started.isoformat(),
            "duration_hours": duration_hours,
            "interval_min": interval_min,
            "captures_planned": captures_planned,
            "captures_run": 1,
            "capture_statuses": [probe["status"]],
            "planned_schedule_min": planned_schedule_min,
            "rows_written_before_abort": 0,
            "probe": {
                "tokens_considered": probe["tokens_considered"],
                "external_calls": probe["external_calls"],
                "survival_label_mix": probe["survival_label_mix"],
            },
            "provider_gap_trend": None,
            "session_summary": {"available": False,
                                "reason": "no persisted runs (dry-run session)"},
            "tape_run_ids": [],
        }

    captures: list[dict] = []
    run_ids: list[int] = []
    abort_reason = None
    failed_capture_index: int | None = None
    for i in range(captures_planned):
        # --- one capture with bounded, lock-safe retry -------------------------
        result = None
        last_exc: BaseException | None = None
        for attempt in range(1, max_lock_attempts + 1):
            try:
                result = recorder.run_once(session, limit=limit, hours=hours)
                break
            except Exception as exc:
                last_exc = exc
                # ANY failed flush/commit poisons the transaction — always
                # rollback so the session stays usable (and the summary path
                # never hits PendingRollbackError).
                try:
                    session.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                if _is_db_locked(exc) and attempt < max_lock_attempts:
                    logger.warning(
                        "crypto tape session: capture %d hit a locked database — "
                        "retry %d/%d in %.1fs",
                        i + 1, attempt, max_lock_attempts - 1, lock_retry_seconds,
                    )
                    await sleeper(lock_retry_seconds)
                    continue
                break  # non-locked error, or lock retries exhausted
        if result is None:
            failed_capture_index = i
            abort_reason = (
                ABORT_DB_LOCKED if _is_db_locked(last_exc)
                else f"capture {i + 1} raised {type(last_exc).__name__}"
            )
            break

        captures.append(result)
        if result.get("tape_run_id"):
            run_ids.append(result["tape_run_id"])
        if result["status"] != STATUS_OK:
            failed_capture_index = i
            abort_reason = f"capture {i + 1} status={result['status']}"
            break
        if _marketops_degraded(session):
            failed_capture_index = i
            abort_reason = "latest MarketOps run errored"
            break
        if i < captures_planned - 1:
            await sleeper(interval_min * 60)

    def gap_share(capture: dict) -> float | None:
        tokens = capture.get("tokens_considered") or 0
        if not tokens:
            return None
        return round(capture["survival_label_mix"].get("provider_gap", 0) / tokens, 4)

    trend = None
    if len(captures) >= 2:
        first, last = gap_share(captures[0]), gap_share(captures[-1])
        if first is not None and last is not None:
            trend = {
                "first_capture_gap_share": first,
                "last_capture_gap_share": last,
                "direction": (
                    "improving" if last < first
                    else ("worsening" if last > first else "flat")
                ),
            }

    # rows written before an abort, computed from the successful captures
    # (independent of the DB summary, so it survives a poisoned session)
    rows_written_before_abort = sum(
        1  # the run row itself
        + c.get("birth_events_created", 0)
        + c.get("snapshots_created", 0)
        + c.get("actor_observations_created", 0)
        + c.get("outcomes_updated", 0)
        for c in captures
    )

    return {
        "note": SESSION_NOTE,
        "status": SESSION_ABORTED if abort_reason else SESSION_OK,
        "aborted": bool(abort_reason),
        "abort_reason": abort_reason,
        "failed_capture_index": failed_capture_index,
        "started_at": started.isoformat(),
        "duration_hours": duration_hours,
        "interval_min": interval_min,
        "captures_planned": captures_planned,
        "captures_run": len(captures),
        "capture_statuses": [c["status"] for c in captures],
        "planned_schedule_min": planned_schedule_min,
        "provider_gap_trend": trend,
        "rows_written_before_abort": rows_written_before_abort,
        "session_summary": summarize_tape_session(session, run_ids),
        "tape_run_ids": run_ids,
    }
