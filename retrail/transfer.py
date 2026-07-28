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

from collections.abc import Iterable, Iterator, Sequence

from .errors import ExportFormatError, NotFound
from .portable import dump_line, header_row, session_row, step_row
from .storage import Store, schema_version
from .types import Session


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
