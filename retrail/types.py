"""The shapes retrail hands back.

Every public function returns a plain dict. That stays true - the records are
JSON-shaped, they print, they serialize, and nothing downstream has to learn a
class hierarchy to read one. But "plain dict" is not the same as "undocumented
dict", and before this module the only description of a step record was prose
in a README, which no editor and no type checker can read.

A TypedDict *is* a dict at runtime, so nothing here changes behaviour: no
construction cost, no isinstance semantics, no new failure mode. What it buys
is that the key names exist somewhere a tool can find them, and that a shape
change has exactly one place to happen.

On `JSON = Any`
---------------
Recorded inputs and outputs are whatever the user's model and tools produced.
A precise recursive JSON alias would be more honest about the data but would
force a cast at nearly every read of a payload, which buries the annotations
that carry real information. `JSON` is an alias for `Any` that says "arbitrary
recorded payload" to a reader while staying out of the way of the checker.

Stability
---------
Per CHANGELOG.md: before 1.0 these shapes may GAIN keys in a minor release.
An existing key will not silently change meaning.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, TypedDict, Union

__all__ = [
    "JSON",
    "StepType",
    "Origin",
    "SessionStatus",
    "Session",
    "Step",
    "TrajectoryEntry",
    "PatchOp",
    "Patch",
    "Edit",
    "EditProvenance",
    "Check",
    "CheckFunction",
    "Agent",
    "BisectProbe",
    "BisectResult",
    "AblateProbe",
    "AblateResult",
    "SweepProbe",
    "SweepBoundary",
    "SweepResult",
    "RerunVerdict",
    "RerunOutcome",
    "RerunResult",
    "DiffSide",
    "DiffBlock",
    "Divergence",
    "DiffFinal",
    "DiffResult",
]

#: An arbitrary recorded payload: message history, model response, tool result.
JSON = Any

StepType = Literal["model_call", "tool_call"]

#: Where a trajectory entry came from. Not a heuristic - a step is `replayed`
#: if it lives in an ancestor session and `live` if this session executed it.
Origin = Literal["replayed", "live"]

SessionStatus = Literal["running", "complete", "failed"]


# -- stored records -----------------------------------------------------------


class Session(TypedDict):
    """A run. A fork is a session with a parent, never a mutation of one."""

    id: str
    name: str | None
    parent_session_id: str | None
    parent_sha: str | None
    forked_at_step: int | None
    #: The fork's edit, as stored JSON text. `None` for a root run or an
    #: unedited fork. Parsed form reaches you as `TrajectoryEntry["edit"]`.
    edit_json: str | None
    created_at: float
    status: SessionStatus


class Step(TypedDict):
    """One recorded model call or tool call, as it comes back from the store."""

    id: int
    #: Full sha256. Short forms are display-only; prefixes resolve like git's.
    sha: str
    session_id: str
    step_number: int
    step_type: StepType
    input: JSON
    output: JSON
    tokens_used: int | None
    #: `None` means unpriced, never free. See pricing.py.
    cost_usd: float | None
    duration_ms: float | None
    created_at: float


class TrajectoryEntry(TypedDict):
    """A step as it appears in a materialized trajectory.

    Deliberately not a `Step`: it carries provenance (`origin`, `edited`,
    `edit`) that only exists once the parent chain has been walked, and drops
    the storage-local `id` and `created_at`.
    """

    sha: str
    session_id: str
    step_number: int
    step_type: StepType
    input: JSON
    output: JSON
    tokens_used: int | None
    cost_usd: float | None
    duration_ms: float | None
    origin: Origin
    #: True on the one entry a fork's edit landed on.
    edited: bool
    edit: EditProvenance | None


# -- editing ------------------------------------------------------------------


class _PatchOpRequired(TypedDict):
    op: Literal["replace", "add", "remove"]
    #: JSON Pointer rooted at the step, e.g. "/output/0/content".
    path: str


class PatchOp(_PatchOpRequired, total=False):
    """One patch operation. `value` is required except for `remove`.

    Split across two TypedDicts because `typing.NotRequired` landed in 3.11 and
    retrail supports 3.10.
    """

    value: JSON


Patch = Union[PatchOp, list[PatchOp]]

#: What `fork(edit=...)` accepts. A patch round-trips from the stored record; a
#: callback does not, and says so in its provenance.
Edit = Union[Patch, Callable[[dict[str, Any]], dict[str, Any]], None]


class PatchProvenance(TypedDict):
    type: Literal["patch"]
    patch: Patch


class CallbackProvenance(TypedDict):
    type: Literal["callback"]
    repr: str
    note: str


#: Stored on the fork's session row so `retrail log` can show WHAT changed, not
#: just where.
EditProvenance = Union[PatchProvenance, CallbackProvenance]


# -- checks and agents --------------------------------------------------------


class CheckFunction(Protocol):
    """A parsed check: does this final answer look like a good run?

    Carries the source expression so results can report the check they applied
    rather than `<function check at 0x...>`.
    """

    expression: str

    def __call__(self, answer: str | None) -> bool: ...


#: A check expression the CLI can parse, or any predicate over the final answer.
Check = Union[str, Callable[[str | None], bool]]


class Agent(Protocol):
    """A `@record`-decorated agent loop.

    The one structural requirement: it takes the message history as its first
    positional argument, which is what lets a fork seed it with edited state.
    """

    __retrail_agent__: bool
    last_session_id: str | None

    def __call__(self, messages: list[Any], /, *args: Any, **kwargs: Any) -> Any: ...


# -- bisect -------------------------------------------------------------------


class BisectProbe(TypedDict):
    """One re-execution resumed from a candidate step."""

    step_number: int
    sha: str
    #: The probe's own session, recorded so the result is auditable.
    session_id: str
    passed: bool
    answer: str | None


class BisectResult(TypedDict):
    session_id: str
    check: str
    original_answer: str | None
    candidates: list[Step]
    probes: list[BisectProbe]
    re_executions: int
    #: The earliest step from which the agent could not recover.
    culprit: Step | None
    #: The run was doomed from step 0 - nothing downstream caused it.
    inherent: bool
    #: Every probe recovered, so the failure did not reproduce at all.
    unreproducible: bool
    samples: int


# -- ablate -------------------------------------------------------------------


class AblateProbe(TypedDict):
    """One step's fact, perturbed, re-executed, and scored."""

    session_id: str | None
    answer: str | None
    #: A probe that raised is data, not a crash: the sweep continues.
    error: str | None
    passed: bool | None
    sha: str
    step_number: int
    tools: list[str | None]
    #: True  -> outcome changed, so possibly load-bearing (weak signal).
    #: False -> outcome held, so not load-bearing (sound conclusion).
    #: None  -> the probe errored and proves nothing.
    flipped: bool | None
    cost_usd: float | None
    unpriced: int
    cost_delta: float | None


