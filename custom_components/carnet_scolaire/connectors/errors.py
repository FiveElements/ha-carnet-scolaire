from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import ChallengeKind


class ConnectorError(Exception):
    """Base class for connector failures above the transport details."""


class ConnectorTransportError(ConnectorError):
    """The remote service could not be reached or did not answer in time."""


class ConnectorSessionExpiredError(ConnectorError):
    """The session the connector held is no longer accepted, and was dropped.

    Distinct from :class:`ConnectorCredentialsError` because the credentials
    are still correct, and distinct from :class:`ConnectorUndecodableError`
    because nothing is wrong with what came back. It is the ordinary end of a
    session: the raiser forgets its tokens, so the next attempt logs in again
    and succeeds.

    It has its own class for one reason, and the reason is what the user sees.
    Raised as a bare ``ConnectorError`` it fell into the set-up arm that opens
    the ``account_unreadable`` repair -- a card asking the user to file a bug
    report, never closed again by any code path, for a condition that repaired
    itself eighty seconds later on the retry.
    """


class ConnectorCredentialsError(ConnectorError):
    """The connector could not authenticate with the provided credentials."""


class ConnectorChallengeRequired(ConnectorError):  # noqa: N818 -- spec name
    """The connector requires a human challenge response before continuing."""

    __slots__ = ("kind", "propositions", "question")

    def __init__(
        self,
        kind: ChallengeKind,
        *,
        question: str | None = None,
        propositions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"connector challenge required: {kind}")
        self.kind = kind
        self.question = question
        self.propositions = propositions


class ConnectorUndecodableError(ConnectorError):
    """The remote payload could not be decoded into the declared contract."""


class ConnectorChildMissingError(ConnectorError):
    """The requested child is unknown to this connector."""


class ConnectorUnsupportedError(ConnectorError):
    """The caller asked the connector for something outside its capabilities."""
