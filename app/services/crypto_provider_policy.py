"""CRYPTO-DISCOVERY-PROVIDER-GATE-001: explicit, run-scoped, fail-closed
provider authorization for the crypto discovery + risk-enrichment graph.

Every crypto discovery run must carry an explicit ``ProviderPolicy``. There is
NO permissive default: a real external request issued without an installed
run-context fails closed (``MissingPolicyError``) *before* the network is
touched. Authorization failures (denied / not-planned / unconfirmed-paid /
unknown / missing) are HARD errors (``ProviderPolicyError`` subclasses) that the
adapter/registry broad exception handlers must never swallow. Cap and budget
outcomes are DETERMINISTIC soft skips (``ProviderSkip``) — no request, no error.

This module authorizes read-only research lookups only. It never trades, sizes,
recommends, or moves capital, and it holds no secrets (provider API keys stay in
the adapters, header-only, and never reach a policy, plan, ledger, or log line).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


class Provider(str, Enum):
    """Closed canonical provider identifiers. Any string outside this set is a
    fail-closed ``UnknownProviderError`` — spoofed/mislabeled names cannot slip
    past authorization."""

    DEXSCREENER = "dexscreener"
    GOPLUS = "goplus"
    SOLANA_TRACKER = "solana-tracker"
    BIRDEYE = "birdeye"


PAID_PROVIDERS: frozenset[Provider] = frozenset({Provider.SOLANA_TRACKER, Provider.BIRDEYE})
MANDATORY_PROVIDERS: frozenset[Provider] = frozenset({Provider.DEXSCREENER})


# --- errors -----------------------------------------------------------------
# Hard authorization failures. Distinct from ordinary provider errors so the
# adapters' broad `except Exception: return None` never degrades a policy
# violation into a silent empty result.


class ProviderPolicyError(RuntimeError):
    """Base class for hard provider-authorization failures (never swallowed)."""


class MissingPolicyError(ProviderPolicyError):
    """A real provider request was attempted with no run-scoped policy installed."""


class ProviderDeniedError(ProviderPolicyError):
    """A request was attempted for an explicitly denied provider."""


class ProviderNotPlannedError(ProviderPolicyError):
    """A request was attempted for a provider absent from the run plan."""


class PaidProviderNotConfirmedError(ProviderPolicyError):
    """A paid provider was reached without provider-specific confirmation."""


class UnknownProviderError(ProviderPolicyError):
    """An unknown/spoofed provider identifier reached the guard."""


class MandatoryProviderDeniedError(ProviderPolicyError):
    """A mandatory provider (e.g. DexScreener) was denied — the whole requested
    execution mode is rejected before any scan begins."""


# --- soft skips -------------------------------------------------------------
# Deterministic "do not issue this request" signals. NOT ProviderPolicyError:
# adapters catch these to return None (no request), never a hard failure.


class ProviderSkip(Exception):
    """Base class for deterministic, non-error request skips."""

    reason = "skipped"

    def __init__(self, provider: "Provider"):
        super().__init__(f"{self.reason}:{provider.value}")
        self.provider = provider


class ProviderCapExhausted(ProviderSkip):
    reason = "skipped_cap"


class ProviderBudgetBlocked(ProviderSkip):
    reason = "skipped_budget"


def canonical(name) -> Provider:
    """Map any identifier to the closed ``Provider`` enum; fail closed on unknown."""
    if isinstance(name, Provider):
        return name
    try:
        return Provider(name)
    except ValueError as exc:  # unknown/spoofed identifier
        raise UnknownProviderError(f"unknown provider identifier: {name!r}") from exc


class Authorization(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NOT_PLANNED = "not_planned"
    PAID_UNCONFIRMED = "paid_unconfirmed"


@dataclass(frozen=True)
class ProviderDescriptor:
    """Typed metadata exposed by a constructed provider-capable component. The
    preflight plan and the run's default policy are BOTH derived from these, so
    the plan can never drift from what runtime will actually do."""

    provider: Provider
    role: str
    direct: bool          # direct call path vs reached via the risk engine
    enabled: bool         # actually constructed/enabled in this graph
    paid: bool
    mandatory: bool
    per_token: bool       # call cardinality scope
    max_requests: int | None  # planned upper bound for the run
    config_source: str    # flag name or "mandatory"
    fallback: str
    cap: int | None = None    # enforced per-run request cap (None = uncapped)


@dataclass(frozen=True)
class ProviderPolicy:
    """Immutable, run-scoped authorization. Deny overrides everything (flags,
    allow-list, adapter enablement, fallbacks). Paid providers additionally
    require explicit per-provider confirmation."""

    run_id: str
    allowed: frozenset[Provider]
    denied: frozenset[Provider]
    caps: Mapping[Provider, int | None]
    paid_confirmed: frozenset[Provider]

    def authorization(self, provider) -> Authorization:
        p = canonical(provider)
        if p in self.denied:                       # deny wins over all
            return Authorization.DENIED
        if p not in self.allowed:
            return Authorization.NOT_PLANNED
        if p in PAID_PROVIDERS and p not in self.paid_confirmed:
            return Authorization.PAID_UNCONFIRMED
        return Authorization.ALLOWED

    def cap(self, provider) -> int | None:
        return self.caps.get(canonical(provider))

    def mandatory_denied(self) -> Provider | None:
        """The first mandatory provider that is denied or absent from allow."""
        for p in sorted(MANDATORY_PROVIDERS, key=lambda x: x.value):
            if p in self.denied or p not in self.allowed:
                return p
        return None

    @classmethod
    def compatibility_from_settings(
        cls, settings, run_id: str, *, limit: int | None = None
    ) -> "ProviderPolicy":
        """Behavior-equivalent explicit policy for a legacy/ambient caller
        (MarketOps): allow exactly the currently-enabled providers, CONFIRM the
        currently-enabled paid providers, and copy existing caps. This changes
        no provider set, cap, or output — it only makes authorization explicit."""
        pair_limit = limit or getattr(settings, "crypto_pair_limit", 100)
        allowed: set[Provider] = {Provider.DEXSCREENER}
        engine_on = bool(getattr(settings, "enable_crypto_risk_engine", False))
        if engine_on and getattr(settings, "enable_goplus_risk", False):
            allowed.add(Provider.GOPLUS)
        if engine_on and getattr(settings, "enable_solana_tracker_risk", False):
            allowed.add(Provider.SOLANA_TRACKER)
        if engine_on and getattr(settings, "enable_birdeye_risk", False):
            allowed.add(Provider.BIRDEYE)
        paid_confirmed = frozenset(p for p in allowed if p in PAID_PROVIDERS)
        caps = {
            Provider.DEXSCREENER: 2 + pair_limit,
            Provider.SOLANA_TRACKER: getattr(
                settings, "solana_tracker_per_run_lookup_limit", 25
            ),
        }
        return cls(
            run_id=run_id,
            allowed=frozenset(allowed),
            denied=frozenset(),
            caps=caps,
            paid_confirmed=paid_confirmed,
        )

    @classmethod
    def allow_all_for_tests(cls, run_id: str = "test-run") -> "ProviderPolicy":
        """EXPLICIT permissive policy for tests only. Never constructed by a
        service or adapter — the fail-closed guarantee is that production code
        must build a real policy, and tests opt in visibly."""
        return cls(
            run_id=run_id,
            allowed=frozenset(Provider),
            denied=frozenset(),
            caps={p: None for p in Provider},
            paid_confirmed=frozenset(PAID_PROVIDERS),
        )


_LEDGER_FIELDS = (
    "planned_max",
    "authorized",
    "started",
    "succeeded",
    "failed",
    "blocked_policy",
    "skipped_cap",
    "skipped_budget",
)


@dataclass
class ProviderLedger:
    """True run-scoped request accounting. ``authorized`` (reserved) is distinct
    from ``started`` (HTTP began) and ``succeeded``/``failed`` (outcome).
    Concurrency-safe: reservation is done under ``lock``."""

    planned_max: dict = field(default_factory=dict)
    authorized: dict = field(default_factory=dict)
    started: dict = field(default_factory=dict)
    succeeded: dict = field(default_factory=dict)
    failed: dict = field(default_factory=dict)
    blocked_policy: dict = field(default_factory=dict)
    skipped_cap: dict = field(default_factory=dict)
    skipped_budget: dict = field(default_factory=dict)
    _reserved: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _bump(self, bucket: dict, provider: Provider, n: int = 1) -> None:
        bucket[provider] = bucket.get(provider, 0) + n

    def snapshot(self) -> dict:
        providers = set()
        for name in _LEDGER_FIELDS:
            providers.update(getattr(self, name).keys())
        return {
            p.value: {name: getattr(self, name).get(p, 0) for name in _LEDGER_FIELDS}
            for p in sorted(providers, key=lambda x: x.value)
        }


@dataclass
class ProviderRunContext:
    policy: ProviderPolicy
    ledger: ProviderLedger

    @property
    def run_id(self) -> str:
        return self.policy.run_id


_CURRENT: contextvars.ContextVar[ProviderRunContext | None] = contextvars.ContextVar(
    "crypto_provider_run", default=None
)


def current_context() -> ProviderRunContext | None:
    return _CURRENT.get()


def new_run_id() -> str:
    return uuid.uuid4().hex


@contextlib.contextmanager
def provider_run(policy: ProviderPolicy, planned_max: dict | None = None):
    """Install ``policy`` as the ambient run context for lowest-level guards.
    Copied into ``asyncio.gather`` child tasks automatically."""
    ledger = ProviderLedger(planned_max=dict(planned_max or {}))
    ctx = ProviderRunContext(policy=policy, ledger=ledger)
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def require_context(expected_run_id: str | None = None) -> ProviderRunContext:
    """Return the ambient context or fail closed. Optionally verify the ambient
    policy run_id matches an explicit policy's run_id (context-integrity check)."""
    ctx = _CURRENT.get()
    if ctx is None:
        raise MissingPolicyError("no run-scoped provider policy is installed")
    if expected_run_id is not None and ctx.run_id != expected_run_id:
        raise MissingPolicyError(
            "ambient provider policy run_id does not match the explicit policy"
        )
    return ctx


