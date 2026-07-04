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


# --- CRYPTO-002: real provider adapters (read-only lookups, keys optional) ---
# Both adapters send GET requests only. API keys travel as request headers and
# are never logged or printed. Every failure path (missing auth, 429, HTTP
# errors, schema drift, empty results) returns None so the risk engine falls
# back to heuristics instead of failing a scan.


def _pct(value) -> float | None:
    """Normalize a provider ratio/percent-ish value to a 0-100 percent."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number * 100, 4) if 0 <= number <= 1 else round(number, 4)


def _truthy_flag(value) -> bool:
    """Provider booleans arrive as bool, "1"/"0", or {"status": "1"}."""
    if isinstance(value, dict):
        value = value.get("status")
    if isinstance(value, str):
        return value.strip() in ("1", "true", "True", "yes")
    return bool(value)


class GoPlusSolanaRiskAdapter:
    """Read-only client for the GoPlus Solana Token Security API shape.
    Returns None on any failure; parses defensively against schema drift."""

    name = "goplus"
    API_BASE = "https://api.gopluslabs.io/api/v1/solana/token_security"

    def __init__(self, api_key: str = "", timeout: float = 10.0):
        self._api_key = api_key  # optional; header-only, never logged
        self.timeout = timeout

    async def assess(self, token_address: str) -> RiskAssessment | None:
        import httpx

        headers = {"Authorization": self._api_key} if self._api_key else {}
        url = f"{self.API_BASE}?contract_addresses={token_address}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers)
                if response.status_code == 429:
                    logger.warning("GoPlus rate limit hit for %s", token_address)
                    return None
                response.raise_for_status()
                payload = response.json()
        except (Exception,) as exc:  # httpx errors, JSON errors — never raise
            logger.warning("GoPlus fetch failed for %s: %s", token_address, type(exc).__name__)
            return None
        return self.parse(token_address, payload)

    def parse(self, token_address: str, payload) -> RiskAssessment | None:
        if not isinstance(payload, dict):
            return None
        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        entry = result.get(token_address) or next(iter(result.values()), None)
        if not isinstance(entry, dict):
            return None

        flags: dict = {}
        top10 = _pct(entry.get("top_10_holder_rate") or entry.get("top10_holder_percent"))
        if top10 is not None:
            flags["top10_holder_pct"] = top10
        creator = _pct(entry.get("creator_percent") or entry.get("creator_balance_rate"))
        if creator is not None:
            flags["insider_pct"] = creator
        if "mintable" in entry:
            flags["mint_authority_enabled"] = _truthy_flag(entry.get("mintable"))
        if "freezable" in entry:
            flags["freeze_authority_enabled"] = _truthy_flag(entry.get("freezable"))
        for key in ("is_honeypot", "honeypot"):
            if key in entry:
                flags["honeypot"] = _truthy_flag(entry.get(key))
                break
        if "rug_pull" in entry or "is_rug_pull" in entry:
            flags["rug_risk"] = _truthy_flag(entry.get("rug_pull") or entry.get("is_rug_pull"))
        holder_count = entry.get("holder_count")
        try:
            flags["holder_count"] = int(str(holder_count).replace(",", ""))
        except (TypeError, ValueError):
            pass

        if not flags:
            return None  # schema drift: nothing recognizable
        return RiskAssessment(
            provider=self.name,
            token_address=token_address,
            risk_score=None,  # GoPlus exposes facts, not a single score
            risk_level=None,
            flags=flags,
            raw=entry,
        )


class SolanaTrackerRiskAdapter:
    """Read-only client for the SolanaTracker token risk shape.
    Returns None on any failure; parses defensively."""

    name = "solana-tracker"
    API_BASE = "https://data.solanatracker.io/tokens"

    def __init__(self, api_key: str = "", timeout: float = 10.0):
        self._api_key = api_key  # optional; header-only, never logged
        self.timeout = timeout

    async def assess(self, token_address: str) -> RiskAssessment | None:
        import httpx

        headers = {"x-api-key": self._api_key} if self._api_key else {}
        url = f"{self.API_BASE}/{token_address}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers)
                if response.status_code == 429:
                    logger.warning("SolanaTracker rate limit hit for %s", token_address)
                    return None
                response.raise_for_status()
                payload = response.json()
        except (Exception,) as exc:
            logger.warning(
                "SolanaTracker fetch failed for %s: %s", token_address, type(exc).__name__
            )
            return None
        return self.parse(token_address, payload)

    def parse(self, token_address: str, payload) -> RiskAssessment | None:
        if not isinstance(payload, dict):
            return None
        risk = payload.get("risk")
        if not isinstance(risk, dict):
            return None

        flags: dict = {}
        names = []
        for item in risk.get("risks") or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        if names:
            flags["provider_risk_names"] = names
        lowered = {name.lower() for name in names}
        if risk.get("rugged") or any("rug" in name for name in lowered):
            flags["rug_risk"] = True
        if any("honeypot" in name for name in lowered):
            flags["honeypot"] = True
        if any("mint" in name for name in lowered):
            flags["mint_authority_enabled"] = True
        if any("freeze" in name for name in lowered):
            flags["freeze_authority_enabled"] = True
        for key, flag in (
            ("snipers", "sniper_pct"),
            ("insiders", "insider_pct"),
            ("bundlers", "bundler_pct"),
            ("top10", "top10_holder_pct"),
        ):
            value = _pct((risk.get(key) or {}).get("percentage")) if isinstance(
                risk.get(key), dict
            ) else _pct(risk.get(key))
            if value is not None:
                flags[flag] = value

        score = None
        try:
            # SolanaTracker score: 0 (clean) .. 10 (worst) -> normalize 0..1
            score = round(min(max(float(risk.get("score")), 0.0), 10.0) / 10.0, 4)
        except (TypeError, ValueError):
            pass

        if not flags and score is None:
            return None
        return RiskAssessment(
            provider=self.name,
            token_address=token_address,
            risk_score=score,
            risk_level=None,
            flags=flags,
            raw=risk,
        )
