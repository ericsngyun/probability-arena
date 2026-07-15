"""Frontier evaluation harness (EVAL-001): measures the whole read-only
market desk from persisted data — signal quality, forecast quality,
edge-precheck quality, gap follow-through, microstructure validity, crypto
risk quality, latency, and a safety audit — and produces a conservative
readiness scorecard.

Hard boundary: this module EVALUATES; it never acts. No dollar EV, no paper
trading, no trade recommendations, no sizing, no orders, no wallets, no
swaps, no execution. Gap follow-through is market-movement analysis (did the
midpoint later move toward the forecast?) — it is NOT PnL and simulates no
fills or positions. Readiness labels gate further MEASUREMENT milestones
only and never authorize live capital.
"""

import ast
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenRiskAssessment,
    EdgePrecheckSnapshot,
    FrontierEvalRun,
    MarketForecastRecord,
    MarketOpsRun,
    MarketPriceTick,
    OpportunitySignal,
)

logger = logging.getLogger(__name__)

READY_NOT = "not_ready"
READY_OBSERVE = "observe_more"
READY_MANUAL = "ready_for_manual_edge_measurement"
READY_CYCLE_AUTOMATION = "ready_for_cycle_scoped_edge_automation"
READY_PAPER_DESIGN = "ready_for_paper_design"
# Deliberately absent: any live/autonomous-trading label. They do not exist.

FOLLOW_THROUGH_HORIZONS_MINUTES = (5, 15, 30, 60)
MIN_WATCHLIST_SAMPLE = 10  # below this: observe_more
MIN_FOLLOW_THROUGH_SAMPLES = 20  # paper-design gate
MIN_FOLLOW_THROUGH_TOWARD_RATE = 0.55
MARKETOPS_P90_MAX_SECONDS = 60.0

VALID_EDGE_STATUSES = ("watchlist", "paper_candidate_later", "no_gap")

# Identifier fragments that must never appear as code identifiers in app/
BANNED_IDENTIFIER_FRAGMENTS = (
    "wallet",
    "private_key",
    "keypair",
    "swap",
    "jupiter",
    "sign_transaction",
    "send_transaction",
    "place_order",
    "submit_order",
    "create_order",
    "expected_value",
    "kelly",
    "position_siz",
    "paper_trad",
    "trade_recommend",
    "recommended_side",
    "execute_trade",
    "portfolio",
)
# Known-legitimate identifier fragments per file (documented): the Kalshi
# WebSocket auth helper signs a subscription challenge with the user's
# OPTIONAL Kalshi API key — pre-existing, read-only, unrelated to trading.
SAFETY_ALLOWLIST_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "app/services/ws_snapshots.py": ("private_key",),
    "app/config.py": ("private_key",),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 4)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _domain_for(ticker: str) -> str:
    from app.services.research import DOMAIN_GENERAL, DOMAIN_RULES

    upper = ticker.upper()
    for domain, markers, _keywords in DOMAIN_RULES:
        if any(upper.startswith(marker) for marker in markers):
            return domain
    return DOMAIN_GENERAL


@dataclass
class EvalWindow:
    start: datetime
    end: datetime
    hours: int