async def guard_provider_request(provider) -> None:
    """Lowest-level guard, called immediately before an external request.

    Hard-raises a ``ProviderPolicyError`` on any authorization failure (missing
    policy / denied / not-planned / unconfirmed-paid / unknown). On success,
    atomically reserves a cap slot; raises ``ProviderCapExhausted`` (soft) when
    the configured cap is reached. Callers must let ``ProviderPolicyError``
    propagate and may catch ``ProviderSkip`` to return None (no request)."""
    p = canonical(provider)  # UnknownProviderError (hard) on spoofed name
    ctx = _CURRENT.get()
    if ctx is None:
        raise MissingPolicyError(
            f"provider {p.value} request attempted with no run-scoped policy"
        )
    auth = ctx.policy.authorization(p)
    if auth is not Authorization.ALLOWED:
        ctx.ledger._bump(ctx.ledger.blocked_policy, p)
        if auth is Authorization.DENIED:
            raise ProviderDeniedError(f"provider {p.value} is explicitly denied")
        if auth is Authorization.NOT_PLANNED:
            raise ProviderNotPlannedError(f"provider {p.value} is not in the run plan")
        raise PaidProviderNotConfirmedError(
            f"paid provider {p.value} requires --confirm-paid-provider {p.value}"
        )
    cap = ctx.policy.cap(p)
    async with ctx.ledger.lock:
        if cap is not None and ctx.ledger._reserved.get(p, 0) >= cap:
            ctx.ledger._bump(ctx.ledger.skipped_cap, p)
            raise ProviderCapExhausted(p)
        ctx.ledger._bump(ctx.ledger._reserved, p)
        ctx.ledger._bump(ctx.ledger.authorized, p)


