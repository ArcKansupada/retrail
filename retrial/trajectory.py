"""Materializing a full trajectory from root to tip.

A fork session stores only the steps it actually re-executed; the prefix it
replayed lives in its parent. That's the right storage shape - a fork is a git
branch, not a copy - but the full path from session start to tip then has to be
assembled by walking the parent chain.

Every entry is tagged with where it came from, so nothing downstream has to
guess which steps are replayed history and which are genuine new generation:

    origin="replayed"  came from an ancestor; no model call was made for it
    origin="live"      this session actually re-executed it

One of the design doc's open questions, answered by construction rather than by
a heuristic.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, cast

from .types import JSON, EditProvenance, Origin, Session, Step, TrajectoryEntry

if TYPE_CHECKING:
    from .storage import Store


def trajectory(store: Store, session_id: str) -> list[TrajectoryEntry]:
    """The full path of steps from the root run through to this session's tip."""
    return _walk(store, session_id, set())


def _walk(store: Store, session_id: str, seen: set[str]) -> list[TrajectoryEntry]:
    if session_id in seen:
        raise ValueError(f"cycle in session ancestry at {session_id}")
    seen = seen | {session_id}

    session = store.get_session(session_id)
    own = [_entry(s, origin="live") for s in store.steps_for(session_id)]

    parent_id = session["parent_session_id"]
    if not parent_id:
        return own

    ancestors = _walk(store, parent_id, seen)
    cut = _index_of(ancestors, parent_id, session["forked_at_step"])
    if cut is None:
        raise ValueError(
            f"session {session_id} forked from step {session['forked_at_step']} of "
            f"{parent_id}, but that step is not in the parent's trajectory"
        )

    forked_from = ancestors[cut]
    for entry in ancestors[: cut + 1]:
        entry["origin"] = "replayed"

    if forked_from["step_type"] == "tool_call":
        # The fork resumed *after* this tool call, so the step is part of the
        # replayed prefix - but the fork saw a different output for it than the
        # parent recorded, and that difference is the whole reason the
        # trajectories diverge. Show what the fork saw.
        forked_from = cast(
            TrajectoryEntry,
            dict(
                forked_from,
                output=_as_seen_by(store, session_id, forked_from),
                edited=True,
                edit=_edit_of(session),
            ),
        )
        prefix = ancestors[:cut] + [forked_from]
    else:
        # A model_call fork re-runs *that* call with edited history, so the
        # parent's version of it is not part of the fork's path at all.
        prefix = ancestors[:cut]
        if own:
            own[0] = cast(
                TrajectoryEntry,
                dict(own[0], edited=True, edit=_edit_of(session)),
            )

    return prefix + own


def _as_seen_by(store: Store, session_id: str, forked_from: TrajectoryEntry) -> JSON:
    """Recover the tool output the fork actually resumed with.

    The fork's first model call recorded its input verbatim, and that input is
    the spliced history - so the edited value reads straight off the fork's own
    record. That covers a callback edit too, which cannot be replayed from its
    stored provenance: we read the effect rather than re-deriving it.
    """
    own = store.steps_for(session_id)
    if not own or own[0]["step_type"] != "model_call":
        return forked_from["output"]

    seed = own[0]["input"].get("messages", [])
    blocks = {
        block["tool_use_id"]: block
        for message in seed
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and "tool_use_id" in block
    }
    return [
        copy.deepcopy(blocks.get(entry.get("tool_use_id"), entry))
        for entry in forked_from["output"]
    ]


def _edit_of(session: Session) -> EditProvenance | None:
    import json

    if not session["edit_json"]:
        return None
    try:
        return cast(EditProvenance, json.loads(session["edit_json"]))
    except ValueError:
        return None


def _index_of(
    entries: list[TrajectoryEntry], session_id: str, step_number: int | None
) -> int | None:
    for i, entry in enumerate(entries):
        if entry["session_id"] == session_id and entry["step_number"] == step_number:
            return i
    return None


def _entry(step: Step, origin: Origin) -> TrajectoryEntry:
    return {
        "sha": step["sha"],
        "session_id": step["session_id"],
        "step_number": step["step_number"],
        "step_type": step["step_type"],
        "input": step["input"],
        "output": step["output"],
        "tokens_used": step.get("tokens_used"),
        "cost_usd": step.get("cost_usd"),
        "duration_ms": step.get("duration_ms"),
        "origin": origin,
        "edited": False,
        "edit": None,
    }