class FrontierEvalService:
    """Read-only evaluation over persisted rows. Never calls external APIs,
    never mutates market state; --save-run persists only its own audit row."""

    def __init__(self, settings: Settings | None = None):
        if settings is not None:
            self.settings = settings
        else:
            try:
                self.settings = get_settings()
            except Exception as exc:  # reporting must degrade without config
                logger.warning(
                    "frontier eval runtime settings unavailable: %s",
                    type(exc).__name__,
                )
                self.settings = None

    # --- sections -----------------------------------------------------------

    def signal_quality(self, session: Session, window: EvalWindow, domains) -> dict:
        signals = session.execute(
            select(OpportunitySignal).where(
                OpportunitySignal.created_at >= window.start,
                OpportunitySignal.created_at <= window.end,
            )
        ).scalars().all()
        if domains:
            signals = [s for s in signals if _domain_for(s.market_ticker) in domains]
        seen = len(signals)
        promoted = [s for s in signals if s.promoted_at is not None]
        processed = [s for s in signals if s.signal_status == "forecast_refreshed"]

        forecasts = session.execute(
            select(MarketForecastRecord).where(
                MarketForecastRecord.created_at >= window.start,
                MarketForecastRecord.created_at <= window.end,
            )
        ).scalars().all()
        if domains:
            forecasts = [f for f in forecasts if _domain_for(f.market_ticker) in domains]
        source_backed = [f for f in forecasts if f.evidence_depth == "source_backed"]

        snapshots = self._edge_snapshots(session, window, domains)
        valid = [s for s in snapshots if s.status in VALID_EDGE_STATUSES]
        watchlist = [s for s in snapshots if s.status == "watchlist"]
        candidates = [s for s in snapshots if s.status == "paper_candidate_later"]

        by_type: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for signal in signals:
            by_type[signal.signal_type] = by_type.get(signal.signal_type, 0) + 1
            domain = _domain_for(signal.market_ticker)
            by_domain[domain] = by_domain.get(domain, 0) + 1

        return {
            "signals_seen": seen,
            "promoted": len(promoted),
            "promoted_rate": _rate(len(promoted), seen),
            "processed": len(processed),
            "processed_rate": _rate(len(processed), max(len(promoted), 1)),
            "forecasts": len(forecasts),
            "source_backed_forecasts": len(source_backed),
            "source_backed_rate": _rate(len(source_backed), len(forecasts)),
            "edge_snapshots": len(snapshots),
            "valid_edge_rate": _rate(len(valid), len(snapshots)),
            "watchlist_rate": _rate(len(watchlist), len(snapshots)),
            "candidate_label_rate": _rate(len(candidates), len(snapshots)),
            "by_signal_type": by_type,
            "by_domain": by_domain,
        }

    def forecast_quality(self, session: Session, window: EvalWindow, domains) -> dict:
        from app.services.champion_challenger import ChampionChallengerService

        forecasts = session.execute(
            select(MarketForecastRecord).where(
                MarketForecastRecord.created_at >= window.start,
                MarketForecastRecord.created_at <= window.end,
            )
        ).scalars().all()
        if domains:
            forecasts = [f for f in forecasts if _domain_for(f.market_ticker) in domains]

        by_forecaster: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        by_market_type: dict[str, int] = {}
        for forecast in forecasts:
            by_forecaster[forecast.forecaster_name] = (
                by_forecaster.get(forecast.forecaster_name, 0) + 1
            )
            bucket = f"{int(forecast.confidence * 10) / 10:.1f}"
            by_confidence[bucket] = by_confidence.get(bucket, 0) + 1
            for tag in forecast.calibration_tags or []:
                if str(tag).startswith("market_type_"):
                    key = str(tag).replace("market_type_", "")
                    by_market_type[key] = by_market_type.get(key, 0) + 1

        comparisons: dict[str, dict] = {}
        pairs = {
            "sports_baseball": "baseball_evidence_v1",
            "sports_soccer": "soccer_evidence_v1",
        }
        service = ChampionChallengerService()
        for domain, challenger in pairs.items():
            if domains and domain not in domains:
                continue
            try:
                summary = service.compare(session, domain=domain, challenger=challenger)
                comparisons[domain] = {
                    "challenger": challenger,
                    "paired_n": summary.paired.pair_count if summary.paired else 0,
                    "paired_delta_brier": (
                        summary.paired.mean_delta_brier if summary.paired else None
                    ),
                    "paired_delta_log_loss": (
                        summary.paired.mean_delta_log_loss if summary.paired else None
                    ),
                    "paired_sample_label": (
                        summary.paired.sample_label if summary.paired else "insufficient_sample"
                    ),
                    "baseline_scored_n": summary.baseline.scored.count_scored,
                    "challenger_scored_n": summary.challenger.scored.count_scored,
                    "baseline_brier": summary.baseline.scored.mean_brier,
                    "challenger_brier": summary.challenger.scored.mean_brier,
                    "baseline_abs_error": summary.baseline.scored.mean_absolute_error,
                    "challenger_abs_error": summary.challenger.scored.mean_absolute_error,
                }
            except Exception:  # comparison must never sink the report
                logger.exception("champion/challenger comparison failed for %s", domain)
                comparisons[domain] = {"error": "comparison_failed"}

        return {
            "forecasts_in_window": len(forecasts),
            "by_forecaster": by_forecaster,
            "by_confidence_bucket": dict(sorted(by_confidence.items())),
            "by_market_type": by_market_type,
            "champion_challenger": comparisons,
        }

    def _edge_snapshots(self, session: Session, window: EvalWindow, domains):
        snapshots = session.execute(
            select(EdgePrecheckSnapshot).where(
                EdgePrecheckSnapshot.created_at >= window.start,
                EdgePrecheckSnapshot.created_at <= window.end,
            )
        ).scalars().all()
        if domains:
            snapshots = [
                s for s in snapshots if _domain_for(s.market_ticker) in domains
            ]
        return snapshots

    def edge_quality(self, session: Session, window: EvalWindow, domains) -> dict:
        snapshots = self._edge_snapshots(session, window, domains)
        by_status: dict[str, int] = {}
        reasons: dict[str, int] = {}
        persistence: dict[str, int] = {}
        gaps: list[float] = []
        positive = negative = 0
        for row in snapshots:
            by_status[row.status] = by_status.get(row.status, 0) + 1
            for reason in row.invalidation_reasons or []:
                reasons[reason] = reasons.get(reason, 0) + 1
            key = str(row.persistence_count) if row.persistence_count < 3 else "3+"
            persistence[key] = persistence.get(key, 0) + 1
            if row.probability_gap is not None:
                gaps.append(row.probability_gap)
                if row.probability_gap >= 0:
                    positive += 1
                else:
                    negative += 1
        valid = sum(by_status.get(status, 0) for status in VALID_EDGE_STATUSES)
        invalid_rows = [s for s in snapshots if s.status not in VALID_EDGE_STATUSES]
        explainable = sum(1 for s in invalid_rows if s.invalidation_reasons)
        return {
            "total_snapshots": len(snapshots),
            "by_status": by_status,
            "invalidation_reasons": dict(sorted(reasons.items(), key=lambda i: -i[1])),
            "watchlist": by_status.get("watchlist", 0),
            "paper_candidate_later": by_status.get("paper_candidate_later", 0),
            "mean_gap": _mean(gaps),
            "mean_abs_gap": _mean([abs(g) for g in gaps]),
            "persistence_distribution": persistence,
            "gap_direction": {"positive": positive, "negative": negative},
            "valid_measurement_rate": _rate(valid, len(snapshots)),
            "invalid_explainable_rate": _rate(explainable, len(invalid_rows))
            if invalid_rows
            else 1.0,
        }

    def gap_follow_through(self, session: Session, window: EvalWindow, domains) -> dict:
        """Did the market midpoint later move toward the forecast? Market
        MOVEMENT analysis only — no fills, no positions, no PnL."""
        rows = [
            s
            for s in self._edge_snapshots(session, window, domains)
            if s.status in ("watchlist", "paper_candidate_later")
            and s.probability_gap is not None
            and s.market_midpoint is not None
        ]
        horizons: dict[str, dict] = {}
        for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
            samples = []
            for row in rows:
                created = _aware(row.created_at)
                deadline = created + timedelta(minutes=minutes)
                later = session.execute(
                    select(MarketPriceTick)
                    .where(
                        MarketPriceTick.market_ticker == row.market_ticker,
                        MarketPriceTick.observed_at > created,
                        MarketPriceTick.observed_at <= deadline,
                        MarketPriceTick.midpoint.is_not(None),
                    )
                    .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
                ).scalars().first()
                if later is None:
                    continue
                delta = later.midpoint - row.market_midpoint
                closure = delta / row.probability_gap if row.probability_gap else 0.0
                samples.append(
                    {
                        "midpoint_delta": round(delta, 4),
                        "gap_closure": round(delta, 4),
                        "gap_closure_pct": round(closure, 4),
                        "moved_toward_forecast": bool(
                            closure > 0 and abs(delta) > 1e-9
                        ),
                    }
                )
            toward = sum(1 for s in samples if s["moved_toward_forecast"])
            horizons[f"{minutes}m"] = {
                "samples": len(samples),
                "moved_toward_forecast": toward,
                "moved_toward_rate": _rate(toward, len(samples)),
                "mean_midpoint_delta": _mean([s["midpoint_delta"] for s in samples]),
                "mean_gap_closure_pct": _mean([s["gap_closure_pct"] for s in samples]),
            }
        return {
            "note": "Market-movement analysis only — not PnL, no fills, no positions.",
            "watchlist_rows_analyzed": len(rows),
            "horizons": horizons,
        }

    def microstructure_quality(self, session: Session, window: EvalWindow, domains) -> dict:
        ticks = session.execute(
            select(MarketPriceTick).where(
                MarketPriceTick.observed_at >= window.start,
                MarketPriceTick.observed_at <= window.end,
            )
        ).scalars().all()
        if domains:
            ticks = [t for t in ticks if _domain_for(t.market_ticker) in domains]
        two_sided = [t for t in ticks if t.midpoint is not None]
        spreads = [float(t.spread) for t in ticks if t.spread is not None]
        liquidity = [float(t.liquidity_proxy) for t in ticks if t.liquidity_proxy is not None]

        by_domain: dict[str, dict] = {}
        for tick in ticks:
            domain = _domain_for(tick.market_ticker)
            bucket = by_domain.setdefault(domain, {"ticks": 0, "two_sided": 0})
            bucket["ticks"] += 1
            if tick.midpoint is not None:
                bucket["two_sided"] += 1
        for bucket in by_domain.values():
            bucket["two_sided_rate"] = _rate(bucket["two_sided"], bucket["ticks"])

        edge = self.edge_quality(session, window, domains)
        return {
            "ticks": len(ticks),
            "two_sided_rate": _rate(len(two_sided), len(ticks)),
            "midpoint_availability_rate": _rate(len(two_sided), len(ticks)),
            "spread_cents_p50": _percentile(spreads, 50),
            "spread_cents_p90": _percentile(spreads, 90),
            "liquidity_cents_p50": _percentile(liquidity, 50),
            "liquidity_cents_p90": _percentile(liquidity, 90),
            "invalid_wide_spread": edge["invalidation_reasons"].get(
                "invalid_wide_spread", 0
            ),
            "invalid_low_liquidity": edge["invalidation_reasons"].get(
                "invalid_low_liquidity", 0
            ),
            "by_domain": by_domain,
        }

    def crypto_quality(self, session: Session, window: EvalWindow) -> dict:
        tokens = session.execute(
            select(func.count()).select_from(CryptoToken).where(
                CryptoToken.last_seen_at >= window.start
            )
        ).scalar() or 0
        pairs = session.execute(
            select(func.count()).select_from(CryptoPair).where(
                CryptoPair.last_seen_at >= window.start
            )
        ).scalar() or 0
        signals = session.execute(
            select(CryptoOpportunitySignal).where(
                CryptoOpportunitySignal.created_at >= window.start,
                CryptoOpportunitySignal.created_at <= window.end,
            )
        ).scalars().all()
        by_type: dict[str, int] = {}
        for signal in signals:
            by_type[signal.signal_type] = by_type.get(signal.signal_type, 0) + 1

        assessments = session.execute(
            select(CryptoTokenRiskAssessment).where(
                CryptoTokenRiskAssessment.created_at >= window.start,
                CryptoTokenRiskAssessment.created_at <= window.end,
            ).order_by(CryptoTokenRiskAssessment.id.desc())
        ).scalars().all()
        latest_by_token: dict[str, CryptoTokenRiskAssessment] = {}
        provider_errors = 0
        for row in assessments:
            latest_by_token.setdefault(row.token_address, row)
            if (row.raw_payload or {}).get("provider_errors"):
                provider_errors += 1
        by_level: dict[str, int] = {}
        for row in latest_by_token.values():
            level = row.composite_risk_level or row.risk_level or "unknown"
            by_level[level] = by_level.get(level, 0) + 1

        # Post-signal movement (movement summary only — no trade simulation)
        movement_samples = []
        for signal in signals:
            if signal.signal_type not in ("rug_risk", "liquidity_removed"):
                continue
            if not signal.pair_address:
                continue
            created = _aware(signal.created_at)
            base = session.execute(
                select(CryptoPriceTick)
                .where(
                    CryptoPriceTick.pair_address == signal.pair_address,
                    CryptoPriceTick.observed_at <= created,
                )
                .order_by(CryptoPriceTick.observed_at.desc())
            ).scalars().first()
            later = session.execute(
                select(CryptoPriceTick)
                .where(
                    CryptoPriceTick.pair_address == signal.pair_address,
                    CryptoPriceTick.observed_at > created,
                )
                .order_by(CryptoPriceTick.observed_at.desc())
            ).scalars().first()
            if base is None or later is None:
                continue
            if base.liquidity_usd and later.liquidity_usd is not None:
                movement_samples.append(
                    round((later.liquidity_usd - base.liquidity_usd) / base.liquidity_usd, 4)
                )
        return {
            "tokens_seen": tokens,
            "pairs_seen": pairs,
            "signals_by_type": by_type,
            "risk_by_level": by_level,
            "severe_or_high": by_level.get("severe", 0) + by_level.get("high", 0),
            "assessments": len(assessments),
            "provider_error_rate": _rate(provider_errors, len(assessments)),
            "rug_risk_signals": by_type.get("rug_risk", 0),
            "liquidity_removed_signals": by_type.get("liquidity_removed", 0),
            "post_risk_signal_liquidity_change_pct_mean": _mean(movement_samples),
            "post_risk_signal_samples": len(movement_samples),
        }

    def latency_quality(self, session: Session, window: EvalWindow) -> dict:
        runs = session.execute(
            select(MarketOpsRun).where(
                MarketOpsRun.created_at >= window.start,
                MarketOpsRun.created_at <= window.end,
                MarketOpsRun.duration_ms.is_not(None),
                MarketOpsRun.status.in_(("ok", "partial")),
            )
        ).scalars().all()
        durations = [run.duration_ms / 1000 for run in runs]

        signals = session.execute(
            select(OpportunitySignal).where(
                OpportunitySignal.created_at >= window.start,
                OpportunitySignal.created_at <= window.end,
                OpportunitySignal.promoted_at.is_not(None),
            )
        ).scalars().all()
        promotion_ages = []
        processing_lags = []
        for signal in signals:
            observed = _aware(signal.observed_at)
            promoted = _aware(signal.promoted_at)
            if observed and promoted:
                promotion_ages.append((promoted - observed).total_seconds())
            processed = _aware(signal.processed_at)
            if promoted and processed:
                processing_lags.append((processed - promoted).total_seconds())

        forecast_lags = []
        for signal in signals:
            if signal.refreshed_forecast_id is None:
                continue
            forecast = session.get(MarketForecastRecord, signal.refreshed_forecast_id)
            if forecast is None:
                continue
            observed = _aware(signal.observed_at)
            created = _aware(forecast.created_at)
            if observed and created:
                forecast_lags.append((created - observed).total_seconds())

        edge_lags = []
        for snapshot in self._edge_snapshots(session, window, None):
            forecast = session.get(MarketForecastRecord, snapshot.forecast_id)
            if forecast is None:
                continue
            created = _aware(snapshot.created_at)
            forecast_created = _aware(forecast.created_at)
            if created and forecast_created:
                edge_lags.append((created - forecast_created).total_seconds())

        latest_tick = session.execute(
            select(MarketPriceTick).order_by(
                MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc()
            )
        ).scalars().first()
        tick_age = (
            round((window.end - _aware(latest_tick.observed_at)).total_seconds(), 1)
            if latest_tick is not None
            else None
        )

        return {
            "marketops_runs": len(runs),
            "marketops_duration_s_p50": _percentile(durations, 50),
            "marketops_duration_s_p90": _percentile(durations, 90),
            "marketops_duration_s_p99": _percentile(durations, 99),
            "latest_watcher_tick_age_s": tick_age,
            "signal_age_at_promotion_s_p50": _percentile(promotion_ages, 50),
            "promotion_to_processed_s_p50": _percentile(processing_lags, 50),
            "signal_to_forecast_s_p50": _percentile(forecast_lags, 50),
            "forecast_to_edge_precheck_s_p50": _percentile(edge_lags, 50),
        }

    def safety_audit(self) -> dict:
        """AST-based identifier scan of app/ — banned vocabulary must not
        appear as code identifiers (boundary docstrings are strings and pass
        untouched). Reuses the canonical grep vocabulary."""
        app_dir = Path(__file__).resolve().parents[1]
        violations: list[dict] = []
        files_scanned = 0
        for path in sorted(app_dir.rglob("*.py")):
            rel = f"app/{path.relative_to(app_dir)}"
            files_scanned += 1
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover
                violations.append({"file": rel, "identifier": "<unparseable>"})
                continue
            names: set[str] = set()
            for node in ast.walk(tree):
                for attr in ("name", "id", "attr", "arg", "module"):
                    value = getattr(node, attr, None)
                    if isinstance(value, str):
                        names.add(value)
            allowed = SAFETY_ALLOWLIST_FRAGMENTS.get(rel, ())
            for name in names:
                lowered = name.lower()
                for fragment in BANNED_IDENTIFIER_FRAGMENTS:
                    if fragment in lowered and not any(
                        allow in lowered for allow in allowed
                    ):
                        violations.append({"file": rel, "identifier": name})
        return {
            "files_scanned": files_scanned,
            "banned_identifier_fragments": list(BANNED_IDENTIFIER_FRAGMENTS),
            "violations": violations,
            "safety_ok": not violations,
            "note": (
                "Identifier-level scan: boundary docstrings/comments are allowed; "
                "implementation surfaces are not. Canonical text grep lives in "
                "AGENTS.md / docs/TESTING_POLICY.md."
            ),
        }

    # --- scorecard ------------------------------------------------------------

    def readiness(self, edge: dict, follow: dict, forecast: dict, latency: dict, safety: dict | None) -> dict:
        reasons: list[str] = []
        warnings: list[str] = []

        safety_ok = safety is None or safety.get("safety_ok", False)
        if safety is not None and not safety_ok:
            reasons.append("safety scan found potential implementation surfaces")

        watchlist = edge.get("watchlist", 0)
        candidates = edge.get("paper_candidate_later", 0)
        valid_rate = edge.get("valid_measurement_rate")
        explainable = edge.get("invalid_explainable_rate")
        p90 = latency.get("marketops_duration_s_p90")

        if not safety_ok:
            label = READY_NOT
        elif watchlist == 0 and candidates == 0:
            label = READY_NOT
            reasons.append(
                "no valid watchlist rows in the window — measurement machinery may "
                "be working (honest invalidation), but there is nothing to act on "
                "evaluatively yet"
            )
        elif watchlist < MIN_WATCHLIST_SAMPLE:
            label = READY_OBSERVE
            reasons.append(
                f"watchlist sample too thin ({watchlist} < {MIN_WATCHLIST_SAMPLE})"
            )
        else:
            automation_ok = (
                (valid_rate or 0) > 0
                and explainable == 1.0
                and p90 is not None
                and p90 < MARKETOPS_P90_MAX_SECONDS
            )
            if automation_ok:
                label = READY_CYCLE_AUTOMATION
                reasons.append(
                    "valid + watchlist rows exist, invalid rows fully explainable, "
                    f"MarketOps p90 {p90}s < {MARKETOPS_P90_MAX_SECONDS}s, safety clean"
                )
                # paper-design escalation (still measurement-gated)
                horizons = follow.get("horizons", {})
                strong = [
                    h
                    for h in horizons.values()
                    if (h.get("samples") or 0) >= MIN_FOLLOW_THROUGH_SAMPLES
                    and (h.get("moved_toward_rate") or 0) >= MIN_FOLLOW_THROUGH_TOWARD_RATE
                ]
                cc = forecast.get("champion_challenger", {})
                cc_ok = any(
                    entry.get("paired_sample_label") in ("early_signal", "useful_sample", "stronger_sample")
                    for entry in cc.values()
                    if isinstance(entry, dict)
                )
                if candidates > 0 and strong and cc_ok:
                    label = READY_PAPER_DESIGN
                    reasons.append(
                        "persistent candidate labels exist, gap follow-through moves "
                        "toward forecasts at sufficient sample, champion/challenger "
                        "at early_signal or better"
                    )
            else:
                label = READY_MANUAL
                if explainable != 1.0:
                    reasons.append("some invalid rows lack recorded reasons")
                if p90 is None or p90 >= MARKETOPS_P90_MAX_SECONDS:
                    reasons.append(f"MarketOps p90 {p90}s not under {MARKETOPS_P90_MAX_SECONDS}s")

        return {
            "label": label,
            "reasons": reasons,
            "warnings": warnings,
            "note": (
                "Readiness labels gate further MEASUREMENT milestones only; no label "
                "authorizes live capital, orders, or autonomous behavior — those "
                "capabilities do not exist (docs/SAFETY_BOUNDARIES.md)."
            ),
        }

    # --- assembly ---------------------------------------------------------------

    def build(
        self,
        session: Session,
        hours: int = 24,
        domains: list[str] | None = None,
        include_crypto: bool = True,
        include_safety: bool = True,
        now: datetime | None = None,
    ):
        from app.schemas import FrontierEvalReport

        end = now or _now()
        window = EvalWindow(start=end - timedelta(hours=hours), end=end, hours=hours)
        domains = list(domains) if domains else None

        signal = self.signal_quality(session, window, domains)
        forecast = self.forecast_quality(session, window, domains)
        edge = self.edge_quality(session, window, domains)
        follow = self.gap_follow_through(session, window, domains)
        micro = self.microstructure_quality(session, window, domains)
        crypto = self.crypto_quality(session, window) if include_crypto else None
        latency = self.latency_quality(session, window)
        safety = self.safety_audit() if include_safety else None
        scorecard = self.readiness(edge, follow, forecast, latency, safety)
        edge_runtime = self.edge_precheck_runtime()

        executive = (
            f"{signal['signals_seen']} signals seen / {signal['promoted']} promoted / "
            f"{signal['processed']} processed; {edge['total_snapshots']} gap measurements "
            f"({edge['watchlist']} watchlist, {edge['paper_candidate_later']} candidate "
            f"labels); readiness: {scorecard['label']}. Evaluation only — no EV, no "
            "trades, no positions."
        )

        return FrontierEvalReport(
            generated_at=end,
            window_hours=hours,
            domains=domains,
            executive_summary=executive,
            readiness=scorecard,
            signal_quality=signal,
            forecast_quality=forecast,
            edge_precheck_quality=edge,
            gap_follow_through=follow,
            microstructure_quality=micro,
            crypto_risk_quality=crypto,
            latency_quality=latency,
            safety_audit=safety,
            edge_precheck_runtime=edge_runtime,
            recommended_next_action=self._recommend(scorecard, edge, edge_runtime),
        )

    def edge_precheck_runtime(self) -> dict:
        """Report only the edge flags needed to interpret recommendation text."""
        if self.settings is None:
            return {
                "enable_edge_precheck": None,
                "marketops_include_edge_precheck": None,
                "effective_marketops_stage_enabled": None,
            }
        master = bool(self.settings.enable_edge_precheck)
        include = bool(self.settings.marketops_include_edge_precheck)
        return {
            "enable_edge_precheck": master,
            "marketops_include_edge_precheck": include,
            "effective_marketops_stage_enabled": master and include,
        }

    @staticmethod
    def _recommend(scorecard: dict, edge: dict, edge_runtime: dict | None = None) -> str:
        label = scorecard["label"]
        if label == READY_NOT:
            return (
                "Run targeted edge-precheck sessions during prime live windows "
                "(watchlist rows are the missing evidence); keep all automation off"
            )
        if label == READY_OBSERVE:
            return (
                "Keep measuring manually across more live windows to grow the "
                "watchlist sample; no flag changes yet"
            )
        if label == READY_MANUAL:
            return "Address the latency/explainability reasons before considering automation"
        if label == READY_CYCLE_AUTOMATION:
            if edge_runtime is None or any(
                edge_runtime.get(key) is None
                for key in (
                    "enable_edge_precheck",
                    "marketops_include_edge_precheck",
                )
            ):
                return (
                    "Evidence supports cycle-scoped edge measurement; verify runtime "
                    "flag state before changing configuration."
                )
            master = edge_runtime["enable_edge_precheck"]
            include = edge_runtime["marketops_include_edge_precheck"]
            if not master and not include:
                return (
                    "Cycle-scoped edge measurement is operationally ready but disabled. "
                    "Enabling it requires both ENABLE_EDGE_PRECHECK and "
                    "MARKETOPS_INCLUDE_EDGE_PRECHECK."
                )
            if master and not include:
                return (
                    "Edge measurement is permitted, but MarketOps inclusion is disabled. "
                    "Evidence supports enabling MARKETOPS_INCLUDE_EDGE_PRECHECK as a "
                    "measurement-only step."
                )
            if not master and include:
                return (
                    "MarketOps inclusion is requested, but ENABLE_EDGE_PRECHECK is "
                    "disabled, so the stage skips. Resolve the inconsistent flag state "
                    "before expecting rows."
                )
            return (
                "Cycle-scoped edge measurement is already enabled. Continue "
                "accumulating measurements; no configuration change is needed."
            )
        return (
            "Evidence supports drafting the MVP-005B paper-simulator DESIGN "
            "(design + safety review only; no implementation without acceptance)"
        )

    def persist_run(self, session: Session, report, started_at: datetime, window_hours: int):
        finished = _now()
        row = FrontierEvalRun(
            status="ok",
            started_at=started_at,
            finished_at=finished,
            duration_ms=max(0, int((finished - started_at).total_seconds() * 1000)),
            window_start=report.generated_at - timedelta(hours=window_hours),
            window_end=report.generated_at,
            summary=report.model_dump(mode="json"),
            warnings=report.readiness.get("warnings") or [],
            created_at=finished,
        )
        session.add(row)
        session.commit()
        return row
