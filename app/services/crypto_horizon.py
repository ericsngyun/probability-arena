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
    CryptoToken,
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


# --- CRYPTO-HORIZON-OBS-002: pair diagnostics + deterministic selection ---------
# The first real cohort-1 pass failed 3/5 6h observations as no_liquidity_state
# because selection was `max(liquidity_usd or 0)` — which treats None and 0
# identically and never checks whether ANOTHER returned pair carried usable
# liquidity. OBS-002 inspects every candidate pair and selects the highest
# ELIGIBLE-quality pair (valid price + positive liquidity, activity-preferred),
# preserving an honest no_liquidity_state only when NO candidate has liquidity.
# This is market-quality selection for observation coverage — NOT a trading
# score, NOT a recommendation.

# selection policies (shadow-comparable)
POLICY_FIRST = "first_returned"
POLICY_MAX_LIQ = "maximum_liquidity_usd"
POLICY_HIGH_VOL = "highest_recent_volume_with_liquidity"
POLICY_NEWEST_ACTIVE = "newest_active_pair"
POLICY_LAUNCHPAD_THEN_AMM = "pump_or_launchpad_preferred_then_amm"
POLICY_QUALITY = "active_pair_quality_score"
SELECTION_POLICIES = (
    POLICY_FIRST, POLICY_MAX_LIQ, POLICY_HIGH_VOL, POLICY_NEWEST_ACTIVE,
    POLICY_LAUNCHPAD_THEN_AMM, POLICY_QUALITY,
)
# the deterministic policy the real observe pass uses
OBSERVE_POLICY = POLICY_QUALITY

# liquidity field states
LIQ_ABSENT, LIQ_NULL, LIQ_ZERO, LIQ_MALFORMED, LIQ_PRESENT = (
    "absent", "null", "zero", "malformed", "present"
)


def _txn_count(raw: dict | None, key: str) -> int:
    entry = ((raw or {}).get("txns") or {}).get(key) or {}
    try:
        return int(entry.get("buys") or 0) + int(entry.get("sells") or 0)
    except (TypeError, ValueError):
        return 0


def recent_txns(pair) -> int:
    """Recent (5m + 1h) transaction count from the raw payload (activity)."""
    raw = getattr(pair, "raw", None) or {}
    return _txn_count(raw, "m5") + _txn_count(raw, "h1")


def recent_volume(pair) -> float:
    return pair.volume_1h_usd or pair.volume_5m_usd or 0.0


def liquidity_field_state(pair) -> str:
    """Classify the liquidity field: absent / null / zero / malformed / present."""
    raw = getattr(pair, "raw", None) or {}
    if "liquidity" not in raw or raw.get("liquidity") is None:
        # fall back to the parsed value (adapter may have normalized it)
        if pair.liquidity_usd is None:
            return LIQ_ABSENT
        return LIQ_ZERO if pair.liquidity_usd == 0 else LIQ_PRESENT
    liq = raw.get("liquidity")
    usd = liq.get("usd") if isinstance(liq, dict) else liq
    if usd is None:
        return LIQ_NULL
    try:
        value = float(usd)
    except (TypeError, ValueError):
        return LIQ_MALFORMED
    return LIQ_ZERO if value == 0 else LIQ_PRESENT


def pair_is_eligible(pair) -> bool:
    """Usable for a price-tick observation: valid positive price AND positive
    liquidity. FDV / market cap / volume are NEVER treated as liquidity."""
    return (
        pair.price_usd is not None and pair.price_usd > 0
        and pair.liquidity_usd is not None and pair.liquidity_usd > 0
    )


def active_pair_quality_score(pair, token: str) -> float | None:
    """Observable market-quality score for an ELIGIBLE pair (None if
    ineligible). Liquidity is primary; recent activity/volume preferred; an
    exact base-token match preferred; stale+inactive pairs penalized. This is a
    coverage-quality score for picking the best OBSERVATION pair — never a
    trade signal, EV, or recommendation."""
    if not pair_is_eligible(pair):
        return None
    liq = pair.liquidity_usd
    txns = recent_txns(pair)
    vol = recent_volume(pair)
    active = txns > 0 or vol > 0
    score = min(liq, 1_000_000) / 10_000          # liquidity, up to ~100
    score += min(txns, 1000) * 0.2                # activity, up to 200
    score += min(vol, 1_000_000) / 20_000         # recent volume, up to 50
    if pair.base_token_address == token:
        score += 25                               # exact base-token match
    if not active:
        score -= 50                               # stale/inactive penalty
    return round(score, 4)


