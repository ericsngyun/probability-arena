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

import fcntl
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url
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
# CRYPTO-COVERAGE-REPAIR-001: the scheduled reconciler's interval, kept here
# rather than only in the systemd unit so the window guard can require that the
# window outlasts the closing edge PLUS one interval. If the unit's OnCalendar
# changes, change this with it.
RECONCILER_CADENCE_HOURS = 6
# liquidity below this fraction of the initial value => not survived / removed
SURVIVAL_LIQUIDITY_FRACTION = 0.3
# 24h volume below this at >=6h after birth => dead_volume
DEAD_VOLUME_24H_USD = 500.0
# bonding-curve launchpads; a later non-launchpad pair = graduated/migrated
LAUNCHPAD_DEXES = frozenset({"pumpfun", "moonshot", "launchlab"})

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"
# CRYPTO-COVERAGE-REPAIR-001 write-coordination hardening — statuses a caller
# must treat as "not fully done", distinct from ok/dry_run:
STATUS_PARTIAL = "partial"                       # stopped early (deadline or
    # exhausted lock retries) but committed whatever batches it finished;
    # restart-safe, the next pass continues via oldest-first + backlog.
STATUS_SKIPPED_OVERLAP = "skipped_overlap"        # another pass already holds
    # the per-chain overlap lock; nothing was read or written this call.
STATUS_SKIPPED_CONTENTION = "skipped_contention"  # the very first write of the
    # pass (the run row) never got a lock even after the full retry ladder;
    # nothing was written.
STATUS_DRY_RUN_PARTIAL = "dry_run_partial"        # a dry-run probe that was
    # ITSELF stopped early (deadline or exhausted lock retries) before it
    # could examine the whole selected set. Nothing is ever written by a dry
    # run, so this is distinct from STATUS_PARTIAL (which implies some
    # batches committed); it exists so a truncated probe never reports plain
    # "dry_run", which looks indistinguishable from a complete one.

BONDING_LAUNCHPAD = "launchpad_curve"
BONDING_AMM = "amm_pool"

# CRYPTO-TAPE-CADENCE-002: SQLite write-lock resilience. On a shared host the
# baseline/watcher/MarketOps writers can hold the write lock past the DB busy
# timeout, so a capture's run-row INSERT raises "database is locked". A bounded
# app-level retry (mirrors the OPS-013 tick-aggregation idiom) recovers from
# transient contention; a persistent lock aborts loudly and CLEANLY (session
# rolled back first) so the summary path never hits PendingRollbackError.
# Defined here (not just near run_tape_session, its original home) because
# CRYPTO-COVERAGE-REPAIR-001's per-batch commit retry in `_assemble_pass` and
# `run_once` need it as a default argument value, which is evaluated at
# `def` time and must already exist.
DB_LOCKED_MAX_ATTEMPTS = 3       # total tries per capture (1 + 2 retries)
DB_LOCKED_RETRY_SECONDS = 3.0    # short wait between attempts
ABORT_DB_LOCKED = "database_locked"

# CRYPTO-COVERAGE-REPAIR-001 B1/B3 — measured blocker: `_assemble_pass` used to
# be one write transaction for the whole pass (36.9s measured at production
# density, blocking a competing writer 97% of a 30s busy_timeout). Bounding
# each committed transaction to a small, fixed batch of tokens keeps the write
# lock held for a small fraction of a second at a time instead of for the
# whole pass. 25 is the shipped default from the B1 profile (see the
# CRYPTO-COVERAGE-REPAIR-001 debugging session): at ~51 ticks/28 discovery
# events/23 risk assessments/2 pairs per token, a 25-token batch's write phase
# measured well under one second.
RECONCILE_BATCH_SIZE = 25
# Internal wall-clock deadline for one `run_once` call. None = unbounded
# (existing manual-path behaviour, unchanged). The scheduled path sets this so
# one pass can never run indefinitely; remaining tokens simply stay backlog
# for the next scheduled pass (oldest-first + state-driven selection already
# guarantee they are not starved — see `unreconciled_backlog`).
RECONCILE_MAX_DURATION_SECONDS = 20.0
# Overlap guard: a coordination-only flock file, one per chain, living next to
# the sqlite file itself (or the system temp dir for non-sqlite/in-memory
# configurations). Never touches the database's own locking; kernel-released
# if the process dies, so a crash can never leave a stale lock.
RECONCILE_LOCK_FILENAME = ".crypto-tape-reconcile-{chain}.lock"


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


def _resolve_lock_dir(settings: Settings | None) -> Path:
    """Host-local directory to anchor the reconciliation overlap lock (B4).
    Prefers the directory the sqlite file itself lives in — co-located,
    host-scoped, and stable across process restarts. Falls back to the system
    temp dir for non-sqlite backends and the in-memory `sqlite://` tests use
    (no file to co-locate with)."""
    s = settings or get_settings()
    try:
        url = make_url(s.database_url)
        if url.get_backend_name() == "sqlite" and url.database:
            return Path(url.database).resolve().parent
    except Exception:  # pragma: no cover - defensive (malformed URL, etc.)
        pass
    return Path(tempfile.gettempdir())


@contextmanager
def _reconcile_overlap_lock(lock_dir: Path, chain: str):
    """Bounded, non-blocking, kernel-held (flock) overlap guard (B4) so the
    scheduled reconciler, a manual tape session, and a second concurrent
    instance can never mutate the same chain's reconciliation window at once.

    A coordination-only file — this never touches the database's own locking,
    and it is orthogonal to SQLite's busy_timeout/retry ladder (B5), which
    guards against unrelated writers (MarketOps, the watcher, ...). The kernel
    releases an flock automatically when the holding process dies, so a
    crashed pass can never leave a stale lock behind (no TOCTOU PID file, no
    unbounded wait). Yields True when acquired, False when another pass
    already holds it. Mirrors `app.services.backup._backup_lock`."""
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        pass
    lock_path = lock_dir / RECONCILE_LOCK_FILENAME.format(chain=chain)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
    finally:
        os.close(fd)


