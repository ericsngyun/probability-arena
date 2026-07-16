"""Crypto Arena scout (CRYPTO-001): read-only Solana memecoin surveillance.

Three services over public DEX data:

- CryptoDiscoveryService — fetches token profiles/boosts/pairs from the
  configured provider (DEX Screener), upserts tokens and pairs, records
  discovery events, price ticks, and (optionally) risk assessments, then
  runs signal detection. One pass = one crypto_watcher_runs audit row.
- CryptoSignalService — deterministic detectors comparing the latest tick to
  the previous one (plus provider risk reads), deduped per token+type within
  CRYPTO_SIGNAL_COOLDOWN_SECONDS.
- CryptoReportService — aggregate view of tokens/pairs/ticks/signals/risk
  and provider errors.

Signals are surveillance/risk telemetry for later human review and (in a
future gated milestone) paper simulation. They carry no EV, no sizing, no
trade directives. This module contains no wallet, key, swap, transaction,
order, or execution code — see docs/SAFETY_BOUNDARIES.md.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.dexscreener import DexScreenerAdapter, PairData, TokenProfile
from app.config import Settings, get_settings
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenDiscoveryEvent,
    CryptoTokenRiskAssessment,
    CryptoWatcherRun,
)
from app.services.crypto_risk import CryptoRiskProvider, RiskAssessment, get_risk_provider

logger = logging.getLogger(__name__)

SIGNAL_NEW_PAIR = "new_pair"
SIGNAL_LIQUIDITY_APPEARED = "liquidity_appeared"
SIGNAL_VOLUME_SPIKE = "volume_spike"
SIGNAL_PRICE_MOMENTUM = "price_momentum"
SIGNAL_BOOST_DETECTED = "boost_detected"
SIGNAL_HOLDER_RISK = "holder_risk"
SIGNAL_RUG_RISK = "rug_risk"
SIGNAL_LIQUIDITY_REMOVED = "liquidity_removed"
SIGNAL_SUSPICIOUS_SUPPLY = "suspicious_supply_control"

ALL_SIGNAL_TYPES = (
    SIGNAL_NEW_PAIR,
    SIGNAL_LIQUIDITY_APPEARED,
    SIGNAL_VOLUME_SPIKE,
    SIGNAL_PRICE_MOMENTUM,
    SIGNAL_BOOST_DETECTED,
    SIGNAL_HOLDER_RISK,
    SIGNAL_RUG_RISK,
    SIGNAL_LIQUIDITY_REMOVED,
    SIGNAL_SUSPICIOUS_SUPPLY,
)

SIGNAL_STATUS_NEW = "new"

# Detector tuning (deterministic; config holds the operational thresholds,
# these hold the shape of each rule)
NEW_PAIR_MAX_AGE_HOURS = 24
VOLUME_SPIKE_MULTIPLIER = 3.0
PRICE_MOMENTUM_MIN_CHANGE_5M_PCT = 15.0
LIQUIDITY_REMOVED_FRACTION = 0.5  # latest below half of previous
HOLDER_RISK_TOP10_PCT = 50.0

EVENT_PROFILE = "profile"
EVENT_BOOST = "boost"
EVENT_PAIR_SEEN = "pair_seen"


@dataclass
class CryptoScoutConfig:
    chain: str = "solana"
    pair_limit: int = 100
    min_liquidity_usd: float = 5000.0
    min_volume_5m_usd: float = 1000.0
    signal_cooldown_seconds: int = 900

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "CryptoScoutConfig":
        s = settings or get_settings()
        return cls(
            chain=s.crypto_chain,
            pair_limit=s.crypto_pair_limit,
            min_liquidity_usd=s.crypto_min_liquidity_usd,
            min_volume_5m_usd=s.crypto_min_volume_5m_usd,
            signal_cooldown_seconds=s.crypto_signal_cooldown_seconds,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def latest_tick_for_pair(session: Session, pair_address: str) -> CryptoPriceTick | None:
    return session.execute(
        select(CryptoPriceTick)
        .where(CryptoPriceTick.pair_address == pair_address)
        .order_by(CryptoPriceTick.observed_at.desc(), CryptoPriceTick.id.desc())
    ).scalars().first()


class CryptoSignalService:
    """Deterministic, informational-only signal detection over crypto ticks
    and optional risk assessments."""

    def __init__(self, config: CryptoScoutConfig | None = None):
        self.config = config or CryptoScoutConfig.from_settings()

    def _last_signal_at(
        self, session: Session, token_address: str, signal_type: str
    ) -> datetime | None:
        row = session.execute(
            select(CryptoOpportunitySignal.created_at)
            .where(
                CryptoOpportunitySignal.chain == self.config.chain,
                CryptoOpportunitySignal.token_address == token_address,
                CryptoOpportunitySignal.signal_type == signal_type,
            )
            .order_by(
                CryptoOpportunitySignal.created_at.desc(), CryptoOpportunitySignal.id.desc()
            )
        ).scalars().first()
        return _aware(row)

    def _passes_cooldown(
        self, session: Session, signal: CryptoOpportunitySignal, now: datetime
    ) -> bool:
        last = self._last_signal_at(session, signal.token_address, signal.signal_type)
        if last is None:
            return True
        return (now - last) >= timedelta(seconds=self.config.signal_cooldown_seconds)

    def detect(
        self,
        pair: CryptoPair,
        previous: CryptoPriceTick | None,
        tick: CryptoPriceTick,
        risk: RiskAssessment | None,
        observed_at: datetime,
    ) -> list[CryptoOpportunitySignal]:
        """All applicable signals for one new tick (pre-cooldown). Risk-based
        detectors stay inactive when no risk assessment is available."""
        cfg = self.config
        signals: list[CryptoOpportunitySignal] = []

        def make(signal_type: str, reason: str, evidence: dict) -> None:
            signals.append(
                CryptoOpportunitySignal(
                    chain=cfg.chain,
                    token_address=tick.token_address,
                    pair_address=tick.pair_address,
                    signal_type=signal_type,
                    signal_status=SIGNAL_STATUS_NEW,
                    observed_at=observed_at,
                    reason=reason,
                    evidence=evidence,
                    raw_payload=tick.raw_payload,
                    created_at=observed_at,
                )
            )

        pair_created_at = _aware(pair.pair_created_at)
        if (
            previous is None
            and pair_created_at is not None
            and (observed_at - pair_created_at) <= timedelta(hours=NEW_PAIR_MAX_AGE_HOURS)
        ):
            age_hours = (observed_at - pair_created_at).total_seconds() / 3600
            make(
                SIGNAL_NEW_PAIR,
                f"New pair first observed {age_hours:.1f}h after creation "
                f"(<= {NEW_PAIR_MAX_AGE_HOURS}h window)",
                {
                    "pair_created_at": pair_created_at.isoformat(),
                    "age_hours": round(age_hours, 2),
                    "liquidity_usd": tick.liquidity_usd,
                },
            )

        prev_liquidity = previous.liquidity_usd if previous is not None else None
        if (
            tick.liquidity_usd is not None
            and tick.liquidity_usd >= cfg.min_liquidity_usd
            and previous is not None
            and (prev_liquidity is None or prev_liquidity < cfg.min_liquidity_usd)
        ):
            make(
                SIGNAL_LIQUIDITY_APPEARED,
                f"Liquidity rose from {prev_liquidity or 0:.0f} to {tick.liquidity_usd:.0f} USD "
                f"(>= {cfg.min_liquidity_usd:.0f})",
                {
                    "old_liquidity_usd": prev_liquidity,
                    "new_liquidity_usd": tick.liquidity_usd,
                    "min_liquidity_usd": cfg.min_liquidity_usd,
                },
            )

        if (
            previous is not None
            and prev_liquidity is not None
            and prev_liquidity >= cfg.min_liquidity_usd
            and tick.liquidity_usd is not None
            and tick.liquidity_usd < prev_liquidity * LIQUIDITY_REMOVED_FRACTION
        ):
            make(
                SIGNAL_LIQUIDITY_REMOVED,
                f"Liquidity dropped from {prev_liquidity:.0f} to {tick.liquidity_usd:.0f} USD "
                f"(below {LIQUIDITY_REMOVED_FRACTION:.0%} of previous) — possible pull",
                {
                    "old_liquidity_usd": prev_liquidity,
                    "new_liquidity_usd": tick.liquidity_usd,
                    "fraction_threshold": LIQUIDITY_REMOVED_FRACTION,
                },
            )

        prev_volume = previous.volume_5m_usd if previous is not None else None
        if (
            tick.volume_5m_usd is not None
            and tick.volume_5m_usd >= cfg.min_volume_5m_usd
            and prev_volume is not None
            and prev_volume > 0
            and tick.volume_5m_usd >= prev_volume * VOLUME_SPIKE_MULTIPLIER
        ):
            make(
                SIGNAL_VOLUME_SPIKE,
                f"5m volume spiked from {prev_volume:.0f} to {tick.volume_5m_usd:.0f} USD "
                f"(>= {VOLUME_SPIKE_MULTIPLIER:.0f}x and >= {cfg.min_volume_5m_usd:.0f} USD)",
                {
                    "old_volume_5m_usd": prev_volume,
                    "new_volume_5m_usd": tick.volume_5m_usd,
                    "multiplier": VOLUME_SPIKE_MULTIPLIER,
                    "min_volume_5m_usd": cfg.min_volume_5m_usd,
                },
            )

        if (
            tick.price_change_5m is not None
            and tick.price_change_5m >= PRICE_MOMENTUM_MIN_CHANGE_5M_PCT
        ):
            make(
                SIGNAL_PRICE_MOMENTUM,
                f"Price up {tick.price_change_5m:.1f}% in 5m "
                f"(>= {PRICE_MOMENTUM_MIN_CHANGE_5M_PCT:.0f}%)",
                {
                    "price_change_5m_pct": tick.price_change_5m,
                    "price_change_1h_pct": tick.price_change_1h,
                    "price_usd": tick.price_usd,
                },
            )

        boosts_active = (tick.raw_payload or {}).get("boosts_active") or 0
        prev_boosts = (
            (previous.raw_payload or {}).get("boosts_active") or 0
            if previous is not None
            else 0
        )
        if boosts_active and not prev_boosts:
            make(
                SIGNAL_BOOST_DETECTED,
                f"Token boost detected ({boosts_active} active boost(s) on DEX Screener)",
                {"boosts_active": boosts_active},
            )

        if risk is not None:
            # Risk evidence can come from a raw provider read (CRYPTO-001
            # mock: flag keys) or the CRYPTO-002 risk engine (category codes
            # in flags["categories"]). Absent risk data fires nothing.
            from app.services.crypto_risk_engine import (
                HOLDER_CATEGORIES,
                SUPPLY_CATEGORIES,
            )

            flags = risk.flags or {}
            categories = set(flags.get("categories") or [])
            if (
                risk.risk_level in ("high", "critical", "severe")
                or flags.get("honeypot")
                or flags.get("rug_risk")
                or "provider_rug_flag" in categories
                or "provider_honeypot_flag" in categories
            ):
                make(
                    SIGNAL_RUG_RISK,
                    f"Risk source {risk.provider} flags rug risk "
                    f"(level={risk.risk_level}, score={risk.risk_score}) — "
                    "avoid/flag verdict, not a trade direction",
                    {"risk_level": risk.risk_level, "risk_score": risk.risk_score,
                     "flags": flags},
                )
            top10 = flags.get("top10_holder_pct")
            if (
                flags.get("holder_risk")
                or (isinstance(top10, (int, float)) and top10 >= HOLDER_RISK_TOP10_PCT)
                or categories & HOLDER_CATEGORIES
            ):
                make(
                    SIGNAL_HOLDER_RISK,
                    f"Holder concentration risk (top10 holds {top10}%)" if top10 is not None
                    else f"Risk source {risk.provider} flags holder concentration",
                    {"top10_holder_pct": top10, "flags": flags},
                )
            if (
                flags.get("mint_authority_enabled")
                or flags.get("freeze_authority_enabled")
                or flags.get("suspicious_supply_control")
                or categories & SUPPLY_CATEGORIES
            ):
                make(
                    SIGNAL_SUSPICIOUS_SUPPLY,
                    f"Risk source {risk.provider} flags supply control "
                    f"(mint_authority={flags.get('mint_authority_enabled')}, "
                    f"freeze_authority={flags.get('freeze_authority_enabled')})",
                    {"flags": flags},
                )

        return signals

    def persist_deduped(
        self, session: Session, signals: list[CryptoOpportunitySignal], now: datetime
    ) -> int:
        """Add signals that pass the per-(token, type) cooldown; returns the
        number persisted (session flush left to the caller)."""
        created = 0
        for signal in signals:
            if self._passes_cooldown(session, signal, now):
                session.add(signal)
                session.flush()  # visible to subsequent cooldown checks
                created += 1
            else:
                logger.debug(
                    "Cooldown: suppressing %s for %s", signal.signal_type, signal.token_address
                )
        return created


class CryptoDiscoveryService:
    """One read-only discovery pass over the configured provider."""

    def __init__(
        self,
        adapter: DexScreenerAdapter | None = None,
        risk_provider: CryptoRiskProvider | None = None,
        signal_service: CryptoSignalService | None = None,
        config: CryptoScoutConfig | None = None,
        risk_engine=None,
    ):
        from app.services.crypto_risk_engine import get_risk_engine

        self.config = config or CryptoScoutConfig.from_settings()
        self.adapter = adapter or DexScreenerAdapter()
        # None means "flag off": risk detectors stay inactive
        self.risk_provider = (
            risk_provider if risk_provider is not None else get_risk_provider()
        )
        # CRYPTO-002: engine takes precedence over the raw provider when on
        self.risk_engine = risk_engine if risk_engine is not None else get_risk_engine()
        self.signal_service = signal_service or CryptoSignalService(self.config)
        self.last_ledger: dict | None = None  # GATE-001 run-scoped request ledger

    def _upsert_token(
        self,
        session: Session,
        token_address: str,
        now: datetime,
        symbol: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
    ) -> CryptoToken:
        token = session.execute(
            select(CryptoToken).where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.token_address == token_address,
            )
        ).scalar_one_or_none()
        if token is None:
            token = CryptoToken(
                chain=self.config.chain,
                token_address=token_address,
                first_seen_at=now,
                created_at=now,
            )
            session.add(token)
        token.last_seen_at = now
        if symbol:
            token.symbol = symbol
        if name:
            token.name = name
        if metadata:
            token.token_metadata = {**(token.token_metadata or {}), **metadata}
        session.flush()
        return token

    def _upsert_pair(self, session: Session, pair: PairData, now: datetime) -> tuple[CryptoPair, bool]:
        """(row, is_new)."""
        row = session.execute(
            select(CryptoPair).where(
                CryptoPair.chain == self.config.chain,
                CryptoPair.pair_address == pair.pair_address,
            )
        ).scalar_one_or_none()
        is_new = row is None
        if row is None:
            row = CryptoPair(
                chain=self.config.chain,
                pair_address=pair.pair_address,
                base_token_address=pair.base_token_address,
                first_seen_at=now,
                created_at=now,
            )
            session.add(row)
        row.quote_token_address = pair.quote_token_address or row.quote_token_address
        row.dex_id = pair.dex_id or row.dex_id
        row.url = pair.url or row.url
        row.pair_created_at = pair.pair_created_at or row.pair_created_at
        row.pair_metadata = {"base_symbol": pair.base_token_symbol}
        row.last_seen_at = now
        session.flush()
        return row, is_new

    def _record_event(
        self,
        session: Session,
        token_address: str,
        event_type: str,
        now: datetime,
        pair_address: str | None = None,
        raw: dict | None = None,
    ) -> None:
        session.add(
            CryptoTokenDiscoveryEvent(
                chain=self.config.chain,
                token_address=token_address,
                pair_address=pair_address,
                source=self.adapter.source_name,
                event_type=event_type,
                observed_at=now,
                raw_payload=raw,
                created_at=now,
            )
        )

    def _record_tick(
        self, session: Session, pair: PairData, now: datetime
    ) -> CryptoPriceTick:
        tick = CryptoPriceTick(
            chain=self.config.chain,
            token_address=pair.base_token_address,
            pair_address=pair.pair_address,
            observed_at=now,
            price_usd=pair.price_usd,
            liquidity_usd=pair.liquidity_usd,
            volume_5m_usd=pair.volume_5m_usd,
            volume_1h_usd=pair.volume_1h_usd,
            volume_24h_usd=pair.volume_24h_usd,
            price_change_5m=pair.price_change_5m,
            price_change_1h=pair.price_change_1h,
            market_cap=pair.market_cap,
            fdv=pair.fdv,
            raw_payload={"boosts_active": pair.boosts_active, "dex_id": pair.dex_id},
            created_at=now,
        )
        session.add(tick)
        session.flush()
        return tick

    def _record_risk(
        self, session: Session, assessment: RiskAssessment, now: datetime
    ) -> CryptoTokenRiskAssessment:
        row = CryptoTokenRiskAssessment(
            chain=self.config.chain,
            token_address=assessment.token_address,
            provider=assessment.provider,
            risk_score=assessment.risk_score,
            risk_level=assessment.risk_level,
            flags=assessment.flags,
            raw_payload=assessment.raw,
            created_at=now,
        )
        session.add(row)
        session.flush()
        return row

    def describe_providers(self, limit: int | None = None) -> list:
        """Typed provider descriptors derived from THIS constructed graph — the
        single source the preflight plan and the default run policy both use, so
        the plan cannot drift from runtime (GATE-001)."""
        from app.services.crypto_provider_policy import Provider, ProviderDescriptor

        limit = limit or self.config.pair_limit
        descriptors = [
            ProviderDescriptor(
                provider=Provider.DEXSCREENER,
                role="token discovery",
                direct=True,
                enabled=True,
                paid=False,
                mandatory=True,
                per_token=False,
                max_requests=2 + limit,
                config_source="mandatory",
                fallback="none (required)",
                cap=2 + limit,
            )
        ]
        if self.risk_engine is not None:
            descriptors.extend(self.risk_engine.registry.describe(limit))
        else:
            # engine off: risk providers exist in config but are not dispatched
            from app.services.crypto_risk_engine import CryptoRiskProviderRegistry

            descriptors.extend(
                CryptoRiskProviderRegistry(adapters=[]).describe(limit)
            )
        return descriptors

    async def scan_once(
        self, session: Session, limit: int | None = None, *, policy=None
    ) -> CryptoWatcherRun:
        """Governed discovery pass (GATE-001). Requires an explicit run-scoped
        provider policy: either passed as ``policy`` or already installed as the
        ambient run context. A missing policy fails closed before any provider
        request; a denied mandatory provider rejects the mode before scanning."""
        from app.services.crypto_provider_policy import (
            MandatoryProviderDeniedError,
            MissingPolicyError,
            current_context,
            provider_run,
        )

        limit = limit or self.config.pair_limit
        ctx = current_context()
        if policy is None and ctx is None:
            raise MissingPolicyError(
                "crypto discovery requires an explicit provider policy"
            )
        if policy is not None and ctx is not None and policy.run_id != ctx.run_id:
            raise MissingPolicyError(
                "explicit policy run_id does not match the installed run context"
            )
        active = policy if policy is not None else ctx.policy
        denied = active.mandatory_denied()
        if denied is not None:
            raise MandatoryProviderDeniedError(
                f"mandatory provider {denied.value} is denied; discovery mode rejected"
            )
        planned = {
            d.provider: d.max_requests
            for d in self.describe_providers(limit)
            if d.max_requests is not None
        }
        if ctx is not None:
            for provider, value in planned.items():
                ctx.ledger.planned_max.setdefault(provider, value)
            run = await self._scan_once_unguarded(session, limit=limit)
            self.last_ledger = ctx.ledger.snapshot()
            return run
        with provider_run(active, planned_max=planned) as run_ctx:
            run = await self._scan_once_unguarded(session, limit=limit)
            self.last_ledger = run_ctx.ledger.snapshot()
            return run

    async def _scan_once_unguarded(
        self, session: Session, limit: int | None = None
    ) -> CryptoWatcherRun:
        """One discovery pass: profiles + boosts -> pairs per token -> upserts,
        events, ticks, optional risk, signals. Errors are recorded on the
        crypto_watcher_runs row and re-raised. Invoked only within a governed
        provider run context (see scan_once)."""
        limit = limit or self.config.pair_limit
        started_at = _now()
        run = CryptoWatcherRun(status="running", started_at=started_at, created_at=started_at)
        session.add(run)
        session.commit()

        try:
            profiles = await self.adapter.fetch_latest_token_profiles()
            boosts = await self.adapter.fetch_latest_boosted_tokens()
            observed_at = _now()

            profile_by_token: dict[str, TokenProfile] = {}
            for profile in profiles:
                profile_by_token.setdefault(profile.token_address, profile)
                self._record_event(
                    session, profile.token_address, EVENT_PROFILE, observed_at,
                    raw=profile.raw,
                )
            boosted: set[str] = set()
            for boost in boosts:
                boosted.add(boost.token_address)
                profile_by_token.setdefault(boost.token_address, boost)
                self._record_event(
                    session, boost.token_address, EVENT_BOOST, observed_at, raw=boost.raw
                )

            tokens_checked = 0
            pairs_checked = 0
            ticks_recorded = 0
            signals_created = 0

            for token_address, profile in profile_by_token.items():
                if pairs_checked >= limit:
                    break
                tokens_checked += 1
                pairs = await self.adapter.fetch_pairs_for_token(token_address)

                metadata = {
                    "description": profile.description,
                    "profile_url": profile.url,
                    "boosted": token_address in boosted,
                }
                best = max(
                    (p for p in pairs), key=lambda p: p.liquidity_usd or 0, default=None
                )
                token_row = self._upsert_token(
                    session,
                    token_address,
                    observed_at,
                    symbol=best.base_token_symbol if best else None,
                    name=best.base_token_name if best else None,
                    metadata=metadata,
                )

                # Upsert pairs + record ticks first (capturing each pair's
                # previous tick), so risk evaluation sees the fresh state.
                pair_states: list[tuple] = []  # (pair_row, previous, tick, pair)
                for pair in pairs:
                    if pairs_checked >= limit:
                        break
                    pairs_checked += 1
                    pair_row, is_new_pair = self._upsert_pair(session, pair, observed_at)
                    if is_new_pair:
                        self._record_event(
                            session,
                            token_address,
                            EVENT_PAIR_SEEN,
                            observed_at,
                            pair_address=pair.pair_address,
                            raw={"dex_id": pair.dex_id, "url": pair.url},
                        )
                    previous = latest_tick_for_pair(session, pair.pair_address)
                    tick = self._record_tick(session, pair, observed_at)
                    ticks_recorded += 1
                    pair_states.append((pair_row, previous, tick, pair))

                # Risk: CRYPTO-002 engine (best pair context) beats the raw
                # CRYPTO-001 provider; neither -> risk detectors stay inactive
                risk: RiskAssessment | None = None
                if self.risk_engine is not None and pair_states:
                    best_state = max(
                        pair_states, key=lambda s: (s[2].liquidity_usd or 0)
                    )
                    evaluation = await self.risk_engine.evaluate(
                        session,
                        token=token_row,
                        pair=best_state[0],
                        tick=best_state[2],
                        previous=best_state[1],
                        pair_count=len(pairs),
                    )
                    risk = evaluation.as_signal_view()
                elif self.risk_provider is not None:
                    risk = await self.risk_provider.assess(token_address)
                    if risk is not None:
                        self._record_risk(session, risk, observed_at)

                for pair_row, previous, tick, _pair in pair_states:
                    detected = self.signal_service.detect(
                        pair_row, previous, tick, risk, observed_at
                    )
                    signals_created += self.signal_service.persist_deduped(
                        session, detected, observed_at
                    )

            run.tokens_checked = tokens_checked
            run.pairs_checked = pairs_checked
            run.ticks_recorded = ticks_recorded
            run.signals_created = signals_created
            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            return run
        except Exception as exc:
            session.rollback()
            logger.exception("Crypto scan pass failed")
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            raise


class CryptoReportService:
    """Aggregate, informational-only view of the crypto surveillance lane."""

    def build(self, session: Session, recent_limit: int = 10):
        from app.schemas import (
            CryptoReport,
            CryptoRunSummary,
            CryptoSignalOut,
            CryptoTokenOut,
        )

        totals = {
            "tokens": session.execute(
                select(func.count()).select_from(CryptoToken)
            ).scalar() or 0,
            "pairs": session.execute(
                select(func.count()).select_from(CryptoPair)
            ).scalar() or 0,
            "ticks": session.execute(
                select(func.count()).select_from(CryptoPriceTick)
            ).scalar() or 0,
            "discovery_events": session.execute(
                select(func.count()).select_from(CryptoTokenDiscoveryEvent)
            ).scalar() or 0,
            "risk_assessments": session.execute(
                select(func.count()).select_from(CryptoTokenRiskAssessment)
            ).scalar() or 0,
        }
        signals_by_type = dict(
            session.execute(
                select(CryptoOpportunitySignal.signal_type, func.count()).group_by(
                    CryptoOpportunitySignal.signal_type
                )
            ).all()
        )
        signals_by_status = dict(
            session.execute(
                select(CryptoOpportunitySignal.signal_status, func.count()).group_by(
                    CryptoOpportunitySignal.signal_status
                )
            ).all()
        )
        risk_by_level = dict(
            session.execute(
                select(CryptoTokenRiskAssessment.risk_level, func.count()).group_by(
                    CryptoTokenRiskAssessment.risk_level
                )
            ).all()
        )
        recent_signals = session.execute(
            select(CryptoOpportunitySignal)
            .order_by(CryptoOpportunitySignal.id.desc())
            .limit(recent_limit)
        ).scalars().all()
        recent_tokens = session.execute(
            select(CryptoToken).order_by(CryptoToken.last_seen_at.desc(), CryptoToken.id.desc())
            .limit(recent_limit)
        ).scalars().all()
        recent_runs = session.execute(
            select(CryptoWatcherRun).order_by(CryptoWatcherRun.id.desc()).limit(recent_limit)
        ).scalars().all()
        provider_errors = [
            CryptoRunSummary.model_validate(run)
            for run in recent_runs
            if run.error_type is not None
        ]
        latest_run = (
            CryptoRunSummary.model_validate(recent_runs[0]) if recent_runs else None
        )

        return CryptoReport(
            totals=totals,
            signals_by_type=signals_by_type,
            signals_by_status=signals_by_status,
            risk_by_level={level or "unknown": n for level, n in risk_by_level.items()},
            recent_signals=[CryptoSignalOut.model_validate(s) for s in recent_signals],
            recent_tokens=[CryptoTokenOut.model_validate(t) for t in recent_tokens],
            latest_run=latest_run,
            provider_errors=provider_errors,
        )