def mark_started(provider) -> None:
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.ledger._bump(ctx.ledger.started, canonical(provider))


def mark_succeeded(provider) -> None:
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.ledger._bump(ctx.ledger.succeeded, canonical(provider))


def mark_failed(provider) -> None:
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.ledger._bump(ctx.ledger.failed, canonical(provider))


def record_dispatch_skip(provider, reason: str) -> None:
    """Layer-A accounting when the orchestrator declines to dispatch a provider
    (existing budget guard or per-run cap), recorded as skipped_budget/cap."""
    ctx = _CURRENT.get()
    if ctx is None:
        return
    bucket = ctx.ledger.skipped_budget if reason == "skipped_budget" else ctx.ledger.skipped_cap
    ctx.ledger._bump(bucket, canonical(provider))


def record_not_dispatched(provider, authorization: "Authorization") -> None:
    """Layer-A accounting when an optional provider is not dispatched because
    the policy did not authorize it (graceful fallback path)."""
    ctx = _CURRENT.get()
    if ctx is None:
        return
    if authorization is not Authorization.ALLOWED:
        ctx.ledger._bump(ctx.ledger.blocked_policy, canonical(provider))


def dispatch_decision(provider) -> Authorization:
    """Layer-A authorization lookup against the ambient policy. Returns
    ``ALLOWED`` when no context is installed (legacy/ungoverned dispatch); the
    lowest-level guard still fails closed on the real request."""
    ctx = _CURRENT.get()
    if ctx is None:
        return Authorization.ALLOWED
    return ctx.policy.authorization(canonical(provider))


# --- CLI policy resolution (single source: descriptors, no duplicated list) --


@dataclass(frozen=True)
class CliPolicyResolution:
    descriptors: list
    allowed: frozenset
    denied: frozenset
    confirmed: frozenset
    unresolved_paid: frozenset  # enabled+selected paid providers lacking confirmation
    mandatory_blocked: Provider | None
    policy: ProviderPolicy | None  # None when execution is blocked (fail closed)
    run_id: str

    @property
    def blocked(self) -> bool:
        return self.policy is None


