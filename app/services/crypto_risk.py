"""Optional crypto token risk providers (CRYPTO-001).

A CryptoRiskProvider returns a RiskAssessment for a token address —
GoPlus/RugCheck/SolanaTracker-style fields (holder concentration, mint/freeze
authority, honeypot/rug heuristics). CRYPTO-001 ships only the deterministic
MockCryptoRiskProvider (tests, dry runs); real providers are a later
milestone and would still be read-only lookups.

Assessment data feeds *informational* risk signals (holder_risk, rug_risk,
suspicious_supply_control). Nothing here scores trades, sizes positions, or
recommends action — see docs/SAFETY_BOUNDARIES.md.
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskAssessment:
    provider: str
    token_address: str
    risk_score: float | None = None  # 0 (clean) .. 1 (worst)
    risk_level: str | None = None  # low|medium|high|critical
    flags: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)


class CryptoRiskProvider(Protocol):
    """Read-only risk lookup. Implementations return None on any failure."""

    name: str

    async def assess(self, token_address: str) -> RiskAssessment | None: ...


class MockCryptoRiskProvider:
    """Deterministic canned assessments keyed by token address. Tokens not
    in the canned map get a clean low-risk read."""

    name = "mock"

    def __init__(self, assessments: dict[str, RiskAssessment] | None = None):
        self.assessments = assessments or {}
        self.assessed: list[str] = []

    async def assess(self, token_address: str) -> RiskAssessment | None:
        self.assessed.append(token_address)
        canned = self.assessments.get(token_address)
        if canned is not None:
            return canned
        return RiskAssessment(
            provider=self.name,
            token_address=token_address,
            risk_score=0.1,
            risk_level="low",
            flags={},
            raw={"mock": True},
        )


def get_risk_provider(settings: Settings | None = None) -> CryptoRiskProvider | None:
    """Provider selected by config; None when the flag is off or the provider
    name is unknown (risk signals then stay inactive — honest absence)."""
    settings = settings or get_settings()
    if not settings.enable_crypto_risk_provider:
        return None
    provider = settings.crypto_risk_provider.strip().lower()
    if provider == "mock":
        return MockCryptoRiskProvider()
    logger.warning("Unknown CRYPTO_RISK_PROVIDER %r; risk signals stay inactive", provider)
    return None
