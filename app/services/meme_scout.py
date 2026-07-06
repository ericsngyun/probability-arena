"""Meme/news attention scout (MEME-NEWS-001, Part A + B): read-only token
genesis + catalyst-velocity surveillance over the existing Solana DEX lane.

For the newest token profiles / boosted tokens it records an **attention_score**
— a composite interest/velocity signal (freshness + liquidity/volume growth +
boost velocity + profile completeness + social/catalyst presence, penalized by
the existing read-only risk overlay and dampened by provider confidence) — plus
generic **catalyst events** in a source-agnostic table.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): read-only discovery and
scoring ONLY. An `attention_score` is an interest/velocity signal for human
review — it is NOT a buy score, trade score, EV, alpha score, sizing, or
recommendation, and it triggers no behavior. No dollar EV, no orders, no
wallets/keys, no swaps, no signing, no execution. Inputs are the public
read-only DexScreener GETs already in scope plus our own persisted rows; no
authenticated scraping. Catalyst sources beyond dexscreener (rss/x/discord/
telegram) are schema placeholders only — added later ONLY if explicitly
configured.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.dexscreener import DexScreenerAdapter, PairData, TokenProfile
from app.config import Settings, get_settings
from app.models import (
    CryptoToken,
    CryptoTokenRiskAssessment,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
    MemeScoutRun,
)

logger = logging.getLogger(__name__)

# attention_score component weights (sum to 1.0 before risk penalty / provider
# damping). Freshness + growth + boost velocity dominate; metadata/social are
# secondary catalysts.
W_FRESHNESS = 0.20
W_LIQUIDITY_GROWTH = 0.20
W_VOLUME_GROWTH = 0.20
W_BOOST_VELOCITY = 0.20
W_METADATA = 0.10
W_SOCIAL = 0.10

FRESHNESS_HORIZON_SECONDS = 7 * 86400  # linear decay to 0 over 7 days

# risk_level -> multiplicative penalty on the raw attention score
RISK_PENALTY = {
    "severe": 0.6,
    "high": 0.4,
    "medium": 0.2,
    "low": 0.0,
    "unknown": 0.1,
    None: 0.1,
}

CATALYST_SOURCE_DEX = "dexscreener"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clamp01(x: float | None) -> float:
    if x is None:
        return 0.0
    return max(0.0, min(1.0, x))


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / previous, 4)


@dataclass
class MemeScoutConfig:
    chain: str = "solana"
    limit: int = 30
    version: str = "v1"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "MemeScoutConfig":
        s = settings or get_settings()
        return cls(chain=s.crypto_chain, limit=s.meme_scout_limit, version=s.meme_scout_version)


class MemeScoutService:
    """Scores the newest/boosted tokens for attention and records catalysts.
    Read-only over the public DexScreener GETs + our own rows."""

    def __init__(
        self,
        adapter: DexScreenerAdapter | None = None,
        config: MemeScoutConfig | None = None,
    ):
        self.config = config or MemeScoutConfig.from_settings()
        self.adapter = adapter or DexScreenerAdapter()

    # --- catalyst helpers ---------------------------------------------------

    def _record_catalyst(
        self,
        session: Session,
        run: MemeScoutRun,
        *,
        subject_ref: str,
        catalyst_type: str,
        now: datetime,
        subject_type: str = "token",
        source: str = CATALYST_SOURCE_DEX,
        magnitude: float | None = None,
        detail: dict | None = None,
    ) -> None:
        session.add(
            MemeCatalystEvent(
                run_id=run.id,
                source=source,
                subject_type=subject_type,
                subject_ref=subject_ref[:256],
                catalyst_type=catalyst_type,
                magnitude=magnitude,
                observed_at=now,
                detail=detail,
                created_at=now,
            )
        )

    # --- risk overlay (read-only; no re-assessment) -------------------------

    def _risk_overlay(self, session: Session, token_address: str) -> tuple[str | None, float | None, float]:
        """Latest existing risk assessment for the token -> (level, score,
        provider_confidence). No provider call — reads what the crypto lane
        already produced; missing provider data lowers confidence (the
        provider_unknown / holder-sniper-insider gap)."""
        risk = session.execute(
            select(CryptoTokenRiskAssessment)
            .where(CryptoTokenRiskAssessment.token_address == token_address)
            .order_by(CryptoTokenRiskAssessment.id.desc())
        ).scalars().first()
        if risk is None:
            return None, None, 0.25  # provider_unknown gap
        level = risk.composite_risk_level or risk.risk_level
        score = risk.composite_risk_score if risk.composite_risk_score is not None else risk.risk_score
        names = [n for n in (risk.provider_names or []) if n not in ("heuristic", "heuristics")]
        confidence = 1.0 if names else 0.5
        return level, score, confidence

    # --- scoring ------------------------------------------------------------

    def _score_token(
        self,
        session: Session,
        run: MemeScoutRun,
        profile: TokenProfile,
        pair: PairData | None,
        now: datetime,
        is_boost: bool,
    ) -> int:
        addr = profile.token_address
        token = session.execute(
            select(CryptoToken).where(CryptoToken.token_address == addr)
        ).scalars().first()
        prev = session.execute(
            select(MemeAttentionSnapshot)
            .where(MemeAttentionSnapshot.token_address == addr)
            .order_by(MemeAttentionSnapshot.id.desc())
        ).scalars().first()

        first_seen = (
            _aware(token.first_seen_at) if token is not None
            else (_aware(prev.first_seen_at) if prev is not None else now)
        )
        age_seconds = max(0, int((now - first_seen).total_seconds())) if first_seen else 0

        liquidity = pair.liquidity_usd if pair else None
        vol_1h = pair.volume_1h_usd if pair else None
        liq_growth = _growth(liquidity, prev.liquidity_usd if prev else None)
        vol_growth = _growth(vol_1h, prev.volume_1h_usd if prev else None)

        boost_amount = profile.boost_amount
        boost_velocity = None
        if prev is not None and prev.boost_amount is not None and boost_amount is not None:
            hours = max((now - _aware(prev.observed_at)).total_seconds() / 3600.0, 1e-6)
            boost_velocity = round((boost_amount - prev.boost_amount) / hours, 4)

        links = profile.raw.get("links") if isinstance(profile.raw, dict) else None
        social_count = len(links) if isinstance(links, list) else 0
        has_social = social_count > 0
        symbol = pair.base_token_symbol if pair else None
        name = pair.base_token_name if pair else None
        present = sum(
            1 for v in (profile.description, profile.url, symbol, name, has_social or None)
            if v
        )
        completeness = round(present / 5.0, 4)

        level, risk_score, provider_confidence = self._risk_overlay(session, addr)

        # component sub-scores (0..1)
        freshness = _clamp01(1 - age_seconds / FRESHNESS_HORIZON_SECONDS)
        liq_growth_s = _clamp01(liq_growth)      # +100% growth -> 1.0
        vol_growth_s = _clamp01(vol_growth)
        boost_s = _clamp01(
            (0.5 if (boost_amount or 0) > 0 else 0.0)
            + (min((boost_velocity or 0) / 1000.0, 0.5) if boost_velocity else 0.0)
        )
        meta_s = _clamp01(completeness)
        social_s = _clamp01(social_count / 3.0) if has_social else 0.0

        raw = (
            W_FRESHNESS * freshness
            + W_LIQUIDITY_GROWTH * liq_growth_s
            + W_VOLUME_GROWTH * vol_growth_s
            + W_BOOST_VELOCITY * boost_s
            + W_METADATA * meta_s
            + W_SOCIAL * social_s
        )
        penalty = RISK_PENALTY.get(level, 0.1)
        attention = round(
            _clamp01(raw * (1 - penalty) * (0.5 + 0.5 * provider_confidence)), 4
        )

        components = {
            "freshness": round(freshness, 4),
            "liquidity_growth": round(liq_growth_s, 4),
            "volume_growth": round(vol_growth_s, 4),
            "boost_velocity": round(boost_s, 4),
            "metadata_completeness": round(meta_s, 4),
            "social_presence": round(social_s, 4),
            "raw": round(raw, 4),
            "risk_penalty": penalty,
            "provider_confidence": provider_confidence,
            "note": "attention/interest signal only — not a buy/trade/EV/alpha score",
        }

        session.add(
            MemeAttentionSnapshot(
                run_id=run.id,
                chain=self.config.chain,
                token_address=addr,
                pair_address=pair.pair_address if pair else None,
                symbol=symbol,
                name=name,
                first_seen_at=first_seen,
                token_age_seconds=age_seconds,
                price_usd=pair.price_usd if pair else None,
                liquidity_usd=liquidity,
                volume_5m_usd=pair.volume_5m_usd if pair else None,
                volume_1h_usd=vol_1h,
                volume_24h_usd=pair.volume_24h_usd if pair else None,
                price_change_5m=pair.price_change_5m if pair else None,
                price_change_1h=pair.price_change_1h if pair else None,
                liquidity_growth=liq_growth,
                volume_growth=vol_growth,
                boost_amount=boost_amount,
                boost_velocity=boost_velocity,
                profile_completeness=completeness,
                has_social=has_social,
                social_links_count=social_count,
                risk_level=level,
                risk_score=risk_score,
                provider_confidence=provider_confidence,
                attention_score=attention,
                score_components=components,
                observed_at=now,
                created_at=now,
            )
        )

        # catalyst events
        catalysts = 0
        self._record_catalyst(
            session, run, subject_ref=addr, catalyst_type="profile_seen", now=now,
            detail={"url": profile.url, "has_social": has_social, "completeness": completeness},
        )
        catalysts += 1
        if (boost_amount or 0) > 0:
            self._record_catalyst(
                session, run, subject_ref=addr, catalyst_type="boost", now=now,
                magnitude=boost_amount, detail={"is_boost_feed": is_boost},
            )
            catalysts += 1
        if boost_velocity is not None and boost_velocity > 0:
            self._record_catalyst(
                session, run, subject_ref=addr, catalyst_type="boost_increase", now=now,
                magnitude=boost_velocity, detail={"per_hour": boost_velocity},
            )
            catalysts += 1
        if has_social:
            self._record_catalyst(
                session, run, subject_ref=addr, catalyst_type="social_present", now=now,
                magnitude=float(social_count),
                detail={"links": links[:8] if isinstance(links, list) else None},
            )
            catalysts += 1
        return catalysts

    # --- scan ---------------------------------------------------------------

    async def scan_once(self, session: Session, limit: int | None = None) -> MemeScoutRun:
        """One read-only attention pass over the newest profiles + boosted
        tokens. Adapter failures degrade to empty lists (never raise), so a
        provider outage yields an ok run that simply scored nothing."""
        now = _now()
        run = MemeScoutRun(status="running", started_at=now, created_at=now)
        session.add(run)
        session.flush()
        try:
            profiles = await self.adapter.fetch_latest_token_profiles()
            boosts = await self.adapter.fetch_latest_boosted_tokens()
            run.profiles_seen = len(profiles)
            run.boosts_seen = len(boosts)

            # boosts carry boost_amount and take precedence over plain profiles
            merged: dict[str, tuple[TokenProfile, bool]] = {}
            for p in profiles:
                merged.setdefault(p.token_address, (p, False))
            for b in boosts:
                merged[b.token_address] = (b, True)

            cap = limit if limit is not None else self.config.limit
            scored = catalysts = 0
            for addr, (profile, is_boost) in list(merged.items())[:cap]:
                pairs = await self.adapter.fetch_pairs_for_token(addr)
                best = max(pairs, key=lambda pr: pr.liquidity_usd or 0.0, default=None)
                catalysts += self._score_token(session, run, profile, best, now, is_boost)
                scored += 1

            run.tokens_scored = scored
            run.catalysts_created = catalysts
            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = int((run.finished_at - now).total_seconds() * 1000)
            session.commit()
            return run
        except Exception as exc:  # unexpected (DB) errors; adapters never raise
            session.rollback()
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:1000]
            run.finished_at = _now()
            session.add(run)
            session.commit()
            logger.exception("meme scout scan failed")
            raise


# --- report services (read-only aggregation) --------------------------------


@dataclass
class MemeScoutReport:
    note: str
    total_snapshots: int
    total_runs: int
    latest_run: dict | None
    by_risk_level: dict
    attention_p50: float | None
    attention_p90: float | None
    top_attention: list[dict] = field(default_factory=list)
    provider_confidence_avg: float | None = None


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 4)


class MemeScoutReportService:
    """Aggregate view of attention snapshots. Measurement only."""

    def build(self, session: Session, recent_limit: int = 10):
        rows = session.execute(
            select(MemeAttentionSnapshot).order_by(MemeAttentionSnapshot.id.desc()).limit(2000)
        ).scalars().all()
        total_runs = session.execute(select(MemeScoutRun)).scalars().all()
        latest = session.execute(
            select(MemeScoutRun).order_by(MemeScoutRun.id.desc())
        ).scalars().first()

        by_level: dict[str, int] = {}
        scores: list[float] = []
        confidences: list[float] = []
        for r in rows:
            key = r.risk_level or "unknown"
            by_level[key] = by_level.get(key, 0) + 1
            if r.attention_score is not None:
                scores.append(r.attention_score)
            if r.provider_confidence is not None:
                confidences.append(r.provider_confidence)

        # newest-per-token, ranked by attention
        seen: set[str] = set()
        top: list[dict] = []
        for r in sorted(rows, key=lambda x: -(x.attention_score or 0)):
            if r.token_address in seen:
                continue
            seen.add(r.token_address)
            top.append(
                {
                    "token": r.token_address[:16],
                    "symbol": r.symbol,
                    "attention_score": r.attention_score,
                    "age_seconds": r.token_age_seconds,
                    "boost_amount": r.boost_amount,
                    "liquidity_usd": r.liquidity_usd,
                    "risk_level": r.risk_level,
                    "provider_confidence": r.provider_confidence,
                }
            )
            if len(top) >= recent_limit:
                break

        return MemeScoutReport(
            note=(
                "Read-only attention/velocity intelligence. attention_score is an "
                "interest signal for human review — not a buy/trade/EV/alpha score, "
                "no sizing, no orders, no execution."
            ),
            total_snapshots=len(rows),
            total_runs=len(total_runs),
            latest_run=(
                {
                    "id": latest.id, "status": latest.status,
                    "profiles_seen": latest.profiles_seen, "boosts_seen": latest.boosts_seen,
                    "tokens_scored": latest.tokens_scored,
                    "catalysts_created": latest.catalysts_created,
                }
                if latest else None
            ),
            by_risk_level=by_level,
            attention_p50=_pct(scores, 50),
            attention_p90=_pct(scores, 90),
            top_attention=top,
            provider_confidence_avg=(
                round(sum(confidences) / len(confidences), 4) if confidences else None
            ),
        )


@dataclass
class CatalystReport:
    note: str
    total: int
    by_type: dict
    by_source: dict
    by_subject_type: dict
    recent: list[dict] = field(default_factory=list)


class CatalystReportService:
    """Aggregate view of the generic catalyst-event stream."""

    def build(self, session: Session, recent_limit: int = 15):
        rows = session.execute(
            select(MemeCatalystEvent).order_by(MemeCatalystEvent.id.desc()).limit(2000)
        ).scalars().all()
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_subject: dict[str, int] = {}
        for r in rows:
            by_type[r.catalyst_type] = by_type.get(r.catalyst_type, 0) + 1
            by_source[r.source] = by_source.get(r.source, 0) + 1
            by_subject[r.subject_type] = by_subject.get(r.subject_type, 0) + 1
        recent = [
            {
                "type": r.catalyst_type, "source": r.source,
                "subject": r.subject_ref[:20], "magnitude": r.magnitude,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            }
            for r in rows[:recent_limit]
        ]
        return CatalystReport(
            note=(
                "Generic catalyst events (informational, never a trade trigger). "
                "Only public read-only dexscreener sources populate this today; "
                "rss/x/discord/telegram are schema placeholders for explicit future config."
            ),
            total=len(rows),
            by_type=dict(sorted(by_type.items(), key=lambda i: -i[1])),
            by_source=by_source,
            by_subject_type=by_subject,
            recent=recent,
        )
