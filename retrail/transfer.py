"""Moving traces between stores.

`portable.py` knows the file format; this knows the store. Export walks the
session tree and emits rows; import (step 3) reads them back. Keeping the two
apart is what lets a file be validated without a database.

The promise being kept here is "here is my trace, fork it yourself and see" -
so an exported session has to arrive still usable, not merely still readable.
That is why ancestors travel with it by default: a fork without its parents
cannot be diffed and has no trajectory to walk, which are the two things you
would send it for.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .errors import ExportFormatError, NotFound, SchemaVersionError
from .portable import (
    FORMAT_VERSION,
    Document,
    dump_line,
    header_row,
    parse_document,
    session_row,
    step_row,
)
from .serialize import canonical_json
from .sha import compute_sha
from .storage import SCHEMA_VERSION, Store, schema_version
from .types import ExportSession, ExportStep, Session, SessionStatus


def export(
    store: Store,
    session_ids: Sequence[str] | None = None,
    ancestors: bool = True,
    requires: Iterable[str] = (),
) -> Iterator[str]:
    """Emit `session_ids` (default: the whole store) as export-file lines.

    Lazy, so a large store streams to a pipe instead of being assembled in
    memory first. Validation of the requested ids happens eagerly though -
    `NotFound` for a bad id arrives before any output, rather than halfway
    through a file the caller has already started writing.

    Ancestors are included unless `ancestors=False`. Descendants never are:
    exporting a root should not hand over every experiment you ran on top of
    it, and fork names tend to be candid.
    """
    selected = _select(store, session_ids, ancestors)

    def lines() -> Iterator[str]:
        yield dump_line(
            header_row(schema=schema_version(store.conn), requires=requires)
        )
        for session in selected:
            yield dump_line(session_row(session))
        # Read each session's steps only when its block is reached, so the
        # whole store is never in memory at once.
        for session in selected:
            for step in store.steps_for(session["id"]):
                yield dump_line(step_row(step))

    return lines()


def _select(
    store: Store, session_ids: Sequence[str] | None, ancestors: bool
) -> list[Session]:
    """The sessions to emit, parents always before children.

    Ordering is a guarantee of the format, so it is produced here rather than
    hoped for. Sorting by `created_at` would *usually* work, since a fork is
    created after its parent - but two rows can share a timestamp, and
    "usually ordered" is not an invariant an importer can build on.
    """
    if session_ids is None:
        wanted = [s["id"] for s in store.list_sessions()]
        ancestors = True  # a whole-store export is closed by definition
    else:
        wanted = list(dict.fromkeys(session_ids))

    # Eagerly, not inside the generator: `_chain` reads every requested
    # session, so a bad id raises from the `export()` call itself rather than
    # from the first `next()` - by which point a caller may have opened a file
    # and written a header into it.
    emitted: dict[str, Session] = {}
    for session_id in wanted:
        for session in _chain(store, session_id, ancestors):
            emitted.setdefault(session["id"], session)
    return list(emitted.values())


# -- import: the validation pass ------------------------------------------------
#
# Nothing here writes. It decides what *would* happen and refuses if any of it
# is wrong, so a file that fails on its last line leaves the store exactly as
# it was - rather than half a trace, which is the state this project spends
# most of its effort not producing.

#: Features an importer must understand by name. Empty at format v1: nothing
#: has needed to declare itself indispensable yet.
KNOWN_FEATURES: frozenset[str] = frozenset()

#: One entry per format version that has ever shipped, mapping it forward.
#: Empty at v1 - there is no older layout to come from.
_TRANSLATORS: dict[int, Callable[[Document], Document]] = {}

#: Terminal states. A session may advance into one of these on import, and
#: never back out of one.
_TERMINAL: frozenset[str] = frozenset({"complete", "failed"})


@dataclass
class ImportPlan:
    """What an import would do. Produced by `validate`, applied by `import_`.

    Separating the two is what makes every refusal cheap to test: a plan can
    be inspected without a write ever happening, and the write in step 4 has
    no decisions left to make.
    """

    new_sessions: list[ExportSession] = field(default_factory=list)
    new_steps: list[ExportStep] = field(default_factory=list)
    #: Already present with identical content. The common case when a trace
    #: comes back from someone who forked it.
    skipped_sessions: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    #: (session_id, new status) for runs that finished after they were sent.
    status_updates: list[tuple[str, SessionStatus]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.new_sessions or self.new_steps or self.status_updates)


def validate(store: Store, document: Document, path: str | None = None) -> ImportPlan:
    """Decide what importing `document` into `store` would do, or refuse.

    Refuses rather than merges whenever the file and the store disagree about
    something they both claim to know. Silently reconciling two different runs
    under one id would produce a trace that reads as valid and describes
    something that never happened - the failure mode this project treats as
    worse than a crash.
    """
    document, warnings = _translate(document, path)
    plan = ImportPlan(warnings=warnings)
    _check_schema(store, document, path)

    known_sessions = {s["id"] for s in document.sessions}
    for session in document.sessions:
        _plan_session(store, session, document, plan, path)

    # One read per session rather than one per step: a session's steps are
    # contiguous in the file, so the whole local block is wanted at once.
    local_steps: dict[str, dict[int, str]] = {}
    for step in document.steps:
        session_id = step["session_id"]
        if session_id not in local_steps:
            local_steps[session_id] = {
                s["step_number"]: s["sha"] for s in store.steps_for(session_id)
            }
        _plan_step(step, local_steps[session_id], document, plan, path)

    _check_parents(store, document, known_sessions, path)
    return plan


def _bad(
    message: str, document: Document, key: str | None = None, path: str | None = None
) -> ExportFormatError:
    return ExportFormatError(
        message, line=document.line_of(key) if key else None, path=path
    )


def _translate(document: Document, path: str | None) -> tuple[Document, list[str]]:
    """Bring an older file up to FORMAT_VERSION, or refuse an unreadable one.

    Forward translation only. A *newer* file cannot be translated in general -
    an unrecognized field might be load-bearing and nothing about it says so -
    which is what `requires` exists to resolve: a producer names the features
    an importer must understand, and anything not named is inert.
    """
    found = document.header["format"]

    if found > FORMAT_VERSION:
        unknown = sorted(set(document.header["requires"]) - KNOWN_FEATURES)
        if unknown:
            raise ExportFormatError(
                f"file is format v{found} and requires {', '.join(unknown)}, which "
                f"this retrail (format v{FORMAT_VERSION}) does not understand. "
                "Upgrade with `pip install -U retrail`.",
                line=1,
                path=path,
            )
        return document, [
            f"file is format v{found}, newer than this retrail's v{FORMAT_VERSION}. "
            "It declares no features this version must understand, so it is being "
            "read anyway - anything unrecognized in it is ignored."
        ]

    while found < FORMAT_VERSION:
        translate = _TRANSLATORS.get(found)
        if translate is None:
            raise ExportFormatError(
                f"file is format v{found}, and this retrail has no translation for "
                "it. Every format retrail has shipped should be readable; this is "
                "a bug.",
                line=1,
                path=path,
            )
        document = translate(document)
        found = document.header["format"]

    return document, []


def _check_schema(store: Store, document: Document, path: str | None) -> None:
    """The rows themselves must come from a schema this store can hold."""
    found = document.header["schema"]
    if found > SCHEMA_VERSION:
        raise SchemaVersionError(path or "<export>", found, SCHEMA_VERSION)
    if found > schema_version(store.conn):
        # Cannot happen through the CLI, since opening a Store upgrades it.
        # Worth stating anyway: importing v2 rows into a v1 store would write
        # columns that are not there.
        raise ExportFormatError(
            f"file carries schema v{found} but this store is at "
            f"v{schema_version(store.conn)}",
            path=path,
        )


def _plan_session(
    store: Store,
    session: ExportSession,
    document: Document,
    plan: ImportPlan,
    path: str | None,
) -> None:
    try:
        existing = store.get_session(session["id"])
    except NotFound:
        plan.new_sessions.append(session)
        return

    # Present already. The overwhelmingly common reason is that this is the
    # same session coming home - you exported it, someone forked it, and the
    # file they sent back contains your original too.
    differing = _disagreements(existing, session)
    if differing:
        raise _bad(
            f"session {session['id']} already exists here with a different "
            f"{', '.join(differing)}. These are two different runs claiming one "
            "id; importing would merge them. Import into a separate store with "
            "--db instead.",
            document,
            session["id"],
            path,
        )

    if existing["status"] != session["status"]:
        # A run legitimately finishes after it was exported: `running` on the
        # first export, `complete` on the second. Advancing is an update;
        # anything else is two different runs.
        if existing["status"] == "running" and session["status"] in _TERMINAL:
            plan.status_updates.append((session["id"], session["status"]))
        else:
            raise _bad(
                f"session {session['id']} is {existing['status']} here but "
                f"{session['status']} in the file; a finished run does not change "
                "its outcome",
                document,
                session["id"],
                path,
            )

    plan.skipped_sessions.append(session["id"])


def _disagreements(existing: Session, incoming: ExportSession) -> list[str]:
    """Fields that must agree. Status is excluded - see `_plan_session`."""
    local_edit = json.loads(existing["edit_json"]) if existing["edit_json"] else None
    pairs = {
        "name": (existing["name"], incoming["name"]),
        "parent_session_id": (
            existing["parent_session_id"],
            incoming["parent_session_id"],
        ),
        "parent_sha": (existing["parent_sha"], incoming["parent_sha"]),
        "forked_at_step": (existing["forked_at_step"], incoming["forked_at_step"]),
        "edit": (local_edit, incoming["edit"]),
        "created_at": (existing["created_at"], incoming["created_at"]),
    }
    return [name for name, (a, b) in pairs.items() if a != b]


def _plan_step(
    step: ExportStep,
    local: dict[int, str],
    document: Document,
    plan: ImportPlan,
    path: str | None,
) -> None:
    # The sha is checked before anything else is believed about the row. It
    # hashes session id, step number, type, input and output, so recomputing
    # it proves the content is the content that was exported - not merely that
    # the file is well formed.
    recomputed = compute_sha(
        step["session_id"],
        step["step_number"],
        step["step_type"],
        step["input"],
        step["output"],
    )
    if recomputed != step["sha"]:
        raise _bad(
            f"step {step['sha'][:12]} does not match its content (computed "
            f"{recomputed[:12]}). The file has been altered since it was "
            "exported, and a trace that changed in transit is not a recording.",
            document,
            step["sha"],
            path,
        )

    existing = local.get(step["step_number"])
    if existing is None:
        plan.new_steps.append(step)
    elif existing == step["sha"]:
        plan.skipped_steps.append(step["sha"])
    else:
        raise _bad(
            f"session {step['session_id']} already has a different step "
            f"{step['step_number']} here ({existing[:12]}, not "
            f"{step['sha'][:12]}). Import into a separate store with --db.",
            document,
            step["sha"],
            path,
        )


def _check_parents(
    store: Store, document: Document, in_file: set[str], path: str | None
) -> None:
    """Every named parent must exist somewhere - in the file, or already here.

    A file exported with `--no-ancestors` names a parent it does not carry,
    which is fine if the receiving store already holds it. If nobody has it,
    the fork cannot be diffed or walked, and importing it would create exactly
    the half-usable state this project refuses elsewhere.
    """
    for session in document.sessions:
        parent = session["parent_session_id"]
        if parent is None or parent in in_file:
            continue
        try:
            store.get_session(parent)
        except NotFound as exc:
            raise _bad(
                f"session {session['id']} names parent {parent}, which is neither "
                "in this file nor in this store. Export the parent too - without "
                "it the fork cannot be diffed or replayed.",
                document,
                session["id"],
                path,
            ) from exc


# -- import: the write pass -----------------------------------------------------


@dataclass
class ImportResult:
    """What an import did. Every count is a row, not a file."""

    sessions_added: int = 0
    steps_added: int = 0
    #: Already present with identical content - the file coming home.
    sessions_skipped: int = 0
    steps_skipped: int = 0
    status_updated: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed_nothing(self) -> bool:
        return not (self.sessions_added or self.steps_added or self.status_updated)


def import_(
    store: Store, source: Iterable[str] | Document, path: str | None = None
) -> ImportResult:
    """Read an export into `store`. All of it, or none of it.

    `source` is either the file's lines or an already-parsed `Document`.

    Everything is decided before anything is written (see `validate`), and the
    writing itself runs in one transaction. A file that is rejected - for a
    sha that does not match, a parent that is nowhere, two runs claiming one
    id - leaves the store byte for byte as it was.
    """
    document = source if isinstance(source, Document) else parse_document(source, path)
    plan = validate(store, document, path)

    result = ImportResult(
        sessions_skipped=len(plan.skipped_sessions),
        steps_skipped=len(plan.skipped_steps),
        warnings=list(plan.warnings),
    )
    if plan.is_empty:
        return result

    with store.transaction() as conn:
        # Sessions first, and ancestors before forks - both guaranteed by the
        # parse - so the foreign keys resolve as each row lands rather than
        # needing the constraint deferred.
        for session in plan.new_sessions:
            _insert_session(conn, session)
            result.sessions_added += 1
        for step in plan.new_steps:
            _insert_step(conn, step)
            result.steps_added += 1
        for session_id, status in plan.status_updates:
            conn.execute(
                "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
            )
            result.status_updated += 1

    return result


def _insert_session(conn: sqlite3.Connection, session: ExportSession) -> None:
    conn.execute(
        "INSERT INTO sessions (id, name, parent_session_id, parent_sha, "
        "forked_at_step, edit_json, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session["id"],
            session["name"],
            session["parent_session_id"],
            session["parent_sha"],
            session["forked_at_step"],
            json.dumps(session["edit"]) if session["edit"] is not None else None,
            session["created_at"],
            session["status"],
        ),
    )


def _insert_step(conn: sqlite3.Connection, step: ExportStep) -> None:
    """Written directly rather than through `Store.add_step`.

    add_step commits per row, which would defeat the transaction, and it mints
    a fresh sha and created_at. Both must survive: the sha is the handle the
    step is quoted by on the machine it came from, and the timestamp is when
    the run happened, not when it arrived.
    """
    conn.execute(
        "INSERT INTO steps (sha, session_id, step_number, step_type, "
        "input_json, output_json, tokens_used, cost_usd, duration_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            step["sha"],
            step["session_id"],
            step["step_number"],
            step["step_type"],
            canonical_json(step["input"]),
            canonical_json(step["output"]),
            step["tokens_used"],
            step["cost_usd"],
            step["duration_ms"],
            step["created_at"],
        ),
    )


def _chain(store: Store, session_id: str, ancestors: bool) -> list[Session]:
    """A session and its ancestors, root first."""
    session = store.get_session(session_id)
    if not ancestors:
        return [session]

    chain = [session]
    seen = {session_id}
    while True:
        parent_id = chain[-1]["parent_session_id"]
        if parent_id is None:
            break
        if parent_id in seen:
            # fork() writes a parent strictly before its child, so a cycle
            # means the store is damaged. Emitting it would produce a file
            # that cannot be imported anywhere, including back here.
            raise ExportFormatError(
                f"session {session_id} has a cyclic parent chain through "
                f"{parent_id}; the store is inconsistent and cannot be exported"
            )
        try:
            chain.append(store.get_session(parent_id))
        except NotFound as exc:
            raise ExportFormatError(
                f"session {chain[-1]['id']} names parent {parent_id}, which is not "
                "in this store. The trace is incomplete and would not be usable "
                "where it landed - export the parent too, or pass ancestors=False "
                "to send this session alone."
            ) from exc
        seen.add(parent_id)

    chain.reverse()
    return chain
