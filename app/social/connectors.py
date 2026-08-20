"""SOCIAL-TAPE-001 — authorized connectors for Telegram/Discord: BOUNDARY ONLY.

This module contains **no implementation and opens no connection**. It defines
the interface a Telegram or Discord connector would have to satisfy, and — the
actual point — the authorization boundary it would have to cross first.

Why the boundary is the deliverable
-----------------------------------
X's filtered stream is a public firehose against a commercial contract. A
Telegram group or a Discord server is not. Reading one usually means:

  * joining with an account, under that community's rules;
  * receiving messages from people who did not publish them to the world and
    have no relationship with us;
  * and, on Telegram, potentially operating a user session rather than a bot,
    which is a different legal and ToS posture entirely.

None of that is a technical problem, so none of it is solved by writing a
client. It is solved by recording, before a single byte is read, WHO granted
access, WHAT they granted, WHEN, and under WHICH platform mechanism. That
record is :class:`AuthorizationGrant`, and
:func:`assert_connector_authorized` refuses without one.

There is deliberately no ``TelegramConnector`` and no ``DiscordConnector`` in
this file. Adding one is a separate milestone that must state its
authorization basis and its retention posture.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

from app.social.artifact import Platform
from app.social.transport import StreamFrame

__all__ = [
    "GrantMechanism",
    "AuthorizationGrant",
    "ConnectorAuthorizationError",
    "AuthorizedConnector",
    "assert_connector_authorized",
    "NO_GRANTS",
]


class ConnectorAuthorizationError(RuntimeError):
    """A connector was asked to operate without a recorded grant."""


class GrantMechanism(str, Enum):
    """Under what platform mechanism access was obtained.

    Recorded because the mechanisms are not interchangeable: a public Discord
    bot invite and a personal Telegram user session carry different consent,
    different ToS exposure, and different obligations to the people whose
    messages we would be storing.
    """

    #: A Discord bot invited to a server by an administrator of that server.
    DISCORD_BOT_INVITE = "DISCORD_BOT_INVITE"
    #: A Telegram bot added to a channel/group by an administrator.
    TELEGRAM_BOT_ADMIN = "TELEGRAM_BOT_ADMIN"
    #: A Telegram channel that is publicly broadcast and readable without
    #: joining a private group.
    TELEGRAM_PUBLIC_CHANNEL = "TELEGRAM_PUBLIC_CHANNEL"
    #: A personal user session. NOT a permitted mechanism in this repository
    #: without an explicit, separately accepted decision — it reads as a human
    #: and carries that human's obligations.
    TELEGRAM_USER_SESSION = "TELEGRAM_USER_SESSION"


#: Mechanisms this repository will not accept on the basis of a grant record
#: alone. Listed so the refusal is a data fact rather than a convention.
_REQUIRES_SEPARATE_DECISION = frozenset({GrantMechanism.TELEGRAM_USER_SESSION})


@dataclass(frozen=True)
class AuthorizationGrant:
    """The recorded fact that someone with standing said yes.

    Every field is required. A grant that cannot name who granted it, what
    they granted, and when, is not a grant — it is an assumption.
    """

    grant_id: str
    platform: Platform
    mechanism: GrantMechanism
    #: The channel/server/group this grant covers. One grant, one scope. A
    #: grant covering "everything on Telegram" is refused: it cannot be
    #: revoked partially and cannot be audited at all.
    scope: str
    #: Who granted it, in a form a human could follow up on.
    granted_by: str
    granted_at: str
    #: Where the evidence of the grant lives (ticket, email thread, message).
    evidence_reference: str
    #: When it lapses. Required: an unexpiring grant is one nobody revisits.
    expires_at: str
    #: What we may retain. Recorded here, not in the collector, so that a
    #: retention promise is attached to the consent that permitted it.
    retention_note: str

    def __post_init__(self) -> None:
        required = (
            "grant_id",
            "scope",
            "granted_by",
            "granted_at",
            "evidence_reference",
            "expires_at",
            "retention_note",
        )
        for name in required:
            if not str(getattr(self, name)).strip():
                raise ConnectorAuthorizationError(
                    f"authorization grant requires {name}; an unrecorded "
                    "consent is an assumed one"
                )
        if self.scope.strip() in {"*", "all", "ALL"}:
            raise ConnectorAuthorizationError(
                "a wildcard scope cannot be revoked partially or audited at "
                "all; grants are per-channel"
            )


#: The grants configured in this repository. Deliberately EMPTY.
NO_GRANTS: tuple[AuthorizationGrant, ...] = ()


def assert_connector_authorized(
    platform: Platform,
    scope: str,
    grants: Sequence[AuthorizationGrant] = NO_GRANTS,
) -> AuthorizationGrant:
    """Find the grant covering this scope, or refuse.

    Fails closed in every ambiguous case: no grants configured, no matching
    scope, or a mechanism that requires its own decision.
    """

    for grant in grants:
        if grant.platform is platform and grant.scope == scope:
            if grant.mechanism in _REQUIRES_SEPARATE_DECISION:
                raise ConnectorAuthorizationError(
                    f"{grant.mechanism.value} requires a separately accepted "
                    "decision, not merely a grant record"
                )
            return grant
    raise ConnectorAuthorizationError(
        f"no recorded authorization grant covers {platform.value}:{scope}; "
        "SOCIAL-TAPE-001 configures none and connects to nothing"
    )


@runtime_checkable
class AuthorizedConnector(Protocol):
    """The interface a future Telegram/Discord connector must satisfy.

    NOT IMPLEMENTED HERE. The shape is fixed now so that the authorization
    argument is settled before any client code exists to argue with it.
    """

    @property
    def platform(self) -> Platform:
        """Which platform this connector speaks to."""

    @property
    def grant(self) -> AuthorizationGrant:
        """The grant this connector is operating under.

        A connector holds its grant as state, so that every frame it produces
        is attributable to a specific consent that can be revoked.
        """

    def frames(self) -> AsyncIterator[StreamFrame]:
        """Yield frames in the same shape as `app.social.transport`.

        Same frame type on purpose: the tape must not care which platform a
        record came from, or cross-platform propagation becomes unmeasurable.
        """

    async def aclose(self) -> None:
        """Release the connection."""