def _eligible(pairs, token):
    return [p for p in pairs if pair_is_eligible(p)]


def select_pair(pairs, token, policy=OBSERVE_POLICY):
    """(selected_pair_or_None, basis). Deterministic; reads only market-quality
    fields. Returns None when no pair is eligible (honest no_liquidity_state)."""
    if not pairs:
        return None, {"policy": policy, "reason": "no_pairs", "candidate_count": 0}
    elig = _eligible(pairs, token)
    basis = {
        "policy": policy, "candidate_count": len(pairs),
        "eligible_count": len(elig),
    }
    chosen = None
    if policy == POLICY_FIRST:
        chosen = pairs[0]
    elif policy == POLICY_MAX_LIQ:
        chosen = max(elig, key=lambda p: p.liquidity_usd, default=None)
    elif policy == POLICY_HIGH_VOL:
        chosen = max(elig, key=recent_volume, default=None)
    elif policy == POLICY_NEWEST_ACTIVE:
        active = [p for p in elig if recent_txns(p) > 0 or recent_volume(p) > 0]
        chosen = max(
            active,
            key=lambda p: (_aware(p.pair_created_at) or _aware(_EPOCH)),
            default=None,
        )
    elif policy == POLICY_LAUNCHPAD_THEN_AMM:
        from app.services.crypto_tape import LAUNCHPAD_DEXES
        lp = [p for p in elig if (p.dex_id or "").lower() in LAUNCHPAD_DEXES]
        pool = lp or elig
        chosen = max(pool, key=lambda p: p.liquidity_usd, default=None)
    else:  # POLICY_QUALITY
        scored = [(active_pair_quality_score(p, token), p) for p in pairs]
        scored = [(s, p) for s, p in scored if s is not None]
        if scored:
            best_score, chosen = max(scored, key=lambda sp: sp[0])
            basis["score"] = best_score
    if chosen is not None:
        basis["selected_pair"] = chosen.pair_address
        basis["selected_liquidity_usd"] = chosen.liquidity_usd
        basis["reason"] = "highest-quality eligible pair"
    else:
        basis["reason"] = (
            "no eligible pair (no candidate has valid price + positive liquidity)"
        )
    return chosen, basis


def describe_pair(pair, token: str) -> dict:
    """Compact per-candidate diagnostic (NOT the full raw payload)."""
    return {
        "pair_address": pair.pair_address,
        "dex_id": pair.dex_id,
        "pair_created_at": (
            _aware(pair.pair_created_at).isoformat() if pair.pair_created_at else None
        ),
        "recent_txns": recent_txns(pair),
        "liquidity_usd": pair.liquidity_usd,
        "liquidity_field_state": liquidity_field_state(pair),
        "volume_5m_usd": pair.volume_5m_usd,
        "volume_1h_usd": pair.volume_1h_usd,
        "volume_24h_usd": pair.volume_24h_usd,
        "price_usd": pair.price_usd,
        "fdv": pair.fdv,
        "market_cap": pair.market_cap,
        "is_base_token": pair.base_token_address == token,
        "quote_token_address": pair.quote_token_address,
        "eligible": pair_is_eligible(pair),
        "quality_score": active_pair_quality_score(pair, token),
    }