class AblateResult(TypedDict):
    session_id: str
    check: str
    baseline_answer: str | None
    baseline_passed: bool
    baseline_cost: float | None
    #: Not load-bearing AND cheaper without it - a deletion backed by evidence.
    deletable: list[AblateProbe]
    probes: list[AblateProbe]
    re_executions: int
    not_load_bearing: list[AblateProbe]
    possibly_load_bearing: list[AblateProbe]
    inconclusive: list[AblateProbe]


# -- sweep --------------------------------------------------------------------


class SweepProbe(TypedDict):
    session_id: str | None
    answer: str | None
    error: str | None
    passed: bool | None
    value: JSON


# Functional syntax: "from" is a keyword, so the class form cannot spell it.
SweepBoundary = TypedDict("SweepBoundary", {"from": SweepProbe, "to": SweepProbe})


class SweepResult(TypedDict):
    sha: str
    step_number: int
    path: str
    check: str | None
    probes: list[SweepProbe]
    re_executions: int
    #: Adjacent pairs where the check flipped: the decision boundary.
    boundaries: list[SweepBoundary]


# -- rerun --------------------------------------------------------------------

RerunVerdict = Literal[
    "regressed",
    "fixed",
    "still passing",
    "still failing",
    "errored",
    "skipped",
]


class _RerunOutcomeRequired(TypedDict):
    session_id: str
    before: bool
    after: bool | None
    verdict: RerunVerdict
    error: str | None
    answer: str | None
    cost_usd: float | None
    model_calls: int


class RerunOutcome(_RerunOutcomeRequired, total=False):
    """One recorded run, re-executed against the current code.

    The three optional keys are absent when the run was skipped or errored -
    there is no fork to point at in that case.
    """

    fork_id: str
    resumed_at: str
    before_answer: str | None


class RerunResult(TypedDict):
    check: str
    #: "first", "last", or "tool:NAME" - where each run resumed from.
    resumed_from: str
    results: list[RerunOutcome]
    regressed: list[RerunOutcome]
    fixed: list[RerunOutcome]
    unchanged: list[RerunOutcome]
    errored: list[RerunOutcome]
    model_calls: int
    cost_usd: float | None


# -- diff ---------------------------------------------------------------------


class DiffSide(TypedDict):
    id: str
    session: Session
    steps: list[TrajectoryEntry]


class DiffBlock(TypedDict):
    """One aligned run of steps. `tag` is difflib's: equal/replace/delete/insert."""

    tag: str
    a: list[TrajectoryEntry]
    b: list[TrajectoryEntry]


class Divergence(TypedDict):
    a: TrajectoryEntry | None
    b: TrajectoryEntry | None
    #: The edit that caused the split, when one side is a fork of the other.
    edit: EditProvenance | None
    sha: str


class DiffFinal(TypedDict):
    a: str | None
    b: str | None


class DiffResult(TypedDict):
    a: DiffSide
    b: DiffSide
    common_ancestor: str | None
    #: Only the LEADING run of equal steps. A later equal block means the runs
    #: re-converged, which is interesting but is not prefix.
    shared_prefix: list[TrajectoryEntry]
    divergence: Divergence | None
    blocks: list[DiffBlock]
    final: DiffFinal
    identical: bool
