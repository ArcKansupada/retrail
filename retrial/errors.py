from __future__ import annotations

from collections.abc import Sequence


class RetrialError(Exception):
    """Base class for every error retrial raises deliberately."""


class NotFound(RetrialError):
    pass


class AmbiguousSha(RetrialError):
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


class ReplayIntegrityError(RetrialError):
    """The recorded state cannot be replayed faithfully.

    Raised instead of guessing. The entire product rests on the replay being
    exactly what happened, so an unverifiable replay is a hard failure.
    """


class IntegrationError(RetrialError):
    """The user's agent function doesn't meet the integration contract."""


class ExportFormatError(RetrialError):
    """An export file cannot be read as what it claims to be.

    Always carries the line, because the failures this covers - a malformed
    row, a step whose sha does not match its content, a parent referenced
    before it is defined - are one line in a thousand, and "somewhere in
    trace.jsonl" is not a report anyone can act on.
    """

    def __init__(self, message: str, line: int | None = None, path: str | None = None) -> None:
        self.message = message
        self.line = line
        self.path = path
        where = ""
        if path is not None and line is not None:
            where = f"{path}:{line}: "
        elif line is not None:
            where = f"line {line}: "
        elif path is not None:
            where = f"{path}: "
        super().__init__(f"{where}{message}")


class SchemaVersionError(RetrialError):
    """The database on disk was written by a different retrial schema.

    Refusing is the point. SQLite will happily open a database whose tables
    don't match what the code expects, and the failure then arrives later as a
    missing column or a silently absent row - long after the command that
    caused it. A trace you cannot trust is worse than one you cannot open.
    """

    def __init__(self, path: str, found: int, expected: int) -> None:
        self.path = path
        self.found = found
        self.expected = expected
        if found > expected:
            detail = (
                f"was written by a newer retrial (schema v{found}); this "
                f"retrial understands v{expected}. Upgrade with "
                "`pip install -U retrial`, or point at a different database "
                "with --db."
            )
        else:
            detail = (
                f"uses schema v{found}, and this retrial (v{expected}) has no "
                "migration for it. This is a bug: every version retrial has "
                "shipped should be readable."
            )
        super().__init__(f"{path} {detail}")
