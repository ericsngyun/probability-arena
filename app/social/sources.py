"""SOCIAL-TAPE-001 — the source universe, as configuration and never as code.

The design target is **100–300 named sources and rules whose individual value
is being measured**. It is explicitly not "ingest Crypto Twitter":

  * an unnamed firehose has no denominator, so no rule's contribution can ever
    be attributed, retired, or defended;
  * per-read pricing makes an unnamed firehose an unbounded bill;
  * and a universe that changes silently makes every longitudinal comparison
    across it invalid.

So a rule is a configuration record with an id, a stated reason for existing,
and an activation date. The live list in this repo is **empty**, on purpose.
:func:`load_source_universe` refuses an empty universe at load time rather than
starting a collector that would match nothing and look healthy doing it.

A documented example set lives in ``docs/social/source-universe.example.json``.
It is an EXAMPLE. Copying it does not constitute configuring a universe.

CONTAINS NO SIGNAL. A rule's ``rationale`` is a hypothesis about where
information appears; it is not a score, a weight, or a priority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.social.artifact import Platform

__all__ = [
    "RuleKind",
    "SourceRule",
    "SourceUniverse",
    "SourceUniverseError",
    "EMPTY_SOURCE_UNIVERSE",
    "load_source_universe",
    "EXAMPLE_RULE_KINDS_DOC",
]


class SourceUniverseError(Exception):
    """The configured universe is absent, malformed, or ambiguous."""


class RuleKind(str, Enum):
    """What kind of thing a rule names.

    The kind is recorded on every artifact's ``matching_rule`` lineage so that
    "which class of source produced the early items" is answerable later
    without re-deriving it from rule ids.
    """

    #: A specific account we follow in full (exchange listing accounts,
    #: launchpad accounts, ecosystem accounts, historically-early callers).
    ACCOUNT = "ACCOUNT"
    #: A named Telegram/Discord channel.
    CHANNEL = "CHANNEL"
    #: A textual pattern — listing/migration keywords, exploit/rug keywords.
    KEYWORD = "KEYWORD"
    #: A structural pattern such as a contract-address shape.
    ADDRESS_PATTERN = "ADDRESS_PATTERN"


#: Rule ids are stable, human-legible, and never reused. Reusing an id silently
#: merges two different hypotheses into one measurement.
_RULE_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


@dataclass(frozen=True)
class SourceRule:
    """One named, individually-measurable reason to collect something."""

    rule_id: str
    platform: Platform
    kind: RuleKind
    #: The platform-native selector: an account handle/id, a channel id, or a
    #: filtered-stream query fragment. Stored verbatim.
    selector: str
    #: WHY this rule exists, in one sentence. Required. A rule whose reason
    #: nobody can state is a rule nobody can retire.
    rationale: str
    #: When it entered the universe. Every longitudinal comparison must be
    #: conditioned on this, because a rule added mid-window changes the
    #: denominator.
    active_from: str
    #: When it left, if it has. Rules are retired, never deleted, so historical
    #: tape stays interpretable.
    active_until: str | None = None
    #: Free-form operator labels. Carries no behaviour.
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _RULE_ID_RE.match(self.rule_id):
            raise SourceUniverseError(
                f"rule_id {self.rule_id!r} must be lowercase dotted/dashed "
                "ascii; ids appear in every record and must stay stable"
            )
        if not self.selector.strip():
            raise SourceUniverseError(f"{self.rule_id}: selector is empty")
        if len(self.rationale.strip()) < 12:
            raise SourceUniverseError(
                f"{self.rule_id}: rationale must state why this rule exists; "
                "an unexplained rule cannot be evaluated or retired"
            )
        if not self.active_from.strip():
            raise SourceUniverseError(
                f"{self.rule_id}: active_from is required; without it the "
                "rule's contribution has no denominator"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "platform": self.platform.value,
            "kind": self.kind.value,
            "selector": self.selector,
            "rationale": self.rationale,
            "active_from": self.active_from,
            "active_until": self.active_until,
            "tags": list(self.tags),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SourceRule":
        try:
            return cls(
                rule_id=str(payload["rule_id"]),
                platform=Platform(payload["platform"]),
                kind=RuleKind(payload["kind"]),
                selector=str(payload["selector"]),
                rationale=str(payload["rationale"]),
                active_from=str(payload["active_from"]),
                active_until=payload.get("active_until"),
                tags=tuple(payload.get("tags") or ()),
            )
        except KeyError as exc:
            raise SourceUniverseError(f"rule is missing field {exc}") from exc
        except ValueError as exc:
            raise SourceUniverseError(f"rule has an invalid value: {exc}") from exc


@dataclass(frozen=True)
class SourceUniverse:
    """The complete, named set of things we collect. Immutable once loaded."""

    universe_id: str
    rules: tuple[SourceRule, ...] = ()
    #: Advisory upper bound. Exceeding it is a refusal, not a warning: the
    #: whole point of the design is that the universe stays enumerable and its
    #: cost stays predictable.
    max_rules: int = 300

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.rule_id in seen:
                raise SourceUniverseError(
                    f"duplicate rule_id {rule.rule_id!r}: two hypotheses "
                    "sharing an id are one unmeasurable hypothesis"
                )
            seen.add(rule.rule_id)
        if len(self.rules) > self.max_rules:
            raise SourceUniverseError(
                f"{len(self.rules)} rules exceeds max_rules={self.max_rules}; "
                "the universe must stay enumerable and individually measurable"
            )

    def __len__(self) -> int:
        return len(self.rules)

    @property
    def is_empty(self) -> bool:
        return not self.rules

    def by_id(self, rule_id: str) -> SourceRule:
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise SourceUniverseError(f"no rule {rule_id!r} in universe")

    def for_platform(self, platform: Platform) -> tuple[SourceRule, ...]:
        return tuple(r for r in self.rules if r.platform is platform)

    def digest_fields(self) -> dict[str, Any]:
        """What gets written into a tape manifest to pin the universe."""

        return {
            "universe_id": self.universe_id,
            "rule_count": len(self.rules),
            "rule_ids": sorted(r.rule_id for r in self.rules),
        }


#: The live universe shipped in this repository. Deliberately EMPTY.
#: Configuring it is one of the three preconditions for activation.
EMPTY_SOURCE_UNIVERSE = SourceUniverse(universe_id="unconfigured", rules=())


def load_source_universe(
    path: str | Path,
    *,
    allow_empty: bool = False,
) -> SourceUniverse:
    """Load a universe from JSON, refusing an empty one by default.

    ``allow_empty`` exists for tests and inspection tooling only. A collector
    must never pass it: a collector with no rules connects, matches nothing,
    reports zero items, and looks exactly like a healthy quiet market.
    """

    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceUniverseError(
            f"source universe at {p} could not be read: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SourceUniverseError(
            f"source universe at {p} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, Mapping):
        raise SourceUniverseError("source universe file must be a JSON object")

    raw_rules: Sequence[Any] = payload.get("rules") or []
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
        raise SourceUniverseError("`rules` must be a list")

    universe = SourceUniverse(
        universe_id=str(payload.get("universe_id") or p.stem),
        rules=tuple(SourceRule.from_json(r) for r in raw_rules),
        max_rules=int(payload.get("max_rules", 300)),
    )
    if universe.is_empty and not allow_empty:
        raise SourceUniverseError(
            f"source universe at {p} names no rules; a collector with an "
            "empty universe cannot be distinguished from a working one, so "
            "starting is refused"
        )
    return universe


#: Documentation of the example rule classes. Prose only — no live selectors.
EXAMPLE_RULE_KINDS_DOC = """\
Example rule classes for a Solana-oriented universe. See
docs/social/source-universe.example.json for worked records.

  exchange_listing      ACCOUNT   venue announcement accounts
  launchpad             ACCOUNT   launch platform accounts
  ecosystem             ACCOUNT   protocol / infrastructure accounts
  early_caller          ACCOUNT   accounts historically early to named items;
                                  named individually so each is measurable and
                                  each can be retired on its own evidence
  address_pattern       ADDRESS_PATTERN   base58 contract-address shapes
  listing_keyword       KEYWORD   listing / migration vocabulary
  incident_keyword      KEYWORD   exploit / rug / halt vocabulary
"""