def shadow_pair_selection(pairs, token) -> dict:
    """Which pair each shadow policy would pick — comparison, no side effects."""
    out = {}
    for policy in SELECTION_POLICIES:
        chosen, _ = select_pair(pairs, token, policy)
        out[policy] = chosen.pair_address if chosen is not None else None
    return out


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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

        existing_by_key = {(o.token_address, o.horizon): o for o in observations}
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
            # OBS-002: deterministic highest-quality eligible pair (one selection
            # per token serves all its due horizons)
            selected, basis = select_pair(pairs, token, policy=OBSERVE_POLICY)
            candidates = [describe_pair(p, token) for p in pairs]
            for entry in due_by_token.get(token, []):
                status, cause, tick = self._record_observation(
                    session, cohort_id, member, entry, selected, basis,
                    candidates, request_failed, now,
                    existing=existing_by_key.get((token, entry.horizon)),
                )
                outcomes[status] = outcomes.get(status, 0) + 1
                if tick is not None:
                    ticks_written += 1

        session.commit()
        summary["external_calls"] = calls
        summary["provider"] = self.adapter.source_name
        summary["observation_policy"] = OBSERVE_POLICY
        summary["observations_recorded"] = sum(outcomes.values())
        summary["ticks_written"] = ticks_written
        summary["outcome_counts"] = {k: v for k, v in outcomes.items() if v}
        return summary

    def _record_observation(
        self, session, cohort_id, member, entry, selected, basis, candidates,
        request_failed, now, existing=None,
    ):
        """Upsert one observation row (+ an ordinary price tick ONLY when an
        eligible pair with liquidity was selected — never a null-liquidity
        tick, never liquidity fabricated from FDV/mcap/volume). A previously
        FAILED (non-observed) row is retried in place; an OBSERVED row is never
        overwritten. Returns (status, missing_cause, tick_or_None)."""
        if existing is not None and existing.status == OBS_OBSERVED:
            return OBS_OBSERVED, None, None  # frozen; never re-observe
        birth_at = entry.birth_at
        aged = (
            birth_at is not None
            and (now - birth_at) >= timedelta(hours=INACTIVE_AGE_HOURS)
        )
        tick = None
        price = liq = vol = mcap = fdv = pair_addr = dex = None
        if request_failed:
            status, cause = OBS_REQUEST_FAILED, OBS_REQUEST_FAILED
        elif basis.get("candidate_count", 0) == 0:
            # no pair from the provider — inactive if the token has aged out
            status = OBS_TOKEN_INACTIVE if aged else OBS_PROVIDER_NO_PAIR
            cause = status
        elif selected is None:
            # pairs exist but NONE eligible (no valid price + positive liquidity)
            status, cause = OBS_NO_LIQUIDITY_STATE, OBS_NO_LIQUIDITY_STATE
            # capture the best-priced candidate's PRICE for the early-liquidity
            # diagnostic (price observed, liquidity honestly absent) — NO tick,
            # NO liquidity fabricated from FDV/mcap/volume
            priced = [c for c in candidates if c.get("price_usd")]
            if priced:
                best_c = max(priced, key=lambda c: c["price_usd"])
                price = best_c.get("price_usd")
                pair_addr = best_c.get("pair_address")
                vol = best_c.get("volume_24h_usd")
                mcap, fdv = best_c.get("market_cap"), best_c.get("fdv")
        else:
            price, vol = selected.price_usd, selected.volume_24h_usd
            mcap, fdv = selected.market_cap, selected.fdv
            pair_addr, dex, liq = (
                selected.pair_address, selected.dex_id, selected.liquidity_usd
            )
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
            status, cause = OBS_OBSERVED, None

        audit = {
            "selected_pair_basis": basis,
            "candidate_count": len(candidates),
            "candidates": candidates[:12],  # bounded; never the full raw payload
        }
        if existing is not None:
            # retry-in-place: update the failed row
            existing.status = status
            existing.missing_cause = cause
            existing.tick_id = tick.id if tick is not None else None
            existing.price_usd = price
            existing.liquidity_usd = liq
            existing.volume_24h_usd = vol
            existing.market_cap = mcap
            existing.fdv = fdv
            existing.pair_address = pair_addr
            existing.dex_id = dex
            existing.provider = self.adapter.source_name
            existing.raw_payload = audit
            existing.observed_at = now
        else:
            session.add(CryptoHorizonObservation(
                cohort_id=cohort_id, member_id=(member.id if member else None),
                chain=self.chain, token_address=entry.token_address,
                horizon=entry.horizon, target_at=entry.target_at,
                window_start=entry.window_start, window_end=entry.window_end,
                status=status, missing_cause=cause,
                tick_id=(tick.id if tick is not None else None),
                price_usd=price, liquidity_usd=liq, volume_24h_usd=vol,
                market_cap=mcap, fdv=fdv, pair_address=pair_addr, dex_id=dex,
                provider=self.adapter.source_name, raw_payload=audit,
                observed_at=now, created_at=now,
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

    # OBS-002 reconciliation: every count is an explicit, disjoint bucket, and
    # each rate names its denominator (the OBS-001 report conflated overdue and
    # observed into one "due" denominator).
    by_horizon = {}
    for label, _ in HORIZONS:
        rows = [e for e in plan if e.horizon == label]
        b = {k: 0 for k in (
            "observed", "missed_attempted", "due_now", "overdue_unobserved",
            "skipped_not_due", "inactive",
        )}
        liq_present = 0
        for e in rows:
            obs = obs_by_key.get((e.token_address, label))
            if obs is not None and obs.status == OBS_OBSERVED:
                b["observed"] += 1
                if obs.liquidity_usd is not None:
                    liq_present += 1
            elif obs is not None:
                b["missed_attempted"] += 1        # attempted, not usable (retryable)
            elif e.status == STATUS_NOT_DUE:
                b["skipped_not_due"] += 1
            elif e.status == STATUS_INACTIVE:
                b["inactive"] += 1
            elif e.status == STATUS_DUE_NOW:
                b["due_now"] += 1
            elif e.status == STATUS_OVERDUE_UNOBSERVED:
                b["overdue_unobserved"] += 1
        attempted = b["observed"] + b["missed_attempted"]
        horizon_due_total = (
            attempted + b["due_now"] + b["overdue_unobserved"] + b["inactive"]
        )
        by_horizon[label] = {
            "horizon_due_total": horizon_due_total,
            "due_now": b["due_now"],
            "overdue_unobserved": b["overdue_unobserved"],
            "attempted": attempted,
            "observed": b["observed"],
            "missed_attempted": b["missed_attempted"],
            "skipped_not_due": b["skipped_not_due"],
            "skipped_already_observed": b["observed"],  # observed == already-observed on re-plan
            "inactive": b["inactive"],
            # explicit-denominator rates
            "completion_rate_of_attempts": _rate(b["observed"], attempted),
            "completion_denominator": "attempted",
            "coverage_rate_of_due": _rate(b["observed"], horizon_due_total),
            "coverage_denominator": "horizon_due_total",
            "liquidity_field_completion_rate": _rate(liq_present, b["observed"]),
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

    # success gates (measurement only) — coverage of everything that became due
    gates = {}
    for label in ("15m", "1h", "6h", "24h"):
        rate = by_horizon[label]["coverage_rate_of_due"]
        gates[label] = {
            "target": SUCCESS_GATES[label], "actual": rate,
            "denominator": "horizon_due_total",
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


# --- CRYPTO-HORIZON-OBS-002: pair-selection diagnostic report -------------------


def build_pair_selection_report(session: Session, cohort_id: int, top: int = 5) -> dict:
    """For each FAILED observation (no usable liquidity), read the candidate
    pairs captured in the observation's audit payload and report whether the
    failure was AVOIDABLE (another returned pair had usable liquidity) and what
    each shadow policy would have selected. Read-only; no external call — it
    reads the candidates a v2 observe pass persisted (re-run observe to capture
    candidates for pre-OBS-002 rows)."""
    service = CryptoHorizonService()
    observations = service._observations(session, cohort_id)
    failed = [o for o in observations if o.status == OBS_NO_LIQUIDITY_STATE]

    entries = []
    avoidable = 0
    for o in failed:
        audit = o.raw_payload or {}
        candidates = audit.get("candidates") or []
        eligible = [c for c in candidates if c.get("eligible")]
        # shadow: which policies would find an eligible pair from the captured set
        shadow = {}
        for policy in SELECTION_POLICIES:
            if policy == POLICY_FIRST:
                pick = candidates[0]["pair_address"] if candidates else None
            elif not eligible:
                pick = None
            elif policy == POLICY_MAX_LIQ:
                pick = max(eligible, key=lambda c: c["liquidity_usd"])["pair_address"]
            elif policy == POLICY_HIGH_VOL:
                pick = max(
                    eligible, key=lambda c: (c.get("volume_1h_usd") or c.get("volume_5m_usd") or 0)
                )["pair_address"]
            elif policy == POLICY_QUALITY:
                pick = max(
                    eligible, key=lambda c: (c.get("quality_score") or -1e9)
                )["pair_address"]
            else:
                pick = eligible[0]["pair_address"]
            shadow[policy] = pick
        was_avoidable = bool(eligible)
        if was_avoidable:
            avoidable += 1
        entries.append({
            "token": o.token_address[:16],
            "horizon": o.horizon,
            "pair_count": len(candidates),
            "eligible_pair_count": len(eligible),
            "current_selected_pair": (audit.get("selected_pair_basis") or {}).get("selected_pair"),
            "another_pair_had_liquidity": was_avoidable,
            "no_liquidity_state_avoidable": was_avoidable,
            "shadow_policy_selection": shadow,
            "liquidity_field_states": [c.get("liquidity_field_state") for c in candidates],
            "has_captured_candidates": bool(candidates),
        })

    return {
        "note": HORIZON_NOTE,
        "cohort_id": cohort_id,
        "failed_no_liquidity": len(failed),
        "avoidable_failures": avoidable,
        "projected_completion_improvement": _rate(avoidable, len(failed)),
        "rows_without_captured_candidates": sum(
            1 for e in entries if not e["has_captured_candidates"]
        ),
        "examples": entries[:top],
        "disclaimer": (
            "pair-selection diagnostic — market-quality coverage analysis only; "
            "selection is for OBSERVATION, never a trade signal/EV/recommendation; "
            "read-only, no external call; no wallets, keys, swaps, signing, "
            "orders, or execution"
        ),
    }


# --- CRYPTO-HORIZON-OBS-002: outcome-transition reconciliation ------------------


def build_outcome_reconciliation_report(
    session: Session, cohort_id: int, top: int = 5,
) -> dict:
    """Cohort-specific PROOF that a horizon observation flips a lifecycle
    outcome unknown -> known. For each observed horizon with a persisted tick,
    recompute survival WITH and WITHOUT that exact tick (read-only): the delta
    isolates the observation's contribution and sidesteps aggregate counts
    polluted by unrelated new births. Nothing persisted."""
    from dataclasses import replace as _replace

    from app.services.crypto_tape import CryptoLifecycleTapeRecorder

    recorder = CryptoLifecycleTapeRecorder()
    service = CryptoHorizonService()
    members = service._members(session, cohort_id)
    observations = service._observations(session, cohort_id)
    now = _now()

    births = {
        b.id: b for b in session.execute(
            select(CryptoTokenBirthEvent).where(
                CryptoTokenBirthEvent.id.in_(
                    [m.birth_event_id for m in members if m.birth_event_id]
                )
            )
        ).scalars().all()
    }
    tokens = {
        t.token_address: t for t in session.execute(
            select(CryptoToken).where(
                CryptoToken.token_address.in_([m.token_address for m in members])
            )
        ).scalars().all()
    }
    member_by_token = {m.token_address: m for m in members}

    rows = []
    transitioned = 0
    for o in observations:
        if o.status != OBS_OBSERVED or o.tick_id is None:
            continue
        member = member_by_token.get(o.token_address)
        birth = births.get(member.birth_event_id) if member else None
        if birth is None:
            continue
        # a transient stand-in is fine — _load_sources only reads .token_address
        token = tokens.get(o.token_address) or CryptoToken(
            chain=service.chain, token_address=o.token_address
        )
        sources = recorder._load_sources(session, token, now)
        key = f"survived_{o.horizon}"
        with_tick = recorder.compute_survival(birth, sources, now)["labels"].get(key)
        without = recorder.compute_survival(
            birth,
            _replace(sources, ticks=[t for t in sources.ticks if t.id != o.tick_id]),
            now,
        )["labels"].get(key)
        did = (without is None and with_tick is not None)
        if did:
            transitioned += 1
        failure = None
        if with_tick is None:
            # the observation tick did not make it measurable — why?
            target = _aware(o.target_at)
            tol_min = _minutes(o.horizon) * OBS_TOLERANCE
            in_window = (
                target is not None
                and abs((_aware(o.observed_at) - target).total_seconds()) <= tol_min * 60
            )
            failure = (
                "tick_outside_horizon_window" if not in_window
                else "liquidity_or_initial_state_missing"
            )
        rows.append({
            "token": o.token_address[:16], "horizon": o.horizon,
            "observation_id": o.id, "tick_id": o.tick_id,
            "outcome_before": without, "outcome_after": with_tick,
            "transitioned_unknown_to_known": did,
            "failure_cause_if_still_unknown": failure,
        })

    return {
        "note": HORIZON_NOTE,
        "cohort_id": cohort_id,
        "observed_with_tick": len(rows),
        "transitioned_unknown_to_known": transitioned,
        "transition_rate": _rate(transitioned, len(rows)),
        "reconciliation": rows[:top] if top else rows,
        "method": (
            "read-only: survival recomputed WITH vs WITHOUT the observation's "
            "exact tick_id, isolating its contribution (no aggregate pollution "
            "from unrelated new births); nothing persisted"
        ),
        "disclaimer": (
            "outcome-transition reconciliation — measurement of whether an "
            "observation matures a survival label; never advice, no EV, no "
            "recommendation, no sizing, no orders, no wallets, no execution"
        ),
    }
