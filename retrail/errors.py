from __future__ import annotations

from collections.abc import Sequence


class RetrailError(Exception):
    """Base class for every error retrail raises deliberately."""


class NotFound(RetrailError):
    pass


class AmbiguousSha(RetrailError):
    """A SHA prefix matched more than one step.

    Resolved the way git resolves it: refuse and ask for a longer prefix.
    """

    def __init__(self, prefix: str, matches: Sequence[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        listed = ", ".join(m[:12] for m in matches[:5])
        more = f" (and {len(matches) - 5} more)" if len(matches) > 5 else ""
        super().__init__(
            f"SHA prefix {prefix!r} is ambiguous - matches {len(matches)} steps: "
            f"{listed}{more}. Use a longer prefix."
        )


class ReplayIntegrityError(RetrailError):
    """The recorded state cannot be replayed faithfully.

    Raised instead of guessing. The entire product rests on the replay being
    exactly what happened, so an unverifiable replay is a hard failure.
    """


class IntegrationError(RetrailError):
    """The user's agent function doesn't meet the integration contract."""
