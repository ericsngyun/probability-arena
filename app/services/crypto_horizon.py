"""CRYPTO-HORIZON-OBS-001 — bounded read-only horizon-observation lane.

CRYPTO-COVERAGE-001 conclusively found the 6h/24h survival-maturation ceiling
is UPSTREAM tick coverage, not tape-recorder selection: the background scout
does not tick aged/quiet tokens near their long horizons (dominant cause
`outside_tolerance_only`), and early ticks often lack liquidity state. This
lane fixes that at the source for a SMALL, STABLE research cohort: on manual
invocation only, it fetches market/liquidity state via the existing read-only
DexScreener adapter near each 15m/1h/6h/24h mark and persists an ordinary
crypto_price_tick (so the tape's survival horizons can actually mature) plus
an audit observation row.

Manual-only by construction: there is NO timer, NO scheduled path, NO loop,
NO autonomy, and NO flag enabling it. Cohort membership is frozen at creation.
A horizon is observed at most once (unique per cohort+token+horizon). Misses
are recorded honestly (token_inactive / provider_no_pair / no_liquidity_state
/ request_failed) — never fabricated.

Provider note: observations use the DexScreener adapter (free, no key, no
SolanaTracker), so this lane has ZERO SolanaTracker budget impact; external
calls are bounded per pass and reported explicitly.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): market/liquidity
OBSERVATION only. Nothing here is EV, a side, a size, an order, a
recommendation, or a trade direction. No wallets, keys, swaps, signing,
orders, execution, or autonomy.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.dexscreener import DexScreenerAdapter
from app.config import Settings, get_settings
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoPriceTick,
    CryptoTokenBirthEvent,
)
from app.services.crypto_tape import HORIZON_TOLERANCE, HORIZONS, _aware, _now

logger = logging.getLogger(__name__)

HORIZON_NOTE = (
    "Bounded read-only horizon-observation lane: a fixed research cohort gets "
    "manual market/liquidity observations near the 15m/1h/6h/24h marks (via the "
    "existing DexScreener adapter) so tape survival horizons can mature. "
    "Manual only — no timer, no loop, no autonomy; zero SolanaTracker impact; "
    "misses recorded honestly, never fabricated. Observation only — never EV, a "
    "side, a size, an order, or a recommendation. No wallets, keys, swaps, "
    "signing, or execution."
)

# cohort caps
COHORT_DEFAULT_LIMIT = 25
COHORT_MAX = 100
# observe-pass caps
OBSERVE_DEFAULT_LIMIT = 25
OBSERVE_MAX_CALLS = 100          # hard cap on adapter calls per pass
# observe at the SAME tolerance the tape uses, so an observation lands inside
# the survival window and the horizon actually matures
OBS_TOLERANCE = HORIZON_TOLERANCE
# a token with no pair whose birth is older than this is treated as inactive
# (aged out) rather than a transient provider gap
INACTIVE_AGE_HOURS = 24

TICK_SOURCE = "crypto-horizon-obs"

# plan statuses
STATUS_NOT_DUE = "not_due"
STATUS_DUE_NOW = "due_now"
STATUS_ALREADY_OBSERVED = "already_observed"
STATUS_OVERDUE_UNOBSERVED = "overdue_unobserved"
STATUS_INACTIVE = "inactive"

# observation statuses / miss causes
OBS_OBSERVED = "observed"
OBS_TOKEN_INACTIVE = "token_inactive"
OBS_PROVIDER_NO_PAIR = "provider_no_pair"
OBS_NO_LIQUIDITY_STATE = "no_liquidity_state"
OBS_REQUEST_FAILED = "request_failed"
MISS_CAUSES = (
    OBS_TOKEN_INACTIVE, OBS_PROVIDER_NO_PAIR, OBS_NO_LIQUIDITY_STATE,
    OBS_REQUEST_FAILED,
)

# early-liquidity (15m/1h) field-completeness diagnostics
EARLY_PRICE_ONLY = "price_only_no_liquidity"
EARLY_PAIR_MISSING = "pair_missing"
EARLY_LIQ_FIELD_MISSING = "liquidity_field_missing"

# success gates (measurement only — enforced by nobody)
SUCCESS_GATES = {
    "15m": 0.80, "1h": 0.80, "6h": 0.70, "24h": 0.60,
    "liquidity_state": 0.80,
}

INACTIVE_STATUSES = frozenset({OBS_TOKEN_INACTIVE, OBS_PROVIDER_NO_PAIR})


def _minutes(label: str) -> int:
    return dict(HORIZONS)[label]


# --- pure planning --------------------------------------------------------------


@dataclass
class HorizonPlanEntry:
    token_address: str
    symbol: str | None
    horizon: str
    birth_at: datetime | None
    target_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    status: str
    target_distance_s: float | None
    member_id: int | None = None


def plan_observations(
    members: list, existing: dict, inactive_tokens: set, now: datetime,
) -> list[HorizonPlanEntry]:
    """Pure planner. `existing` maps (token, horizon) -> observation status;
    `inactive_tokens` is the set already found dead. Never does I/O."""
    entries: list[HorizonPlanEntry] = []
    for m in members:
        anchor = _aware(getattr(m, "first_evidence_at", None)) or _aware(
            getattr(m, "birth_observed_at", None)
        )
        token = m.token_address
        token_inactive = token in inactive_tokens
        for label, minutes in HORIZONS:
            target = (anchor + timedelta(minutes=minutes)) if anchor else None
            tol = timedelta(minutes=minutes * OBS_TOLERANCE)
            ws = (target - tol) if target else None
            we = (target + tol) if target else None
            prior = existing.get((token, label))
            if prior == OBS_OBSERVED:
                status = STATUS_ALREADY_OBSERVED
            elif token_inactive:
                status = STATUS_INACTIVE
            elif target is None:
                status = STATUS_NOT_DUE
            elif now < ws:
                status = STATUS_NOT_DUE
            elif now <= we:
                status = STATUS_DUE_NOW
            else:
                status = STATUS_OVERDUE_UNOBSERVED
            dist = (
                round(abs((now - target).total_seconds()), 1) if target else None
            )
            entries.append(HorizonPlanEntry(
                token_address=token, symbol=getattr(m, "symbol", None),
                horizon=label, birth_at=anchor, target_at=target,
                window_start=ws, window_end=we, status=status,
                target_distance_s=dist, member_id=getattr(m, "id", None),
            ))
    return entries


def due_fetch_order(plan: list[HorizonPlanEntry]) -> list[str]:
    """Tokens to fetch this pass, nearest-target-first (one fetch per token
    serves all its due horizons). due_now only — overdue windows are closed."""
    due = [e for e in plan if e.status == STATUS_DUE_NOW]
    due.sort(key=lambda e: (e.target_distance_s if e.target_distance_s is not None else 1e18))
    order: list[str] = []
    for e in due:
        if e.token_address not in order:
            order.append(e.token_address)
    return order


# --- cohort intake --------------------------------------------------------------


class CryptoHorizonService:
    def __init__(
        self,
        adapter: DexScreenerAdapter | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.chain = self.settings.crypto_chain
        self._adapter = adapter  # lazily created for real passes only

    @property
    def adapter(self) -> DexScreenerAdapter:
        if self._adapter is None:
            self._adapter = DexScreenerAdapter()
        return self._adapter

    def create_cohort(
        self, session: Session, limit: int = COHORT_DEFAULT_LIMIT,
        hours: int = 48, dry_run: bool = False,
    ) -> dict:
        """Freeze a fixed cohort of the most recently born tokens (recent-first,
        so their long horizons are still ahead and catchable). Read-only
        selection from persisted births; dry-run persists nothing."""
        limit = max(1, min(limit, COHORT_MAX))
        now = _now()
        cutoff = now - timedelta(hours=hours)
        births = list(session.execute(
            select(CryptoTokenBirthEvent)
            .where(
                CryptoTokenBirthEvent.chain == self.chain,
                CryptoTokenBirthEvent.observed_at >= cutoff,
            )
            .order_by(CryptoTokenBirthEvent.first_evidence_at.desc(),
                      CryptoTokenBirthEvent.id.desc())
            .limit(limit)
        ).scalars().all())

        preview = [
            {"token": b.token_address[:16], "symbol": b.symbol,
             "first_evidence_at": (
                 _aware(b.first_evidence_at).isoformat() if b.first_evidence_at else None
             )}
            for b in births[:10]
        ]
        summary = {
            "status": "dry_run" if dry_run else "ok",
            "note": HORIZON_NOTE,
            "external_calls": 0,
            "requested_limit": limit,
            "window_hours": hours,
            "members_selected": len(births),
            "preview": preview,
        }
        if dry_run or not births:
            if not births and not dry_run:
                summary["status"] = "no_births"
            return summary

        cohort = CryptoHorizonCohort(
            chain=self.chain, member_limit=limit, window_hours=hours,
            note="recent-first birth cohort for horizon observation",
            provenance={
                "source": "crypto_token_birth_events",
                "order": "first_evidence_at desc",
                "window_hours": hours,
                "selected_at": now.isoformat(),
            },
            created_at=now,
        )
        session.add(cohort)
        session.flush()
        for b in births:
            session.add(CryptoHorizonCohortMember(
                cohort_id=cohort.id, chain=self.chain, token_address=b.token_address,
                symbol=b.symbol, birth_event_id=b.id,
                birth_observed_at=_aware(b.observed_at),
                first_evidence_at=_aware(b.first_evidence_at), added_at=now,
            ))
        session.commit()
        summary["cohort_id"] = cohort.id
        return summary

    # --- shared loaders -----------------------------------------------------

    def _members(self, session: Session, cohort_id: int) -> list[CryptoHorizonCohortMember]:
        return list(session.execute(
            select(CryptoHorizonCohortMember)
            .where(CryptoHorizonCohortMember.cohort_id == cohort_id)
            .order_by(CryptoHorizonCohortMember.id)
        ).scalars().all())

    def _observations(self, session: Session, cohort_id: int) -> list[CryptoHorizonObservation]:
        return list(session.execute(
            select(CryptoHorizonObservation)
            .where(CryptoHorizonObservation.cohort_id == cohort_id)
        ).scalars().all())

    @staticmethod
    def _status_map(observations: list[CryptoHorizonObservation]) -> dict:
        return {(o.token_address, o.horizon): o.status for o in observations}

    @staticmethod
    def _inactive_tokens(observations: list[CryptoHorizonObservation]) -> set:
        return {
            o.token_address for o in observations
            if o.status in INACTIVE_STATUSES
        }

    def build_plan(self, session: Session, cohort_id: int, now=None) -> list[HorizonPlanEntry]:
        now = now or _now()
        members = self._members(session, cohort_id)
        observations = self._observations(session, cohort_id)
        return plan_observations(
            members, self._status_map(observations),
            self._inactive_tokens(observations), now,
        )

    # --- manual observation pass -------------------------------------------

    async def observe_once(
        self, session: Session, cohort_id: int,
        limit: int = OBSERVE_DEFAULT_LIMIT, dry_run: bool = False,
    ) -> dict:
        """One manual, bounded observation pass over currently-due horizons.
        Dry-run makes ZERO external calls and persists nothing (plan preview).
        A real pass fetches nearest-due tokens first, persists ordinary price
        ticks + audit observation rows, and reports provider calls."""
        now = _now()
        cap = max(1, min(limit, OBSERVE_MAX_CALLS))
        members = self._members(session, cohort_id)
        by_token_member = {m.token_address: m for m in members}
        if not members:
            return {"status": "no_cohort", "note": HORIZON_NOTE,
                    "external_calls": 0, "cohort_id": cohort_id}

        observations = self._observations(session, cohort_id)
        plan = plan_observations(
            members, self._status_map(observations),
            self._inactive_tokens(observations), now,
        )
        due_tokens = due_fetch_order(plan)[:cap]
        due_by_token: dict[str, list[HorizonPlanEntry]] = {}
        for e in plan:
            if e.status == STATUS_DUE_NOW and e.token_address in due_tokens:
                due_by_token.setdefault(e.token_address, []).append(e)

        summary = {
            "status": "dry_run" if dry_run else "ok",
            "note": HORIZON_NOTE,
            "cohort_id": cohort_id,
            "due_observations": sum(len(v) for v in due_by_token.values()),
            "due_tokens": len(due_by_token),
            "cap": cap,
        }
        if dry_run:
            summary["external_calls"] = 0
            summary["would_fetch_tokens"] = len(due_by_token)
            summary["plan_status_counts"] = _plan_status_counts(plan)
            return summary

        calls = 0
        outcomes = {OBS_OBSERVED: 0, OBS_NO_LIQUIDITY_STATE: 0,
                    OBS_PROVIDER_NO_PAIR: 0, OBS_TOKEN_INACTIVE: 0,
                    OBS_REQUEST_FAILED: 0}
        ticks_written = 0
        for token in due_tokens:
            if calls >= cap:
                break
            member = by_token_member.get(token)
            try:
                pairs = await self.adapter.fetch_pairs_for_token(token)
                request_failed = False
            except Exception as exc:  # adapter should not raise, but be safe
                logger.warning("horizon observe fetch failed for %s: %s", token, exc)
                pairs = []
                request_failed = True
            calls += 1
            best = max(
                (p for p in pairs), key=lambda p: p.liquidity_usd or 0, default=None
            )
            for entry in due_by_token.get(token, []):
                status, cause, tick = self._record_observation(
                    session, cohort_id, member, entry, best, request_failed, now,
                )
                outcomes[status] = outcomes.get(status, 0) + 1
                if tick is not None:
                    ticks_written += 1

        session.commit()
        summary["external_calls"] = calls
        summary["provider"] = self.adapter.source_name
        summary["observations_recorded"] = sum(outcomes.values())
        summary["ticks_written"] = ticks_written
        summary["outcome_counts"] = {k: v for k, v in outcomes.items() if v}
        return summary

    def _record_observation(
        self, session, cohort_id, member, entry, best, request_failed, now,
    ):
        """Persist one observation row (+ an ordinary price tick when market
        data exists). Returns (status, missing_cause, tick_or_None)."""
        birth_at = entry.birth_at
        aged = (
            birth_at is not None
            and (now - birth_at) >= timedelta(hours=INACTIVE_AGE_HOURS)
        )
        tick = None
        price = liq = vol = mcap = fdv = pair_addr = dex = None
        if request_failed:
            status, cause = OBS_REQUEST_FAILED, OBS_REQUEST_FAILED
        elif best is None:
            # no pair from the provider — inactive if the token has aged out
            status = OBS_TOKEN_INACTIVE if aged else OBS_PROVIDER_NO_PAIR
            cause = status
        else:
            price, vol = best.price_usd, best.volume_24h_usd
            mcap, fdv = best.market_cap, best.fdv
            pair_addr, dex, liq = best.pair_address, best.dex_id, best.liquidity_usd
            # persist an ordinary tick so the tape's survival horizon can mature
            tick = CryptoPriceTick(
                chain=self.chain, token_address=entry.token_address,
                pair_address=pair_addr, observed_at=now, price_usd=price,
                liquidity_usd=liq, volume_24h_usd=vol, market_cap=mcap, fdv=fdv,
                raw_payload={"source": TICK_SOURCE, "cohort_id": cohort_id,
                             "horizon": entry.horizon, "dex_id": dex},
                created_at=now,
            )
            session.add(tick)
            session.flush()
            status = OBS_OBSERVED if liq is not None else OBS_NO_LIQUIDITY_STATE
            cause = None if liq is not None else OBS_NO_LIQUIDITY_STATE
        session.add(CryptoHorizonObservation(
            cohort_id=cohort_id, member_id=(member.id if member else None),
            chain=self.chain, token_address=entry.token_address,
            horizon=entry.horizon, target_at=entry.target_at,
            window_start=entry.window_start, window_end=entry.window_end,
            status=status, missing_cause=cause,
            tick_id=(tick.id if tick is not None else None),
            price_usd=price, liquidity_usd=liq, volume_24h_usd=vol,
            market_cap=mcap, fdv=fdv, pair_address=pair_addr, dex_id=dex,
            provider=self.adapter.source_name, observed_at=now, created_at=now,
        ))
        return status, cause, tick


def _plan_status_counts(plan: list[HorizonPlanEntry]) -> dict:
    counts: dict[str, dict] = {}
    for e in plan:
        counts.setdefault(e.horizon, {})
        counts[e.horizon][e.status] = counts[e.horizon].get(e.status, 0) + 1
    return counts


# --- shadow estimate (pre-observation) ------------------------------------------


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def shadow_estimate(session: Session, cohort_id: int, service=None) -> dict:
    """Estimate coverage gain + provider load BEFORE any real pass. Reads the
    plan only; makes no external call."""
    service = service or CryptoHorizonService()
    plan = service.build_plan(session, cohort_id)
    gain = {}
    for label, _ in HORIZONS:
        rows = [e for e in plan if e.horizon == label]
        due = sum(1 for e in rows if e.status == STATUS_DUE_NOW)
        gain[label] = {"due_now": due, "total": len(rows),
                       "expected_coverage_gain": _rate(due, len(rows))}
    # per-day call estimate: one fetch per token per due horizon, upper bound
    per_day = {
        size: size * len(HORIZONS) for size in (25, 50, 100)
    }
    return {
        "note": HORIZON_NOTE,
        "cohort_id": cohort_id,
        "expected_coverage_gain_by_horizon": gain,
        "required_calls_per_day_estimate": per_day,
        "solana_tracker_usage": (
            "none — this lane fetches via DexScreener only (free, no key, no "
            "SolanaTracker); zero SolanaTracker budget impact"
        ),
        "provider_budget_supported": True,  # DexScreener free tier ~300 rpm
        "external_calls": 0,
    }


# --- coverage report ------------------------------------------------------------


def build_observation_report(session: Session, cohort_id: int, top: int = 5) -> dict:
    service = CryptoHorizonService()
    members = service._members(session, cohort_id)
    observations = service._observations(session, cohort_id)
    plan = service.build_plan(session, cohort_id)
    obs_by_key = {(o.token_address, o.horizon): o for o in observations}

    by_horizon = {}
    for label, _ in HORIZONS:
        rows = [e for e in plan if e.horizon == label]
        due = [e for e in rows if e.status in (STATUS_DUE_NOW, STATUS_OVERDUE_UNOBSERVED)
               or (e.token_address, label) in obs_by_key]
        observed = [
            e for e in rows
            if obs_by_key.get((e.token_address, label)) is not None
            and obs_by_key[(e.token_address, label)].status == OBS_OBSERVED
        ]
        missed = [e for e in rows if e.status == STATUS_OVERDUE_UNOBSERVED
                  and (e.token_address, label) not in obs_by_key]
        # liquidity-field completion among successful observations
        liq_present = sum(
            1 for e in observed
            if obs_by_key[(e.token_address, label)].liquidity_usd is not None
        )
        by_horizon[label] = {
            "due": len(due),
            "observed": len(observed),
            "missed": len(missed),
            "completion_rate": _rate(len(observed), len(due)),
            "liquidity_field_completion_rate": _rate(liq_present, len(observed)),
        }

    status_counts: dict[str, int] = {}
    for o in observations:
        status_counts[o.status] = status_counts.get(o.status, 0) + 1
    total_obs = len(observations) or 0
    inactive = status_counts.get(OBS_TOKEN_INACTIVE, 0)
    no_pair = status_counts.get(OBS_PROVIDER_NO_PAIR, 0)

    # early-liquidity (15m/1h) field diagnostics
    early_diag: dict[str, int] = {}
    for o in observations:
        if o.horizon not in ("15m", "1h") or o.status != OBS_OBSERVED:
            continue
        if o.pair_address is None:
            early_diag[EARLY_PAIR_MISSING] = early_diag.get(EARLY_PAIR_MISSING, 0) + 1
        elif o.liquidity_usd is None and o.price_usd is not None:
            early_diag[EARLY_PRICE_ONLY] = early_diag.get(EARLY_PRICE_ONLY, 0) + 1
        elif o.liquidity_usd is None:
            early_diag[EARLY_LIQ_FIELD_MISSING] = early_diag.get(EARLY_LIQ_FIELD_MISSING, 0) + 1

    # target-distance distribution among successful observations
    dists = sorted(
        abs((_aware(o.observed_at) - _aware(o.target_at)).total_seconds())
        for o in observations
        if o.status == OBS_OBSERVED and o.target_at is not None
    )

    def pctile(p):
        if not dists:
            return None
        idx = min(len(dists) - 1, int(p * (len(dists) - 1)))
        return round(dists[idx], 1)

    # success gates (measurement only)
    gates = {}
    for label in ("15m", "1h", "6h", "24h"):
        rate = by_horizon[label]["completion_rate"]
        gates[label] = {
            "target": SUCCESS_GATES[label], "actual": rate,
            "pass": (rate is not None and rate >= SUCCESS_GATES[label]),
        }
    liq_rates = [
        by_horizon[l]["liquidity_field_completion_rate"]
        for l in ("15m", "1h") if by_horizon[l]["liquidity_field_completion_rate"] is not None
    ]
    liq_overall = round(sum(liq_rates) / len(liq_rates), 4) if liq_rates else None
    gates["liquidity_state"] = {
        "target": SUCCESS_GATES["liquidity_state"], "actual": liq_overall,
        "pass": (liq_overall is not None and liq_overall >= SUCCESS_GATES["liquidity_state"]),
    }

    examples = {
        "observed": [
            {"token": o.token_address[:16], "horizon": o.horizon,
             "liquidity_usd": o.liquidity_usd, "price_usd": o.price_usd}
            for o in observations if o.status == OBS_OBSERVED
        ][:top],
        "missed_or_inactive": [
            {"token": o.token_address[:16], "horizon": o.horizon,
             "status": o.status, "cause": o.missing_cause}
            for o in observations if o.status != OBS_OBSERVED
        ][:top],
    }

    return {
        "note": HORIZON_NOTE,
        "cohort_id": cohort_id,
        "cohort_size": len(members),
        "observations_total": total_obs,
        "by_horizon": by_horizon,
        "inactive_token_rate": _rate(inactive, total_obs),
        "provider_no_pair_rate": _rate(no_pair, total_obs),
        "observation_status_counts": status_counts,
        "early_liquidity_diagnostics": early_diag,
        "target_distance_seconds": {
            "p50": pctile(0.5), "p90": pctile(0.9),
            "min": (round(dists[0], 1) if dists else None),
            "max": (round(dists[-1], 1) if dists else None),
        },
        "success_gates": gates,
        "examples": examples,
        "provider_usage": {
            "provider": "dexscreener",
            "solana_tracker_calls": 0,
            "observations_recorded": total_obs,
        },
        "db_impact_rows": total_obs,
        "disclaimer": (
            "bounded horizon-observation coverage — market/liquidity "
            "observations only; misses recorded honestly; success gates are "
            "MEASUREMENT gates, not enforcement; never advice, no EV, no "
            "recommendation, no sizing, no orders, no wallets, no execution"
        ),
    }
