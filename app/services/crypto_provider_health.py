"""Crypto provider health + holder-risk coverage reporting (MEME-RISK-003).

Makes provider coverage EXPLICIT rather than silent: which risk providers are
enabled, whether a key is present, which risk dimensions each can supply, which
dimensions have NO active provider (the coverage gaps), and — over recently
persisted assessments — how often each holder-risk dimension actually carries
data. A companion view reports the same coverage for the meme-news attention
lane.

Read-only risk intelligence only. Nothing here scores trades, sizes positions,
recommends action, or touches wallets/keys/swaps/signing/execution. API keys
are reported as present/absent booleans only — never their values.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot

# Holder-risk dimensions and the provider-flag keys that evidence each one.
DIMENSION_FLAG_KEYS: dict[str, tuple[str, ...]] = {
    "top10_holder": ("top10_holder_pct",),
    "sniper": ("sniper_pct",),
    "insider": ("insider_pct",),
    "bundler": ("bundler_pct",),
    "creator": ("creator_pct",),
    "authority": ("mint_authority_enabled", "freeze_authority_enabled"),
    "rug": ("rug_risk",),
    "honeypot": ("honeypot",),
}

# The concentration dimensions this milestone specifically targets.
HOLDER_RISK_DIMENSIONS = ("top10_holder", "sniper", "insider", "bundler", "creator")

# What each provider CAN supply (capability, independent of enablement).
PROVIDER_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "goplus": ("top10_holder", "insider", "authority", "rug", "honeypot"),
    "solana-tracker": ("top10_holder", "sniper", "insider", "bundler", "authority", "rug", "honeypot"),
    "birdeye": ("top10_holder", "creator", "authority"),
    "helius": (),      # reserved — no adapter yet
    "rugcheck": (),    # reserved — no adapter yet
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProviderStatus:
    name: str
    enabled: bool
    key_present: bool
    status: str  # active | disabled | reserved
    dimensions: list[str]


@dataclass
class ProviderHealthReport:
    note: str
    engine_enabled: bool
    engine_mode: str
    providers: list[dict]
    covered_dimensions: dict[str, list[str]]   # dimension -> active providers covering it
    coverage_gaps: list[str]                    # dimensions with NO active provider (explicit)
    observed_coverage: dict[str, dict]          # dimension -> {covered, total, rate} over recent
    provider_use: dict[str, int]
    provider_error_counts: dict[str, int]


class CryptoProviderHealthReportService:
    def build(self, session: Session, recent_limit: int = 500) -> ProviderHealthReport:
        s = get_settings()

        providers: list[ProviderStatus] = []
        for name, enabled_attr, key_attr, reserved in (
            ("goplus", "enable_goplus_risk", "goplus_api_key", False),
            ("solana-tracker", "enable_solana_tracker_risk", "solana_tracker_api_key", False),
            ("birdeye", "enable_birdeye_risk", "birdeye_api_key", False),
            ("helius", "enable_helius", None, True),
            ("rugcheck", "enable_rugcheck_risk", None, True),
        ):
            enabled = bool(getattr(s, enabled_attr, False))
            key_present = bool(getattr(s, key_attr, "")) if key_attr else False
            status = "reserved" if reserved else ("active" if enabled else "disabled")
            providers.append(
                ProviderStatus(
                    name=name, enabled=enabled and not reserved, key_present=key_present,
                    status=status, dimensions=list(PROVIDER_DIMENSIONS.get(name, ())),
                )
            )

        active = [p for p in providers if p.status == "active"]
        covered: dict[str, list[str]] = {}
        for dim in DIMENSION_FLAG_KEYS:
            covering = [p.name for p in active if dim in p.dimensions]
            if covering:
                covered[dim] = covering
        gaps = [dim for dim in HOLDER_RISK_DIMENSIONS if dim not in covered]

        # observed coverage over recent latest-per-token assessments
        rows = session.execute(
            select(CryptoTokenRiskAssessment)
            .order_by(CryptoTokenRiskAssessment.id.desc())
            .limit(recent_limit)
        ).scalars().all()
        latest: dict[str, CryptoTokenRiskAssessment] = {}
        for r in rows:
            latest.setdefault(r.token_address, r)
        total = len(latest)
        observed: dict[str, dict] = {}
        provider_use: dict[str, int] = {}
        provider_errors: dict[str, int] = {}
        for dim in DIMENSION_FLAG_KEYS:
            keys = DIMENSION_FLAG_KEYS[dim]
            covered_n = sum(
                1 for r in latest.values()
                if any(k in (r.flags or {}) for k in keys)
            )
            observed[dim] = {
                "covered": covered_n, "total": total,
                "rate": round(covered_n / total, 4) if total else None,
            }
        for r in latest.values():
            for name in r.provider_names or []:
                provider_use[name] = provider_use.get(name, 0) + 1
            for name in ((r.raw_payload or {}).get("provider_errors") or {}):
                provider_errors[name] = provider_errors.get(name, 0) + 1

        mode = "disabled"
        if s.enable_crypto_risk_engine:
            mode = "provider-backed" if active else "heuristic-only"

        return ProviderHealthReport(
            note=(
                "Read-only provider coverage/health. Risk intelligence only — no "
                "trade/EV/sizing/orders/wallets/execution. Keys reported present/absent "
                "only, never their values. Coverage gaps are stated explicitly."
            ),
            engine_enabled=s.enable_crypto_risk_engine,
            engine_mode=mode,
            providers=[p.__dict__ for p in providers],
            covered_dimensions=covered,
            coverage_gaps=gaps,
            observed_coverage=observed,
            provider_use=provider_use,
            provider_error_counts=provider_errors,
        )


@dataclass
class MemeRiskCoverageReport:
    note: str
    window_hours: int
    tokens: int
    with_provider_data: int
    missing_provider_data: int
    by_dimension: dict[str, dict]  # dimension -> {covered, rate}
    coverage_gaps: list[str]
    provider_use: dict[str, int] = field(default_factory=dict)


class MemeRiskCoverageReportService:
    """Holder/sniper/insider/bundler/creator coverage for the meme-news lane:
    over recent attention snapshots, how many tokens have provider-backed
    holder-risk data (joined to their latest risk assessment)."""

    def build(self, session: Session, hours: int = 24) -> MemeRiskCoverageReport:
        now = _now()
        start = now - timedelta(hours=hours)
        snaps = session.execute(
            select(MemeAttentionSnapshot).where(MemeAttentionSnapshot.observed_at >= start)
        ).scalars().all()
        tokens = {s.token_address for s in snaps}

        # latest risk assessment per token
        assessments: dict[str, CryptoTokenRiskAssessment] = {}
        if tokens:
            rows = session.execute(
                select(CryptoTokenRiskAssessment)
                .where(CryptoTokenRiskAssessment.token_address.in_(tokens))
                .order_by(CryptoTokenRiskAssessment.id.desc())
            ).scalars().all()
            for r in rows:
                assessments.setdefault(r.token_address, r)

        with_data = 0
        provider_use: dict[str, int] = {}
        by_dim: dict[str, dict] = {d: {"covered": 0} for d in HOLDER_RISK_DIMENSIONS}
        for tk in tokens:
            r = assessments.get(tk)
            names = [n for n in ((r.provider_names if r else None) or []) if n not in ("heuristic", "heuristics")]
            if r is not None and names:
                with_data += 1
                for n in names:
                    provider_use[n] = provider_use.get(n, 0) + 1
            flags = (r.flags if r else None) or {}
            for dim in HOLDER_RISK_DIMENSIONS:
                if any(k in flags for k in DIMENSION_FLAG_KEYS[dim]):
                    by_dim[dim]["covered"] += 1

        n = len(tokens)
        for dim in by_dim:
            by_dim[dim]["rate"] = round(by_dim[dim]["covered"] / n, 4) if n else None
        gaps = [d for d in HOLDER_RISK_DIMENSIONS if by_dim[d]["covered"] == 0]

        return MemeRiskCoverageReport(
            note=(
                "Read-only holder-risk coverage for the meme-news lane. Risk "
                "intelligence only — never advice/EV/sizing/orders. A gap means no "
                "provider supplied that dimension for any recent token (explicit absence)."
            ),
            window_hours=hours,
            tokens=n,
            with_provider_data=with_data,
            missing_provider_data=n - with_data,
            by_dimension=by_dim,
            coverage_gaps=gaps,
            provider_use=provider_use,
        )
