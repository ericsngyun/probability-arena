"""SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001 — the frozen X source universe.

Loads and validates the concrete 18-source universe frozen under the
preregistration. The file is data; this module is the contract that keeps the
data honest.

Four invariants, each of which has failed somewhere in this repo before and so
is checked rather than trusted:

1. **Class quotas are exact.** The preregistration fixes 4/3/3/4/2/2 across six
   classes. A universe that drifts to 5/2/... is a different experiment.
2. **Selection criteria are absent.** No rationale may cite profitability,
   returns, followers, engagement, reputation or alpha. The rationale is the
   only place a forbidden criterion could hide, because it is the only free
   text a human writes.
3. **CONTROL never appears here.** This file is `NATURAL_LIVE` only. Controls
   are injected, carry their own population, and are structurally incapable of
   entering the natural universe -- so a control can never be pooled into a
   natural count by an ordering accident.
4. **An unresolved handle cannot be activated.** `from:someHandle` against a
   misspelled handle matches nothing and looks exactly like a quiet source.
   Handle resolution needs the network, so this module refuses activation
   until a resolution has been supplied from outside.

CONTAINS NO SIGNAL. A rationale is a statement about protocol, access, or
message shape. It is not a score, a weight, or a priority, and nothing here
ranks one source above another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "CLASS_QUOTAS",
    "FORBIDDEN_CRITERIA",
    "FROZEN_UNIVERSE_PATH",
    "Population",
    "XSourceRule",
    "FrozenXUniverse",
    "UniverseContractError",
    "HandleUnresolvedError",
    "load_frozen_universe",
]

#: Exactly as fixed in the preregistration's class table. Changing a quota is
#: a milestone decision, not an edit: the six classes exist to exercise six
#: different code paths, and reweighting them silently changes which paths the
#: qualification actually tests.
CLASS_QUOTAS: dict[str, int] = {
    "official_protocol_publishing_addresses": 4,
    "launchpad_ecosystem_announcements": 3,
    "exchange_listing_announcements": 3,
    "community_aggregator_high_volume": 4,
    "ticker_only_no_address": 2,
    "impersonation_candidate_surface": 2,
}

#: The two classes that exist to PRODUCE refusals. A qualification in which
#: nothing is refused has not tested the refusal paths, so an empty result from
#: either of these is a finding rather than a success.
REFUSAL_GENERATING_CLASSES = frozenset({
    "ticker_only_no_address", "impersonation_candidate_surface"})

#: Substrings that may not appear in any rationale. Selection is on protocol,
#: access, authority and message shape; a universe chosen on past performance
#: measures our hindsight rather than the pipeline.
FORBIDDEN_CRITERIA = (
    "profit", "return", "follower", "engagement", "reputation", "alpha",
    "performance", "outperform", "winning", "best call", "track record",
)

FROZEN_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "experiments" /
    "SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001-SOURCE-UNIVERSE.frozen.json")


class UniverseContractError(Exception):
    """The frozen universe violates the preregistration."""


class HandleUnresolvedError(Exception):
    """Activation attempted while a handle is still unverified."""


class Population(str, Enum):
    """Structurally separate populations. Never pooled, never summed."""

    NATURAL_LIVE = "NATURAL_LIVE"
    CONTROL = "CONTROL"


@dataclass(frozen=True)
class XSourceRule:
    rule_id: str
    kind: str
    source_class: str
    selector: str
    rationale: str
    active_from: str
    handle: str | None
    handle_resolved: bool
    tags: tuple[str, ...]

    @property
    def is_shape_rule(self) -> bool:
        """A rule defined by message shape rather than by identity.

        Classes 5 and 6 are shape rules ON PURPOSE. Naming specific accounts
        as ticker-only or as impersonators would assert unverifiable facts
        about real entities, and for class 6 it would be an accusation this
        module has no basis to make. The authority resolver decides
        IMPERSONATOR at run time from mutual attestation; the rule only
        surfaces the candidate.
        """
        return self.handle is None

    @property
    def is_refusal_generating(self) -> bool:
        return self.source_class in REFUSAL_GENERATING_CLASSES


@dataclass(frozen=True)
class FrozenXUniverse:
    frozen_at_utc: str
    population: Population
    rules: tuple[XSourceRule, ...]

    def __len__(self) -> int:
        return len(self.rules)

    @property
    def unresolved_handles(self) -> tuple[str, ...]:
        return tuple(r.handle for r in self.rules
                     if r.handle is not None and not r.handle_resolved)

    def assert_activatable(self) -> None:
        """Refuse to activate a universe that would silently match nothing."""
        pending = self.unresolved_handles
        if pending:
            raise HandleUnresolvedError(
                f"{len(pending)} handle(s) unresolved: {sorted(pending)}. "
                "A `from:` rule against a wrong handle matches nothing and is "
                "indistinguishable from a quiet source, so activation is "
                "refused until each handle resolves to a platform user id.")

    def with_resolved_handles(
            self, resolved: Mapping[str, str]) -> "FrozenXUniverse":
        """Stamp handles a caller has resolved OUT OF BAND.

        Resolution needs the network, which this module does not have and must
        not acquire. The caller performs it and passes the result in.
        """
        out = []
        for r in self.rules:
            if r.handle is not None and r.handle in resolved:
                out.append(XSourceRule(**{**r.__dict__,
                                          "handle_resolved": True}))
            else:
                out.append(r)
        return FrozenXUniverse(frozen_at_utc=self.frozen_at_utc,
                               population=self.population,
                               rules=tuple(out))


def _validate(payload: dict, rules: Sequence[XSourceRule]) -> None:
    if payload.get("population") != Population.NATURAL_LIVE.value:
        raise UniverseContractError(
            f"the frozen universe must be {Population.NATURAL_LIVE.value}; "
            f"CONTROL artifacts are injected and never enter it")

    counts: dict[str, int] = {}
    for r in rules:
        counts[r.source_class] = counts.get(r.source_class, 0) + 1
    if counts != CLASS_QUOTAS:
        raise UniverseContractError(
            f"class quotas drifted: {counts} != {CLASS_QUOTAS}")

    ids = [r.rule_id for r in rules]
    if len(set(ids)) != len(ids):
        raise UniverseContractError("rule ids must be unique; a reused id "
                                    "merges two hypotheses into one number")

    for r in rules:
        low = r.rationale.lower()
        for bad in FORBIDDEN_CRITERIA:
            if bad in low:
                raise UniverseContractError(
                    f"{r.rule_id}: rationale cites the forbidden selection "
                    f"criterion {bad!r}. Selection is on protocol, access, "
                    f"authority and message shape only.")
        if len(r.rationale.strip()) < 40:
            raise UniverseContractError(
                f"{r.rule_id}: rationale must state which path this source "
                "exercises; an unexplained source cannot be retired")

    for r in rules:
        if r.is_refusal_generating and not r.is_shape_rule:
            raise UniverseContractError(
                f"{r.rule_id}: a refusal-generating class must be a shape "
                "rule. Naming an account as ticker-only or as an "
                "impersonator asserts an unverifiable fact about a real "
                "entity; the authority resolver makes that call at run time.")


def load_frozen_universe(path: Path | None = None) -> FrozenXUniverse:
    payload = json.loads((path or FROZEN_UNIVERSE_PATH).read_text())
    rules = tuple(
        XSourceRule(
            rule_id=str(r["rule_id"]), kind=str(r["kind"]),
            source_class=str(r["class"]), selector=str(r["selector"]),
            rationale=str(r["rationale"]), active_from=str(r["active_from"]),
            handle=r.get("handle"), handle_resolved=bool(r["handle_resolved"]),
            tags=tuple(r.get("tags") or ()))
        for r in payload["rules"])
    _validate(payload, rules)
    return FrozenXUniverse(frozen_at_utc=str(payload["frozen_at_utc"]),
                           population=Population(payload["population"]),
                           rules=rules)