@dataclass
class CryptoTapeConfig:
    chain: str = "solana"
    default_limit: int = 25
    default_window_hours: int = 48
    lock_dir: Path | None = None  # resolved lazily from settings if None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "CryptoTapeConfig":
        s = settings or get_settings()
        return cls(chain=s.crypto_chain, lock_dir=_resolve_lock_dir(s))


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

    def _universe(
        self, session: Session, limit: int, cutoff: datetime,
        *, oldest_first: bool = False, exclude_final: bool = False,
    ) -> list[CryptoToken]:
        """Tokens in the window. `oldest_first` inverts the ordering for
        CRYPTO-COVERAGE-REPAIR-001: newest-first truncation drops exactly the
        MATURED tokens (the ones whose horizons have closed and whose evidence
        is about to be pruned), while the newest tokens it keeps have no due
        horizon at all. Oldest-first truncation drops the unmatured tail
        instead, which the next pass picks up anyway because those tokens are
        still inside the window. Default preserves the existing manual-path
        behaviour; only the scheduled reconciler opts in.

        `exclude_final` (CRYPTO-COVERAGE-REPAIR-001 B2 fix) — when True,
        tokens whose survival outcome is already `final` are dropped from
        this query entirely. Without this, a deadline-stopped pass over an
        oldest-first ordering RE-SELECTS THE IDENTICAL HEAD on every
        subsequent pass (nothing about `first_seen_at` changes when a token
        is reconciled), so a deadline stop never makes forward progress and
        the pass also wastes its wall-clock budget re-walking tokens that
        are already final and have nothing left to learn. Left OFF by
        default: the manual/CLI path intentionally keeps reprocessing every
        token in the window on every pass (see
        test_manual_path_still_appends_lifecycle_rows_unchanged); only the
        scheduled reconciler, whose whole point is state-driven selection,
        opts in."""
        order = (
            (CryptoToken.first_seen_at.asc(), CryptoToken.id.asc())
            if oldest_first
            else (CryptoToken.first_seen_at.desc(), CryptoToken.id.desc())
        )
        stmt = select(CryptoToken).where(
            CryptoToken.chain == self.config.chain,
            CryptoToken.first_seen_at >= cutoff,
        )
        if exclude_final:
            stmt = stmt.outerjoin(
                CryptoTokenSurvivalOutcome,
                CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
            ).where(
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                )
            ).distinct()
        stmt = stmt.order_by(*order).limit(limit)
        return list(session.execute(stmt).scalars().all())

    def unreconciled_backlog(
        self, session: Session, cutoff: datetime, *, limit: int
    ) -> list[CryptoToken]:
        """Tokens OLDER than the window whose outcome is still not final —
        including tokens that were NEVER reconciled at all (no outcome row
        yet). An INNER join here is a silent data-loss bug: a token that no
        pass has ever reached has no outcome row, so once it ages out of the
        window an INNER join makes it invisible to both this query and
        `backlog_size` forever, permanently capping how much pre-existing
        backlog can ever be recovered. The OUTER join fixes that.

        Window-driven selection alone is lossy: the window carries only
        `window_hours - closing_edge` of slack (12h at the shipped defaults),
        so two missed passes — host down, lock contention, a flag toggle —
        push a cohort out of the window permanently, and the same gap means the
        pre-existing backlog is never reconciled at first enablement. Selection
        must therefore be driven by STATE (is this outcome still open, or
        nonexistent?) and not only by recency. Oldest-first, because that
        evidence is closest to being pruned."""
        return list(session.execute(
            select(CryptoToken)
            .outerjoin(
                CryptoTokenSurvivalOutcome,
                CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
            )
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at < cutoff,
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                ),
            )
            .order_by(CryptoToken.first_seen_at.asc(), CryptoToken.id.asc())
            .distinct()
            .limit(limit)
        ).scalars().all())

    def backlog_size(self, session: Session, cutoff: datetime) -> int:
        """How many still-open (or never-reconciled) outcomes sit outside the
        window. Reported so a shortfall is visible rather than inferred. See
        `unreconciled_backlog` for why this must be an OUTER join."""
        return int(session.execute(
            select(func.count(func.distinct(CryptoToken.id)))
            .select_from(CryptoToken)
            .outerjoin(
                CryptoTokenSurvivalOutcome,
                CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
            )
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at < cutoff,
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                ),
            )
        ).scalar() or 0)

    def universe_size(
        self, session: Session, cutoff: datetime, *, exclude_final: bool = False,
    ) -> int:
        """How many tokens the window actually holds, independent of any limit.
        Without this a truncated pass is indistinguishable from a complete one,
        which is the silent-under-reconciliation class this milestone removes.

        `exclude_final` MUST mirror whatever value the caller passes to
        `_universe`/`run_once` — otherwise a fully-reconciled (all-final)
        window counts as "work remains" against a selection query that has
        already correctly excluded that work, and every subsequent pass
        reports a false `truncated`."""
        count_col = (
            func.count(func.distinct(CryptoToken.id))
            if exclude_final else func.count()
        )
        stmt = select(count_col).select_from(CryptoToken).where(
            CryptoToken.chain == self.config.chain,
            CryptoToken.first_seen_at >= cutoff,
        )
        if exclude_final:
            stmt = stmt.outerjoin(
                CryptoTokenSurvivalOutcome,
                CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
            ).where(
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                )
            )
        return int(session.execute(stmt).scalar() or 0)

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
        *,
        oldest_first: bool = False,
        include_backlog: bool = False,
        exclude_final: bool = False,
        run_config_extra: dict | None = None,
        skip_redundant_when_final: bool = False,
        batch_size: int | None = None,
        max_duration_seconds: float | None = None,
        max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
        lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
        use_overlap_lock: bool = True,
        sleeper=time.sleep,
    ) -> dict:
        """One bounded reconciliation pass.

        CRYPTO-COVERAGE-REPAIR-001 write-coordination parameters (all default
        to the pre-existing manual-path behaviour so nothing changes unless a
        caller opts in — `run_scheduled_reconciliation` is the only caller
        that does):

        * `skip_redundant_when_final` (B2) — once a token's outcome is
          already final, its window is closed and re-appending a lifecycle
          snapshot/actor-observation row teaches nothing new; skip them.
        * `batch_size` (B3) — commit in bounded batches of this many tokens
          instead of one commit for the whole pass, so the write lock is held
          for a small fraction of a second at a time. None keeps the old
          single-commit behaviour (needed by dry-run and by tests asserting
          nothing is ever partially persisted).
        * `max_duration_seconds` (B6) — internal wall-clock deadline; the pass
          stops after whichever batch is in flight when the deadline is
          crossed and reports `stop_reason="deadline"`. None = unbounded.
        * `max_lock_attempts` / `lock_retry_seconds` (B5) — reuses the same
          DB_LOCKED_* retry ladder `run_tape_session` already uses, applied
          per batch commit. Exhausting it on the very first write yields
          `status="skipped_contention"`; exhausting it after some batches
          already committed yields `status="partial"`,
          `stop_reason="contention"` — the batches that already committed are
          real, durable work, not corruption.
        * `use_overlap_lock` (B4) — a non-blocking, per-chain flock guard so
          two reconciliation passes (scheduled, manual, or a second instance)
          can never mutate the same window concurrently. Disabled only by
          tests exercising `_assemble_pass` directly under a lock they hold
          themselves.
        * `exclude_final` (B2 fix) — drop already-final tokens from the
          in-window selection entirely, so a deadline-stopped, oldest-first
          pass makes forward progress on its NEXT invocation instead of
          re-selecting the identical head. False keeps the manual path's
          historical "reprocess everything in the window, every pass"
          behaviour; only `run_scheduled_reconciliation` opts in.
        """
        started = _now()
        limit = limit if limit is not None else self.config.default_limit
        hours = hours if hours is not None else self.config.default_window_hours
        cutoff = started - timedelta(hours=hours)
        tokens = self._universe(
            session, limit, cutoff, oldest_first=oldest_first,
            exclude_final=exclude_final,
        )
        total = self.universe_size(session, cutoff, exclude_final=exclude_final)
        backlog_total = 0
        if include_backlog:
            # State-driven top-up: still-open outcomes that have aged out of the
            # window. Without this a missed pass loses a cohort permanently.
            backlog_total = self.backlog_size(session, cutoff)
            room = max(0, limit - len(tokens))
            if room:
                seen = {t.token_address for t in tokens}
                extra = [
                    t for t in self.unreconciled_backlog(session, cutoff, limit=room)
                    if t.token_address not in seen
                ]
                tokens = tokens + extra
        config = {"limit": limit, "hours": hours, "chain": self.config.chain}
        config.update(run_config_extra or {})
        summary = self._assemble_pass(
            session, tokens, started=started, dry_run=dry_run,
            window_hours=hours,
            run_config=config,
            skip_redundant_when_final=skip_redundant_when_final,
            batch_size=batch_size,
            max_duration_seconds=max_duration_seconds,
            max_lock_attempts=max_lock_attempts,
            lock_retry_seconds=lock_retry_seconds,
            use_overlap_lock=use_overlap_lock,
            sleeper=sleeper,
        )
        # A cap that silently drops work reads as "complete" to every caller.
        summary["universe_size"] = total
        summary["backlog_size"] = backlog_total
        summary["work_available"] = total + backlog_total
        tokens_accounted = summary.get("tokens_processed", len(tokens))
        summary["truncated"] = (total + backlog_total) > len(tokens)
        summary["tokens_omitted"] = max(0, (total + backlog_total) - tokens_accounted)
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
            # CRYPTO-COVERAGE-REPAIR-001 B1 fix: this is a single bounded
            # transaction over a validated <=MAX_ANCHOR_FEED_TOKENS_PER_CYCLE
            # token set that `_assemble_pass` never chunks/retries for this
            # caller (batch_size stays None => legacy single-commit mode), so
            # it does not need — and must not take — the B4 overlap flock.
            # The anchor feed is EXACT-CYCLE: a skipped cycle is never
            # retried, so silently deferring to another lock holder here
            # would zero out an anchor-feed cycle for good.
            use_overlap_lock=False,
        )
        # Defensive: `_assemble_pass` can still, in principle, return a
        # non-normal summary (e.g. if a future change adds a path that skips
        # or truncates work here). A skipped/degraded summary must never be
        # allowed to serialize as "ok, all anchors already existed" — that is
        # exactly the fabricated-anchors-existing failure this fix removes.
        pass_status = summary.get("status")
        if pass_status not in (STATUS_OK, STATUS_DRY_RUN):
            return _result(
                "degraded",
                error=(
                    f"underlying reconciliation pass returned "
                    f"status={pass_status!r} instead of ok/dry_run; refusing "
                    "to report anchor counts derived from that result "
                    f"(pass error: {summary.get('error')})"
                ),
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

    def _commit_with_retry(
        self, session: Session, prepare, max_attempts: int, retry_seconds: float,
        sleeper=time.sleep,
    ) -> tuple[bool, int]:
        """CRYPTO-COVERAGE-REPAIR-001 B5 — bounded retry ladder for one commit,
        reusing the DB_LOCKED_* constants `run_tape_session` already uses.
        `prepare()` is called before every attempt (including the first) and
        must (re)stage this attempt's writes via `session.add(...)` /
        attribute assignment — required because a rolled-back SQLAlchemy
        session EXPIRES already-persistent objects, silently discarding any
        staged-but-uncommitted attribute change, so simply retrying
        `session.commit()` after a rollback would commit stale/empty state.
        Returns (committed, attempts_used); never raises for a lock error —
        a non-lock error still propagates so the caller's normal error
        handling (mark the run row, re-raise) applies unchanged. A bounded
        for-loop over a small, explicit `max_attempts`, never an unbounded
        loop — this module is audited for autonomy vocabulary (see
        test_no_timer_or_daemon_vocabulary_in_session_code)."""
        for attempt in range(1, max(1, max_attempts) + 1):
            prepare()
            try:
                session.commit()
                return True, attempt
            except OperationalError as exc:
                session.rollback()
                if _is_db_locked(exc) and attempt < max_attempts:
                    sleeper(retry_seconds)
                    continue
                if _is_db_locked(exc):
                    return False, attempt
                raise
        return False, max(1, max_attempts)  # pragma: no cover - defensive

    def _process_batch(
        self,
        session: Session,
        chunk: list,
        *,
        run: "CryptoTokenLifecycleRun | None",
        started: datetime,
        dry_run: bool,
        existing_births_snapshot: dict,
        final_by_birth_id: dict,
        skip_redundant_when_final: bool,
    ) -> dict:
        """Process one bounded chunk of tokens: reads (`_load_sources`),
        object construction, and the `session.add()`/`flush()` calls this
        chunk needs. Does NOT commit — the caller commits (with retry) once
        per chunk. Deliberately pure with respect to the CALLER's running
        totals: everything this chunk did is returned as a delta, not
        mutated in place, so a rolled-back retry can call this again from
        scratch with zero double-counting.

        CRYPTO-COVERAGE-REPAIR-001 B2 write classification applied here:
          * birth event   — REQUIRED_FOR_OUTCOME (the anchor everything else
            hangs off); always written when new.
          * survival outcome — REQUIRED_FOR_OUTCOME; always upserted, but a
            row whose `final` is already True is never rewritten (pre-existing
            behaviour, unchanged).
          * lifecycle snapshot / actor observation — REQUIRED_FOR_AUDIT the
            FIRST time a token is seen, DERIVED/HISTORICAL_ARTIFACT on every
            later pass once the outcome is final (the window is closed; nothing
            new can be learned). `skip_redundant_when_final` skips exactly
            that redundant case; the manual path never sets it, so its
            row-append behaviour is byte-for-byte unchanged.
        """
        new_births = snapshots = actors = outcomes = 0
        snapshots_skipped = actors_skipped = 0
        coverage_delta = {
            "tokens_with_ticks": 0,
            "tokens_with_risk": 0,
            "tokens_with_provider_backed_risk": 0,
            "tokens_with_attention": 0,
            "tokens_without_any_source": 0,
        }
        survival_delta: dict[str, int] = {}
        new_examples: list[dict] = []
        births_seen: list[CryptoTokenBirthEvent] = []
        existing_births = dict(existing_births_snapshot)  # chunk-local copy

        for token in chunk:
            sources = self._load_sources(session, token, started)
            if sources.ticks:
                coverage_delta["tokens_with_ticks"] += 1
            if sources.assessments:
                coverage_delta["tokens_with_risk"] += 1
                if any(a.provider_names for a in sources.assessments):
                    coverage_delta["tokens_with_provider_backed_risk"] += 1
            if sources.attention is not None:
                coverage_delta["tokens_with_attention"] += 1
            if not (sources.ticks or sources.assessments or sources.attention):
                coverage_delta["tokens_without_any_source"] += 1

            birth = existing_births.get(token.token_address)
            is_new_birth = birth is None
            if birth is None:
                birth = self.build_birth_event(sources, started)
                new_births += 1
                if not dry_run:
                    birth.run_id = run.id
                    session.add(birth)
                    session.flush()
                    existing_births[token.token_address] = birth
            births_seen.append(birth)

            already_final = (
                not is_new_birth and birth.id is not None
                and final_by_birth_id.get(birth.id, False)
            )
            skip_snapshot_actor = skip_redundant_when_final and already_final

            snapshot = None
            if skip_snapshot_actor:
                snapshots_skipped += 1
                actors_skipped += 1
            else:
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
                    survival_delta[label] = survival_delta.get(label, 0) + 1
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

            if len(new_examples) < 5:
                new_examples.append({
                    "token": token.token_address[:16],
                    "symbol": token.symbol,
                    "launch_source": birth.launch_source,
                    "risk_level": snapshot.risk_level if snapshot is not None else None,
                    "top10_holder_pct": (
                        snapshot.top10_holder_pct if snapshot is not None else None
                    ),
                    "labels": {
                        k: v for k, v in survival["labels"].items() if v is not None
                    },
                })

        return {
            "new_births": new_births, "snapshots": snapshots, "actors": actors,
            "outcomes": outcomes, "snapshots_skipped": snapshots_skipped,
            "actors_skipped": actors_skipped, "coverage_delta": coverage_delta,
            "survival_delta": survival_delta, "examples": new_examples,
            "births_seen": births_seen, "existing_births": existing_births,
        }

    def _assemble_pass(
        self,
        session: Session,
        tokens: list,
        *,
        started: datetime,
        dry_run: bool,
        window_hours: int | None,
        run_config: dict,
        skip_redundant_when_final: bool = False,
        batch_size: int | None = None,
        max_duration_seconds: float | None = None,
        max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
        lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
        use_overlap_lock: bool = True,
        sleeper=time.sleep,
    ) -> dict:
        """B4 overlap guard entry point. A non-blocking, per-chain flock (see
        `_reconcile_overlap_lock`) wraps the ENTIRE pass — from the
        `existing_births` read through the last commit — because the
        measured race this milestone found was exactly a concurrent pass's
        pre-transaction `existing_births` read colliding with another pass's
        insert, producing an IntegrityError that discarded the whole pass.
        Dry-run never mutates anything, so it never takes the lock (two
        concurrent dry probes are harmless)."""
        if dry_run or not use_overlap_lock:
            return self._assemble_pass_locked(
                session, tokens, started=started, dry_run=dry_run,
                window_hours=window_hours, run_config=run_config,
                skip_redundant_when_final=skip_redundant_when_final,
                batch_size=batch_size, max_duration_seconds=max_duration_seconds,
                max_lock_attempts=max_lock_attempts,
                lock_retry_seconds=lock_retry_seconds, sleeper=sleeper,
            )
        lock_dir = self.config.lock_dir or _resolve_lock_dir(None)
        with _reconcile_overlap_lock(lock_dir, self.config.chain) as acquired:
            if not acquired:
                return {
                    "status": STATUS_SKIPPED_OVERLAP,
                    "note": TAPE_NOTE,
                    "external_calls": 0,
                    "window_hours": window_hours,
                    "tokens_considered": 0,
                    "tokens_processed": 0,
                    "birth_events_created": 0,
                    "snapshots_created": 0,
                    "actor_observations_created": 0,
                    "outcomes_updated": 0,
                    "snapshots_skipped_redundant": 0,
                    "actor_observations_skipped_redundant": 0,
                    "provider_coverage": {}, "survival_label_mix": {},
                    "examples": [], "batches_committed": 0,
                    "batch_size": batch_size, "stop_reason": "overlap",
                    "lock_retry_events": 0,
                    "error": (
                        f"another crypto-tape reconciliation pass already "
                        f"holds the {self.config.chain} overlap lock at "
                        f"{lock_dir / RECONCILE_LOCK_FILENAME.format(chain=self.config.chain)}; "
                        "nothing was read or written"
                    ),
                    "_births": [],
                }
            return self._assemble_pass_locked(
                session, tokens, started=started, dry_run=dry_run,
                window_hours=window_hours, run_config=run_config,
                skip_redundant_when_final=skip_redundant_when_final,
                batch_size=batch_size, max_duration_seconds=max_duration_seconds,
                max_lock_attempts=max_lock_attempts,
                lock_retry_seconds=lock_retry_seconds, sleeper=sleeper,
            )

    def _assemble_pass_locked(
        self,
        session: Session,
        tokens: list,
        *,
        started: datetime,
        dry_run: bool,
        window_hours: int | None,
        run_config: dict,
        skip_redundant_when_final: bool,
        batch_size: int | None,
        max_duration_seconds: float | None,
        max_lock_attempts: int,
        lock_retry_seconds: float,
        sleeper=time.sleep,
    ) -> dict:
        """The actual assembly work, run with the overlap lock already held
        (or dry-run, which needs none).

        `batch_size` is the fork point between two modes, and this is
        deliberate — every existing caller before CRYPTO-COVERAGE-REPAIR-001
        leaves it `None` and must see byte-identical behaviour:

        * `batch_size is None` (LEGACY, unchanged) — one flush for the run
          row, one pass over all tokens, one commit at the end, no retry
          ladder around any of it. This is exactly the pre-milestone
          `_assemble_pass` body, including "any exception (lock or not)
          raises straight through, caught only by the outer error-row
          handler" — `record_discovery_run`'s "one bounded transaction"
          guarantee (pinned by
          test_crypto_anchor_feed_measurement_001::test_one_bounded_
          transaction) depends on this.
        * `batch_size` given (B3, opt-in — only `run_scheduled_reconciliation`
          sets it) — the run row is created in its own short retried
          transaction, each token batch commits (with retry) on its own, and
          the run row is finalized in a last short retried transaction. B6's
          wall-clock deadline only applies in this mode, between batches,
          never mid-batch."""
        hours = window_hours
        chunked = batch_size is not None
        deadline = (
            started + timedelta(seconds=max_duration_seconds)
            if (chunked and max_duration_seconds is not None) else None
        )

        # --- read phase: no write transaction is open for any of this -------
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
        existing_birth_ids = [b.id for b in existing_births.values() if b.id is not None]
        final_by_birth_id: dict[int, bool] = {
            row[0]: bool(row[1])
            for row in session.execute(
                select(
                    CryptoTokenSurvivalOutcome.birth_event_id,
                    CryptoTokenSurvivalOutcome.final,
                ).where(CryptoTokenSurvivalOutcome.birth_event_id.in_(existing_birth_ids))
            )
        } if existing_birth_ids else {}

        new_births = snapshots = actors = outcomes = 0
        snapshots_skipped = actors_skipped = 0
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
        lock_retry_events = 0
        batches_committed = 0
        tokens_processed = 0
        stop_reason: str | None = None

        run: CryptoTokenLifecycleRun | None = None
        if not dry_run:
            run = CryptoTokenLifecycleRun(
                status="running", started_at=started, window_hours=hours,
                config=run_config, created_at=started,
            )
            if chunked:
                def _prepare_run_creation() -> None:
                    session.add(run)

                ok, attempts = self._commit_with_retry(
                    session, _prepare_run_creation, max_lock_attempts,
                    lock_retry_seconds, sleeper,
                )
                lock_retry_events += max(0, attempts - 1)
                if not ok:
                    return {
                        "status": STATUS_SKIPPED_CONTENTION,
                        "note": TAPE_NOTE,
                        "external_calls": 0,
                        "window_hours": hours,
                        "tokens_considered": 0,
                        "tokens_processed": 0,
                        "birth_events_created": 0,
                        "snapshots_created": 0,
                        "actor_observations_created": 0,
                        "outcomes_updated": 0,
                        "snapshots_skipped_redundant": 0,
                        "actor_observations_skipped_redundant": 0,
                        "provider_coverage": coverage_summary,
                        "survival_label_mix": {},
                        "examples": [], "batches_committed": 0,
                        "batch_size": batch_size, "stop_reason": "contention",
                        "lock_retry_events": lock_retry_events,
                        "error": (
                            f"database is locked; exhausted {max_lock_attempts} "
                            "attempts before the run row could even be created "
                            "— nothing was written"
                        ),
                        "_births": [],
                    }
            else:
                # LEGACY single-transaction mode: flush only (no commit, no
                # retry) — identical to the pre-milestone `_assemble_pass`.
                session.add(run)
                session.flush()

        effective_batch = batch_size or max(len(tokens), 1)
        chunks = [
            tokens[i:i + effective_batch] for i in range(0, len(tokens), effective_batch)
        ] if tokens else []

        try:
            for chunk in chunks:
                if chunked and deadline is not None and _now() >= deadline and tokens_processed > 0:
                    stop_reason = "deadline"
                    break

                if chunked:
                    result = None
                    for attempt in range(1, max_lock_attempts + 1):
                        try:
                            result = self._process_batch(
                                session, chunk, run=run, started=started,
                                dry_run=dry_run,
                                existing_births_snapshot=existing_births,
                                final_by_birth_id=final_by_birth_id,
                                skip_redundant_when_final=skip_redundant_when_final,
                            )
                            if not dry_run:
                                session.commit()
                            break
                        except OperationalError as exc:
                            session.rollback()
                            result = None
                            if _is_db_locked(exc):
                                lock_retry_events += 1
                                if attempt < max_lock_attempts:
                                    sleeper(lock_retry_seconds)
                                    continue
                                break  # exhausted — handled below as contention
                            raise  # a real DB error, not lock contention

                    if result is None:
                        stop_reason = "contention"
                        break
                else:
                    # LEGACY: no retry, no intermediate commit — any exception
                    # (lock or not) propagates straight to the outer handler,
                    # exactly like the pre-milestone single-transaction pass.
                    result = self._process_batch(
                        session, chunk, run=run, started=started, dry_run=dry_run,
                        existing_births_snapshot=existing_births,
                        final_by_birth_id=final_by_birth_id,
                        skip_redundant_when_final=skip_redundant_when_final,
                    )

                existing_births.update(result["existing_births"])
                new_births += result["new_births"]
                snapshots += result["snapshots"]
                actors += result["actors"]
                outcomes += result["outcomes"]
                snapshots_skipped += result["snapshots_skipped"]
                actors_skipped += result["actors_skipped"]
                for key, delta in result["coverage_delta"].items():
                    coverage_summary[key] += delta
                for label, delta in result["survival_delta"].items():
                    survival_mix[label] = survival_mix.get(label, 0) + delta
                if len(examples) < 5:
                    examples.extend(result["examples"][: 5 - len(examples)])
                births_seen.extend(result["births_seen"])
                tokens_processed += len(chunk)
                batches_committed += 1

            summary = {
                "status": STATUS_DRY_RUN if dry_run else STATUS_OK,
                "note": TAPE_NOTE,
                "external_calls": 0,
                "window_hours": hours,
                "tokens_considered": tokens_processed,
                "tokens_processed": tokens_processed,
                "birth_events_created": new_births,
                "snapshots_created": snapshots,
                "actor_observations_created": actors,
                "outcomes_updated": outcomes,
                "snapshots_skipped_redundant": snapshots_skipped,
                "actor_observations_skipped_redundant": actors_skipped,
                "provider_coverage": coverage_summary,
                "survival_label_mix": dict(sorted(survival_mix.items())),
                "examples": examples,
                "batches_committed": batches_committed,
                "batch_size": batch_size,
                "stop_reason": stop_reason,
                "lock_retry_events": lock_retry_events,
                "_births": births_seen,
            }
            if stop_reason is not None:
                if dry_run:
                    # H1 fix: a dry-run probe truncated by the deadline (or
                    # exhausted lock retries) must never report plain
                    # "dry_run" — that is indistinguishable from a complete
                    # probe to every caller, including the CLI exit code.
                    summary["status"] = STATUS_DRY_RUN_PARTIAL
                    summary["error"] = (
                        f"dry-run probe stopped early (stop_reason={stop_reason}) "
                        f"after examining {tokens_processed} of {len(tokens)} "
                        "selected tokens; nothing was written (dry-run never "
                        "writes) and nothing beyond the examined tokens was "
                        "measured"
                    )
                else:
                    summary["status"] = STATUS_PARTIAL
                    summary["error"] = (
                        f"pass stopped early (stop_reason={stop_reason}) after "
                        f"{batches_committed} batch(es) / {tokens_processed} of "
                        f"{len(tokens)} selected tokens; already-committed "
                        "batches are durable — nothing is duplicated or lost — "
                        "the remaining tokens stay eligible for a future pass"
                    )
            if dry_run:
                return summary

            finished = _now()

            if chunked:
                def _prepare_finalize() -> None:
                    run.status = summary["status"]
                    run.finished_at = finished
                    run.duration_ms = max(
                        0, int((finished - started).total_seconds() * 1000)
                    )
                    run.tokens_considered = tokens_processed
                    run.birth_events_created = new_births
                    run.snapshots_created = snapshots
                    run.actor_observations_created = actors
                    run.outcomes_updated = outcomes
                    run.provider_coverage = coverage_summary
                    # NEW-H1 fix: B2's skip-when-final path makes
                    # `snapshots_created`/`actor_observations_created` mean
                    # different things depending on whether this run skipped
                    # redundant writes for already-final tokens — without
                    # recording that classification on the run row itself,
                    # a later reader of the DB (e.g. build_tape_report) has
                    # no way to tell "all tokens got a fresh snapshot" apart
                    # from "only non-final tokens did".
                    run.config = {
                        **(run.config or {}),
                        "write_classification": {
                            "skip_redundant_when_final": skip_redundant_when_final,
                            "snapshots_skipped_redundant": snapshots_skipped,
                            "actor_observations_skipped_redundant": actors_skipped,
                        },
                    }
                    session.add(run)

                ok, attempts = self._commit_with_retry(
                    session, _prepare_finalize, max_lock_attempts,
                    lock_retry_seconds, sleeper,
                )
                lock_retry_events += max(0, attempts - 1)
                summary["lock_retry_events"] = lock_retry_events
                if not ok:
                    # The token batches already committed are real, durable,
                    # correct work — only the run row's own bookkeeping commit
                    # lost the lock race. Never raise: the reconciliation
                    # itself already succeeded (or partially succeeded);
                    # losing the summary row is not the same failure as
                    # losing data.
                    summary["status"] = STATUS_PARTIAL
                    summary["stop_reason"] = summary["stop_reason"] or "contention"
                    summary["tape_run_id"] = run.id
                    finalize_error = (
                        "reconciliation batches committed, but the run row's "
                        "own finalize commit could not acquire the lock; the "
                        "run row stays status=running"
                    )
                    # Append, don't replace: a deadline/contention stop
                    # earlier in the pass already set an "N batches / X of Y
                    # tokens" data-shortfall message, and that is different,
                    # equally real information from the finalize-commit
                    # failure — losing either one is a loss of signal.
                    summary["error"] = (
                        f"{summary['error']}; additionally, {finalize_error}"
                        if summary.get("error") else finalize_error
                    )
                    return summary
            else:
                # LEGACY single-transaction mode: one commit, no retry —
                # identical to the pre-milestone `_assemble_pass`.
                run.status = summary["status"]
                run.finished_at = finished
                run.duration_ms = max(
                    0, int((finished - started).total_seconds() * 1000)
                )
                run.tokens_considered = tokens_processed
                run.birth_events_created = new_births
                run.snapshots_created = snapshots
                run.actor_observations_created = actors
                run.outcomes_updated = outcomes
                run.provider_coverage = coverage_summary
                run.config = {
                    **(run.config or {}),
                    "write_classification": {
                        "skip_redundant_when_final": skip_redundant_when_final,
                        "snapshots_skipped_redundant": snapshots_skipped,
                        "actor_observations_skipped_redundant": actors_skipped,
                    },
                }
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

    # NEW-H1 fix: CRYPTO-COVERAGE-REPAIR-001's B2 skip-when-final option makes
    # `len(snaps)`/distinct-snapshot-addresses an unreliable measure of how
    # many tokens a pass actually considered — a run using skip mode appends
    # NO snapshot/actor row for a token whose outcome was already final, which
    # visibly (and misleadingly) shrinks/reshapes coverage_mix/risk_mix/
    # tokens_observed for the SAME underlying tokens versus a non-skip run.
    # `run.tokens_considered` is recorded independently of whether a snapshot
    # was written, so it is the honest count. Skip totals and which runs used
    # skip mode are surfaced explicitly so a reader is never left guessing
    # whether "N snapshots" means "every considered token" or "only the
    # non-final ones".
    skip_mode_runs = sum(
        1 for r in runs
        if (r.config or {}).get("write_classification", {}).get(
            "skip_redundant_when_final"
        )
    )
    snapshots_skipped_redundant = sum(
        (r.config or {}).get("write_classification", {}).get(
            "snapshots_skipped_redundant", 0
        ) or 0
        for r in runs
    )
    actor_observations_skipped_redundant = sum(
        (r.config or {}).get("write_classification", {}).get(
            "actor_observations_skipped_redundant", 0
        ) or 0
        for r in runs
    )

    return {
        "note": TAPE_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "tape_runs": len(runs),
        "tokens_observed": sum(r.tokens_considered for r in runs),
        "birth_events_in_window": len(births),
        "snapshots_recorded": len(snaps),
        "actor_observations_recorded": len(actor_rows),
        "outcomes_computed": len(outcomes),
        "outcomes_final": sum(1 for o in outcomes if o.final),
        "write_classification": {
            "skip_redundant_when_final_runs": skip_mode_runs,
            "snapshots_skipped_redundant": snapshots_skipped_redundant,
            "actor_observations_skipped_redundant": actor_observations_skipped_redundant,
        },
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

# DB_LOCKED_MAX_ATTEMPTS / DB_LOCKED_RETRY_SECONDS / ABORT_DB_LOCKED now live
# near the top of this module (CRYPTO-COVERAGE-REPAIR-001) so `run_once` and
# `_assemble_pass` can use them as default argument values too.

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


def run_scheduled_reconciliation(
    session: Session,
    recorder: CryptoLifecycleTapeRecorder | None = None,
    *,
    settings=None,
    dry_run: bool = False,
    force: bool = False,
    window_hours: int | None = None,
    limit: int | None = None,
    batch_size: int | None = None,
    max_duration_seconds: float | None = None,
    sleeper=time.sleep,
) -> dict:
    """CRYPTO-COVERAGE-REPAIR-001 — one bounded, provider-free reconciliation
    pass over already-persisted tokens whose survival horizons have matured.

    Why this exists: `record_discovery_run` (the only tape path production
    actually runs) validates that every token was FIRST PERSISTED by the
    originating discovery run, so it sees each token exactly once, at age ~0,
    when no horizon is due. `run_once` — the windowed reconciler that would
    revisit matured tokens — is CLI-only and nothing schedules it. The result
    is that survival horizons never mature at all, and the ticks needed to
    mature them are pruned after `crypto_retention_days`.

    This is a THIN GOVERNED WRAPPER over the existing, already-proven
    `run_once`; it deliberately does not reimplement reconciliation. It adds
    only: a default-OFF gate, explicit bounds, and a structured disabled/ok
    result so a scheduled unit is a clean no-op when the flag is off.

    Guarantees: zero external calls (no provider, no discovery scan, no second
    universe fetch), zero provider-budget impact, no cohort, no arming, no
    observation unit, and no trade-execution capability of any kind. The
    canonical boundary vocabulary for this lane is TAPE_NOTE, which is carried
    in the `note` field of EVERY result this function returns, including the
    disabled and refused ones — see docs/SAFETY_BOUNDARIES.md.

    Honest limits, stated because overclaiming here has bitten this repo before:

    * Labels are idempotent — reconciliation recomputes deterministic labels
      from persisted rows, updating the outcome row in place. The pass is
      MOSTLY, not fully, row-idempotent: `_assemble_pass` APPENDS one
      lifecycle snapshot and one actor observation per token considered on
      every pass UNLESS the token's outcome is already final, in which case
      this path (only this path — B2) skips both as redundant (the window is
      closed; nothing new can be learned). Neither table is covered by
      `retention.py`. Budget roughly `2 x (tokens_considered -
      already_final_tokens)` permanently retained rows per pass.
    * Restart-safe (B3/B6): `run_once` commits in bounded batches
      (`crypto_tape_reconciler_batch_size`, default `RECONCILE_BATCH_SIZE`)
      instead of one transaction for the whole pass, and stops at an internal
      wall-clock deadline (`crypto_tape_reconciler_max_duration_seconds`,
      default `RECONCILE_MAX_DURATION_SECONDS`). A crash or a hit deadline
      after batch N leaves batches 1..N durably committed and nothing
      duplicated; the next pass continues via oldest-first + state-driven
      backlog selection. `status="partial"` (with `stop_reason`) reports this
      explicitly rather than claiming `status="ok"`.
    * Overlap-safe (B4): a non-blocking, per-chain flock guards the whole
      pass, so a second concurrent instance (another scheduled tick, a manual
      tape session, a stray CLI run) never races this one on the same window;
      it gets `status="skipped_overlap"` instead of the IntegrityError this
      milestone measured.
    * Contention-safe (B5): reuses the same DB_LOCKED_* bounded retry ladder
      `run_tape_session` already uses, per batch commit. Exhausting it before
      even the run row can be created yields `status="skipped_contention"`
      (nothing written); exhausting it after some batches already committed
      yields `status="partial"`, `stop_reason="contention"`.
    * The gate is honest about being bypassed: `force` and `dry_run` both run a
      pass while the flag is off, and the result says which.

    Measurement only, never advice."""
    s = settings if settings is not None else get_settings()
    started = _now()
    enabled = bool(getattr(s, "enable_crypto_tape_reconciler", False))
    bypass = "force" if force else ("dry_run" if dry_run else None)
    if not (enabled or force or dry_run):
        return {
            "status": "disabled",
            "note": TAPE_NOTE,
            "mode": "scheduled_reconciliation",
            "external_calls": 0,
            "tokens_considered": 0,
            "outcomes_updated": 0,
            "flag": "enable_crypto_tape_reconciler",
            "gate_bypassed": None,
            "duration_ms": 0,
        }

    hours = window_hours if window_hours is not None else int(
        getattr(s, "crypto_tape_reconciler_window_hours", 48)
    )
    cap = limit if limit is not None else int(
        getattr(s, "crypto_tape_reconciler_limit", 2000)
    )

    def _refused(status: str, error: str) -> dict:
        return {
            "status": status,
            "note": TAPE_NOTE,
            "mode": "scheduled_reconciliation",
            "external_calls": 0,
            "tokens_considered": 0,
            "outcomes_updated": 0,
            "gate_bypassed": bypass,
            "error": error,
            "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
        }

    if cap < 1:
        # SQLite treats LIMIT -1 as "no limit", so an unvalidated cap is an
        # unbounded pass wearing a bound's clothing.
        return _refused("invalid_limit", f"limit {cap} must be >= 1")

    # The window must outlast the longest horizon's closing edge PLUS one
    # scheduling interval, or a token can mature and fall out of the window
    # between two passes without ever being reconciled.
    closing_edge_hours = int(max(m for _, m in HORIZONS) * (1 + HORIZON_TOLERANCE) / 60)
    required_hours = closing_edge_hours + RECONCILER_CADENCE_HOURS
    if hours < required_hours:
        return _refused(
            "invalid_window",
            f"window {hours}h is shorter than the longest horizon closing edge "
            f"{closing_edge_hours}h plus one {RECONCILER_CADENCE_HOURS}h "
            f"scheduling interval; matured outcomes would be missed",
        )

    if not dry_run and _reconciliation_should_abort(session):
        return _refused(
            "marketops_degraded",
            "latest MarketOps run errored; not adding write pressure",
        )

    rec = recorder or CryptoLifecycleTapeRecorder(CryptoTapeConfig.from_settings(s))
    batch = batch_size if batch_size is not None else int(
        getattr(s, "crypto_tape_reconciler_batch_size", RECONCILE_BATCH_SIZE)
    )
    deadline_seconds = max_duration_seconds if max_duration_seconds is not None else float(
        getattr(
            s, "crypto_tape_reconciler_max_duration_seconds",
            RECONCILE_MAX_DURATION_SECONDS,
        )
    )
    try:
        summary = rec.run_once(
            session, limit=cap, hours=hours, dry_run=dry_run,
            oldest_first=True,
            include_backlog=True,
            # B2 fix: state-driven selection — already-final tokens are
            # dropped from the query so a deadline-stopped pass advances on
            # its next invocation, and the wall-clock budget is never spent
            # re-walking tokens with nothing left to learn.
            exclude_final=True,
            run_config_extra={
                "mode": "scheduled_reconciliation",
                "forced": bool(force),
            },
            # B2: once a token's outcome is final, this path (only this path)
            # skips the now-redundant snapshot/actor rows. The manual path
            # (run_tape_session/CLI) never sets this, so its behaviour is
            # unchanged.
            skip_redundant_when_final=True,
            # B3: bounded commits instead of one transaction for the pass.
            batch_size=batch,
            # B6: internal wall-clock deadline; the rest becomes backlog.
            max_duration_seconds=deadline_seconds,
            sleeper=sleeper,
        )
    except Exception as exc:
        # A poisoned transaction must not leak into the caller's session, and a
        # locked database is an expected operational condition on this host,
        # not a crash — the manual path already treats it that way.
        try:
            session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        if _is_db_locked(exc):
            return _refused("db_locked", "database is locked; pass abandoned")
        raise

    # `run_once` may already have returned a terminal, non-"ok" status
    # (skipped_overlap / skipped_contention / partial — B3/B4/B5/B6). Those
    # must NOT be silently overwritten with "ok"/"dry_run"; that is exactly
    # the "a unit that reconciles nothing must never look healthy" failure
    # class this milestone exists to remove.
    terminal_statuses = {
        STATUS_SKIPPED_OVERLAP, STATUS_SKIPPED_CONTENTION, STATUS_PARTIAL,
        STATUS_DRY_RUN_PARTIAL,
    }
    pass_status = summary.get("status")
    resolved_status = (
        pass_status if pass_status in terminal_statuses
        else ("dry_run" if dry_run else "ok")
    )
    summary.update({
        "status": resolved_status,
        "mode": "scheduled_reconciliation",
        "external_calls": 0,
        "window_hours": hours,
        "selection_limit": cap,
        "batch_size": batch,
        "max_duration_seconds": deadline_seconds,
        "gate_bypassed": bypass,
        "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
    })
    if summary.get("truncated"):
        # Loud, not silent: this is the exact failure this milestone exists to
        # remove, so it must be visible in the result AND in the log. A
        # deadline/contention stop (status already partial/skipped_*) is
        # reported through `stop_reason`/`error` instead of being relabelled
        # "truncated", which is specifically the SELECTION-limit case.
        if resolved_status not in terminal_statuses:
            summary["status"] = "truncated"
        summary["error"] = summary.get("error") or (
            f"window holds {summary['universe_size']} tokens plus "
            f"{summary['backlog_size']} aged-out unreconciled, but the limit is "
            f"{cap}; {summary['tokens_omitted']} were not reconciled. Raise "
            f"--limit (or crypto_tape_reconciler_limit) to cover the window."
        )
        logger.warning("crypto reconciliation truncated: %s", summary["error"])
    if resolved_status in (
        STATUS_SKIPPED_OVERLAP, STATUS_SKIPPED_CONTENTION, STATUS_PARTIAL,
        STATUS_DRY_RUN_PARTIAL,
    ):
        logger.warning(
            "crypto reconciliation %s: %s", resolved_status, summary.get("error")
        )
    return summary


def _reconciliation_should_abort(session: Session) -> bool:
    """Do not add write pressure while MarketOps is already unhealthy. Mirrors
    the manual session path's abort, which the scheduled path previously
    lacked — the scheduled path must not be less careful than the manual one."""
    return _marketops_degraded(session)


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