def resolve_cli_policy(
    descriptors,
    *,
    allow: Iterable = (),
    deny: Iterable = (),
    confirm_paid: Iterable = (),
    run_id: str | None = None,
) -> CliPolicyResolution:
    """Derive an execution policy from constructed provider descriptors and the
    operator's flags. Fail closed: a denied mandatory provider or an unconfirmed
    selected paid provider yields ``policy=None`` (the CLI must refuse to run)."""
    run_id = run_id or new_run_id()
    allow_set = {canonical(a) for a in allow}
    deny_set = {canonical(d) for d in deny}
    confirmed = {canonical(c) for c in confirm_paid}
    enabled = {d.provider for d in descriptors if d.enabled}
    caps = {d.provider: d.cap for d in descriptors}

    if allow_set:
        base = allow_set & enabled
        if Provider.DEXSCREENER in enabled:
            base |= {Provider.DEXSCREENER}  # mandatory unless explicitly denied
    else:
        base = set(enabled)
    allowed = base - deny_set

    mandatory_blocked = None
    for m in sorted(MANDATORY_PROVIDERS, key=lambda x: x.value):
        if m in enabled and m not in allowed:
            mandatory_blocked = m
            break

    selected_paid = {p for p in allowed if p in PAID_PROVIDERS}
    unresolved_paid = frozenset(p for p in selected_paid if p not in confirmed)
    paid_confirmed = frozenset(p for p in selected_paid if p in confirmed)

    policy = None
    if mandatory_blocked is None and not unresolved_paid:
        policy = ProviderPolicy(
            run_id=run_id,
            allowed=frozenset(allowed),
            denied=frozenset(deny_set),
            caps=caps,
            paid_confirmed=paid_confirmed,
        )
    return CliPolicyResolution(
        descriptors=list(descriptors),
        allowed=frozenset(allowed),
        denied=frozenset(deny_set),
        confirmed=frozenset(confirmed),
        unresolved_paid=unresolved_paid,
        mandatory_blocked=mandatory_blocked,
        policy=policy,
        run_id=run_id,
    )


def render_provider_plan(command: str, descriptors, resolution: CliPolicyResolution) -> str:
    """Zero-call preflight text derived from the same descriptors runtime uses."""
    lines = [
        f"crypto discovery provider plan — zero-call preflight (no request issued)",
        f"command: {command}  external_calls=0",
        "derived from: constructed DexScreenerAdapter + CryptoRiskProviderRegistry.adapters",
        f"  {'provider':<15}{'kind':<6}{'role':<20}{'enabled':<9}{'cap':<8}"
        f"{'max_req':<9}decision",
    ]
    for d in sorted(descriptors, key=lambda x: (not x.mandatory, x.provider.value)):
        auth = (
            resolution.policy.authorization(d.provider)
            if resolution.policy is not None
            else None
        )
        if d.provider in resolution.denied:
            decision = "DENIED"
        elif d.provider in resolution.unresolved_paid:
            decision = "NEEDS-CONFIRM"
        elif not d.enabled:
            decision = "disabled"
        elif resolution.policy is not None and auth is Authorization.ALLOWED:
            decision = "will_call"
        elif d.provider in resolution.allowed:
            decision = "will_call"
        else:
            decision = "not_planned"
        lines.append(
            f"  {d.provider.value:<15}{'PAID' if d.paid else 'free':<6}{d.role:<20}"
            f"{'yes' if d.enabled else 'no':<9}{str(d.cap) if d.cap is not None else '-':<8}"
            f"{str(d.max_requests) if d.max_requests is not None else '-':<9}{decision}"
        )
    if resolution.mandatory_blocked is not None:
        lines.append(
            f"verdict: BLOCKED — mandatory provider "
            f"{resolution.mandatory_blocked.value} is denied; discovery rejected."
        )
    elif resolution.unresolved_paid:
        names = ", ".join(sorted(p.value for p in resolution.unresolved_paid))
        lines.append(f"verdict: BLOCKED — paid provider(s) not confirmed: {names}")
        for p in sorted(resolution.unresolved_paid, key=lambda x: x.value):
            lines.append(
                f"  to run: add --confirm-paid-provider {p.value}  "
                f"or --deny-provider {p.value}"
            )
    else:
        lines.append("verdict: READY — pass --yes to execute the plan above.")
    lines.append("no provider was contacted.")
    return "\n".join(lines)


def render_ledger(snapshot: dict) -> str:
    """True run-scoped request accounting for a completed governed run."""
    if not snapshot:
        return "provider ledger: (no provider activity)"
    lines = ["provider request ledger (this run):"]
    for provider, counts in snapshot.items():
        lines.append(
            f"  {provider:<15}"
            + " ".join(f"{k}={counts[k]}" for k in _LEDGER_FIELDS)
        )
    return "\n".join(lines)
