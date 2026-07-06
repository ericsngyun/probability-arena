"""Scheduled meme/news discovery lane (MEME-NEWS-002): a bounded, always-on
READ-ONLY wrapper around the MEME-NEWS-001 attention scout, plus a windowed
report and derived notable-event "alerts".

This turns the manual `meme-scan-once` into a scheduled runner that a systemd
timer can fire every few minutes. It records the same audit spine (status,
timings, tokens scored, catalysts, errors), bounds each pass, and degrades
gracefully on provider errors — it is a separate process from MarketOps and
cannot affect it.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): read-only scheduled
discovery ONLY. `attention_score` is an interest/velocity signal for human
review — never EV, a recommendation, or an instruction. Alerts are local,
derived, informational report rows — no push notifications, no trade triggers,
no sizing. No wallets/keys, swaps, signing, orders, or execution anywhere.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    MemeAttentionSnapshot,
    MemeCatalystEvent,
    MemeScoutRun,
)
from app.services.meme_scout import MemeScoutConfig, MemeScoutService

logger = logging.getLogger(__name__)

HIGH_RISK_LEVELS = ("severe", "high")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 4)


@dataclass
class MemeNewsConfig:
    enabled: bool = False
    max_profiles_per_run: int = 30
    max_boosts_per_run: int = 30
    attention_alert_threshold: float = 0.6
    attention_jump_threshold: float = 0.15
    severe_risk_alert: bool = True

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "MemeNewsConfig":
        s = settings or get_settings()
        return cls(
            enabled=s.enable_meme_news_scout,
            max_profiles_per_run=s.meme_news_max_profiles_per_run,
            max_boosts_per_run=s.meme_news_max_boosts_per_run,
            attention_alert_threshold=s.meme_news_attention_alert_threshold,
            attention_jump_threshold=s.meme_news_attention_jump_threshold,
            severe_risk_alert=s.meme_news_severe_risk_alert,
        )


class MemeNewsScoutRunner:
    """One bounded read-only scan cycle. Wraps MemeScoutService; guarantees it
    never raises out of `run_cycle` (a scheduled lane must not crash-loop)."""

    def __init__(
        self,
        scout: MemeScoutService | None = None,
        config: MemeNewsConfig | None = None,
    ):
        self.config = config or MemeNewsConfig.from_settings()
        # bound the scout's per-pass token cap by the configured max
        cap = max(self.config.max_profiles_per_run, self.config.max_boosts_per_run)
        self.scout = scout or MemeScoutService(config=MemeScoutConfig(limit=cap))

    async def run_cycle(self, session: Session, limit: int | None = None) -> MemeScoutRun | None:
        """Run one bounded pass. Returns the MemeScoutRun (status ok|error), or
        None only if even the audit row could not be recorded. Never raises."""
        cap = limit if limit is not None else max(
            self.config.max_profiles_per_run, self.config.max_boosts_per_run
        )
        try:
            return await self.scout.scan_once(session, limit=cap)
        except Exception as exc:  # scan_once records + re-raises on unexpected DB errors
            logger.exception("meme-news scheduled cycle failed: %s", exc)
            try:
                return session.execute(
                    select(MemeScoutRun).order_by(MemeScoutRun.id.desc())
                ).scalars().first()
            except Exception:  # pragma: no cover - defensive
                return None


# --- windowed report --------------------------------------------------------


@dataclass
class MemeNewsReport:
    note: str
    window_hours: int
    last_run: dict | None
    runs_in_window: int
    error_runs_in_window: int
    new_tokens: int
    catalysts_in_window: int
    attention_p50: float | None
    attention_p90: float | None
    attention_max: float | None
    top_attention: list[dict] = field(default_factory=list)
    high_risk_tokens: list[dict] = field(default_factory=list)
    provider_confidence_avg: float | None = None
    missing_holder_coverage: int = 0
    row_counts: dict = field(default_factory=dict)


class MemeNewsReportService:
    def build(self, session: Session, hours: int = 24, top: int = 10) -> MemeNewsReport:
        now = _now()
        start = now - timedelta(hours=hours)

        runs = session.execute(
            select(MemeScoutRun).where(MemeScoutRun.started_at >= start)
        ).scalars().all()
        last = session.execute(
            select(MemeScoutRun).order_by(MemeScoutRun.id.desc())
        ).scalars().first()

        snaps = session.execute(
            select(MemeAttentionSnapshot).where(MemeAttentionSnapshot.observed_at >= start)
        ).scalars().all()

        scores = [s.attention_score for s in snaps if s.attention_score is not None]
        confidences = [s.provider_confidence for s in snaps if s.provider_confidence is not None]
        # newest snapshot per token in window, ranked by attention
        latest_by_token: dict[str, MemeAttentionSnapshot] = {}
        for s in snaps:
            cur = latest_by_token.get(s.token_address)
            if cur is None or s.id > cur.id:
                latest_by_token[s.token_address] = s
        ranked = sorted(latest_by_token.values(), key=lambda x: -(x.attention_score or 0))

        top_attention = [
            {
                "token": s.token_address[:16], "symbol": s.symbol,
                "attention_score": s.attention_score, "risk_level": s.risk_level,
                "boost_amount": s.boost_amount, "age_seconds": s.token_age_seconds,
                "provider_confidence": s.provider_confidence,
            }
            for s in ranked[:top]
        ]
        high_risk = [
            {"token": s.token_address[:16], "symbol": s.symbol,
             "risk_level": s.risk_level, "attention_score": s.attention_score}
            for s in ranked if (s.risk_level or "") in HIGH_RISK_LEVELS
        ]
        # missing holder/sniper/insider coverage = snapshots with no provider data
        missing_coverage = sum(1 for s in snaps if (s.provider_confidence or 0) <= 0.25)

        catalysts_in_window = session.execute(
            select(func.count()).select_from(MemeCatalystEvent).where(
                MemeCatalystEvent.observed_at >= start
            )
        ).scalar() or 0

        return MemeNewsReport(
            note=(
                "Read-only scheduled discovery. attention_score is an interest signal "
                "for human review — not EV, not a recommendation, not an instruction. "
                "No sizing, orders, wallets, swaps, signing, or execution."
            ),
            window_hours=hours,
            last_run=(
                {
                    "id": last.id, "status": last.status,
                    "started_at": last.started_at.isoformat() if last.started_at else None,
                    "duration_ms": last.duration_ms,
                    "profiles_seen": last.profiles_seen, "boosts_seen": last.boosts_seen,
                    "tokens_scored": last.tokens_scored, "catalysts_created": last.catalysts_created,
                    "error_type": last.error_type,
                } if last else None
            ),
            runs_in_window=len(runs),
            error_runs_in_window=sum(1 for r in runs if r.status == "error"),
            new_tokens=len(latest_by_token),
            catalysts_in_window=catalysts_in_window,
            attention_p50=_pct(scores, 50),
            attention_p90=_pct(scores, 90),
            attention_max=round(max(scores), 4) if scores else None,
            top_attention=top_attention,
            high_risk_tokens=high_risk[:top],
            provider_confidence_avg=(
                round(sum(confidences) / len(confidences), 4) if confidences else None
            ),
            missing_holder_coverage=missing_coverage,
            row_counts={
                "meme_scout_runs": session.execute(
                    select(func.count()).select_from(MemeScoutRun)
                ).scalar() or 0,
                "meme_attention_snapshots": session.execute(
                    select(func.count()).select_from(MemeAttentionSnapshot)
                ).scalar() or 0,
                "meme_catalyst_events": session.execute(
                    select(func.count()).select_from(MemeCatalystEvent)
                ).scalar() or 0,
            },
        )


# --- derived notable-event "alerts" (local, informational) ------------------


@dataclass
class MemeNewsAlert:
    alert_type: str
    severity: str  # info|warn
    token: str | None
    detail: str
    value: float | None = None


class MemeNewsAlertService:
    """Derives notable events from persisted rows — nothing is pushed, nothing
    is stored beyond what already exists, and no alert is a recommendation."""

    def evaluate(self, session: Session, hours: int = 6) -> list[MemeNewsAlert]:
        cfg = MemeNewsConfig.from_settings(get_settings())
        now = _now()
        start = now - timedelta(hours=hours)
        snaps = session.execute(
            select(MemeAttentionSnapshot).where(MemeAttentionSnapshot.observed_at >= start)
            .order_by(MemeAttentionSnapshot.id)
        ).scalars().all()

        alerts: list[MemeNewsAlert] = []

        # 1) new token with attention above threshold
        for s in snaps:
            if (s.attention_score or 0) >= cfg.attention_alert_threshold:
                alerts.append(MemeNewsAlert(
                    "high_attention", "info", s.token_address[:16],
                    f"attention {s.attention_score} >= {cfg.attention_alert_threshold} "
                    f"(risk={s.risk_level})", s.attention_score,
                ))

        # 2) attention jump: latest vs previous snapshot per token
        by_token: dict[str, list[MemeAttentionSnapshot]] = {}
        for s in snaps:
            by_token.setdefault(s.token_address, []).append(s)
        for token, rows in by_token.items():
            if len(rows) >= 2 and rows[-1].attention_score is not None and rows[-2].attention_score is not None:
                jump = round(rows[-1].attention_score - rows[-2].attention_score, 4)
                if jump >= cfg.attention_jump_threshold:
                    alerts.append(MemeNewsAlert(
                        "attention_jump", "info", token[:16],
                        f"attention +{jump} ({rows[-2].attention_score}->{rows[-1].attention_score})",
                        jump,
                    ))

        # 3) boost increase
        boost_inc = session.execute(
            select(MemeCatalystEvent).where(
                MemeCatalystEvent.catalyst_type == "boost_increase",
                MemeCatalystEvent.observed_at >= start,
            )
        ).scalars().all()
        for c in boost_inc:
            alerts.append(MemeNewsAlert(
                "boost_increase", "info", c.subject_ref[:16],
                f"boost velocity +{c.magnitude}/h", c.magnitude,
            ))

        # 4) severe/high risk token (flag/avoid verdict — never a trade direction)
        if cfg.severe_risk_alert:
            seen: set[str] = set()
            for s in snaps:
                if (s.risk_level or "") in HIGH_RISK_LEVELS and s.token_address not in seen:
                    seen.add(s.token_address)
                    alerts.append(MemeNewsAlert(
                        "severe_risk", "warn", s.token_address[:16],
                        f"risk={s.risk_level} — avoid/flag for review, not a trade direction",
                        s.risk_score,
                    ))

        # 5) provider degradation: high share of snapshots without provider data
        if snaps:
            missing = sum(1 for s in snaps if (s.provider_confidence or 0) <= 0.25)
            frac = missing / len(snaps)
            if frac >= 0.5:
                alerts.append(MemeNewsAlert(
                    "provider_degradation", "warn", None,
                    f"{missing}/{len(snaps)} recent snapshots missing provider risk data "
                    f"(holder/sniper/insider gap; frac={round(frac, 3)})", round(frac, 3),
                ))
        return alerts
