"""Ablation and sweep - forking many times over one axis and comparing.

Both are the same move as bisect: fork, re-execute for real, evaluate a check.
Neither needs a new primitive. They differ only in what they vary.

    ablate  varies the STEP    - perturb each fact in turn, see which matter
    sweep   varies the VALUE   - substitute N values at one step, find a threshold

Why a check rather than comparing answers
-----------------------------------------
A real model rewords itself on every run. Two identical re-executions of the
same prompt produce different text, so "did the answer change?" by text
equality would flag every step as load-bearing no matter what we perturbed --
it would be measuring non-determinism, not causation. A check collapses the
answer to a stable predicate, which is the same reason bisect takes one.

What ablation actually proves
-----------------------------
The signal is asymmetric, and callers are told so rather than left to assume
symmetry:

  check did NOT flip -> the fact is soundly NOT load-bearing. The answer
                        survived without it. This is a real conclusion.
  check DID flip     -> the fact is only POSSIBLY load-bearing. The agent may
                        be reacting to the perturbation itself (a tool that now
                        errors) rather than to the specific value it lost.

Ablation rules facts out rigorously and rules them in suggestively. That is
still far more than attribution-based blame could honestly claim, because this
is intervention rather than inference -- and it is only possible because
forking re-executes for real.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from .bisect import describe_check, forkable_steps, parse_check
from .diff import final_answer
from .errors import RetrailError
from .fork import fork
from .pricing import trajectory_cost
from .trajectory import trajectory
from .types import (
    JSON,
    AblateProbe,
    AblateResult,
    Agent,
    Check,
    Edit,
    Patch,
    Step,
    SweepBoundary,
    SweepProbe,
    SweepResult,
)

if TYPE_CHECKING:
    from .storage import Store

# A neutral "this fact was not available" stand-in. Valid JSON, because tool
# result content is conventionally a JSON string and an agent that parses it
# should see a well-formed object rather than a syntax error.
UNAVAILABLE = json.dumps({"error": "data unavailable"})


def _probe(
    store: Store,
    sha: str,
    edit: Edit,
    agent: Agent,
    agent_args: Sequence[Any],
    agent_kwargs: dict[str, Any],
    name: str,
    # Not CheckFunction: a check reaching here may be the caller's own plain
    # callable, which carries no `.expression`. Only the parsed kind does.
    check: Callable[[str | None], bool] | None,
) -> dict[str, Any]:
    """Fork once and re-execute. A failure is an outcome, not a crash.

    Sweeps and ablations run many probes; one bad probe must not destroy the
    other results, so the error is captured and reported alongside them.
    """
    try:
        fork_id = fork(
            from_sha=sha,
            edit=edit,
            agent=agent,
            store=store,
            name=name,
            agent_args=agent_args,
            **agent_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - a probe's failure is data
        return {
            "session_id": None,
            "answer": None,
            "error": f"{type(exc).__name__}: {exc}",
            "passed": None,
        }

    answer = final_answer(trajectory(store, fork_id))
    return {
        "session_id": fork_id,
        "answer": answer,
        "error": None,
        "passed": check(answer) if check else None,
    }


def _default_perturbation(step: Step) -> Patch:
    """Blank every result this step produced.

    One op per result rather than a fixed `/output/0/content`, so a step with
    parallel tool calls is fully ablated instead of only its first result.
    """
    return [
        {"op": "replace", "path": f"/output/{i}/content", "value": UNAVAILABLE}
        for i in range(len(step["output"]))
    ]


def ablate(
    store: Store,
    session_id: str,
    check: Check,
    agent: Agent,
    perturbation: Patch | Callable[[Step], Patch] | None = None,
    agent_args: Sequence[Any] = (),
    on_probe: Callable[[AblateProbe], None] | None = None,
    **agent_kwargs: Any,
) -> AblateResult:
    """Which recorded facts is this run's outcome load-bearing on?

    Perturbs each tool_call's output in turn, re-executes, and reports whether
    the check flipped. `perturbation` may be a patch, a callable taking the step
    and returning a patch, or None for the default (blank the results).
    """
    if isinstance(check, str):
        check = parse_check(check)

    baseline = final_answer(trajectory(store, session_id))
    baseline_passed = check(baseline)
    if not baseline_passed:
        # Ablation asks "which facts is this outcome load-bearing on?" -- that
        # only means something if the outcome is one you wanted. With a failing
        # baseline, every probe reports "outcome held" and gets labelled NOT
        # load-bearing, which reads as reassuring and is actually vacuous: the
        # run was broken the whole time.
        #
        # ablate and bisect are duals. Bisect is the tool for a run that failed;
        # ablate is the tool for a run that worked. Each refuses the other's job.
        raise RetrailError(
            "the check does not pass on the original run, so there is no good "
            "outcome to ablate. Ablation asks which facts a SUCCESSFUL run "
            "depended on.\n"
            f"  final answer was: {baseline!r}\n"
            "Either the check is wrong for this run, or the run failed -- in "
            "which case you want `retrail bisect`, which localizes which step "
            "made a failure inevitable."
        )

    baseline_trajectory = trajectory(store, session_id)
    baseline_cost: float | None
    baseline_cost, baseline_unpriced = trajectory_cost(baseline_trajectory)
    if baseline_unpriced:
        # An unknown model prices as None, and summing that as zero would make
        # every delta a lie. Report no cost at all rather than a partial one.
        baseline_cost = None

    candidates = [
        s for s in forkable_steps(store, session_id) if s["step_type"] == "tool_call"
    ]
    if not candidates:
        raise RetrailError(
            f"session {session_id} has no forkable tool_call steps to ablate. "
            "Ablation perturbs recorded facts, and this run produced none that "
            "can be resumed from."
        )

    probes: list[AblateProbe] = []
    for step in candidates:
        if perturbation is None:
            edit: Patch = _default_perturbation(step)
        elif callable(perturbation):
            edit = perturbation(step)
        else:
            edit = perturbation

        raw = _probe(
            store,
            step["sha"],
            edit,
            agent,
            agent_args,
            agent_kwargs,
            f"ablate-step-{step['step_number']}",
            check,
        )
        probe = cast(AblateProbe, raw)
        raw.update(
            {
                "sha": step["sha"],
                "step_number": step["step_number"],
                "tools": [b.get("name") for b in step["input"] if isinstance(b, dict)],
                "flipped": (
                    None if probe["passed"] is None else probe["passed"] != baseline_passed
                ),
            }
        )
        probe["cost_usd"], probe["unpriced"] = (
            trajectory_cost(trajectory(store, probe["session_id"]))
            if probe["session_id"]
            else (None, 0)
        )
        # What deleting this fact would do to the bill. Measured, not modelled:
        # both figures are the real recorded cost of a real trajectory. A step
        # that is NOT load-bearing and has a negative delta is a deletion
        # candidate backed by evidence -- the outcome survived without it, and
        # the run got cheaper.
        probe["cost_delta"] = (
            None
            if probe["cost_usd"] is None or baseline_cost is None
            else probe["cost_usd"] - baseline_cost
        )
        probes.append(probe)
        if on_probe:
            on_probe(probe)

    return {
        "session_id": session_id,
        "check": describe_check(check),
        "baseline_answer": baseline,
        "baseline_passed": baseline_passed,
        "baseline_cost": baseline_cost,
        # Not load-bearing AND cheaper without it: delete the tool call.
        "deletable": [
            p
            for p in probes
            if p["flipped"] is False
            and p["cost_delta"] is not None
            and p["cost_delta"] < 0
        ],
        "probes": probes,
        "re_executions": len(probes),
        # Only this one is a sound conclusion. See the module docstring.
        "not_load_bearing": [p for p in probes if p["flipped"] is False],
        "possibly_load_bearing": [p for p in probes if p["flipped"] is True],
        "inconclusive": [p for p in probes if p["flipped"] is None],
    }


def sweep(
    store: Store,
    from_sha: str,
    values: Sequence[JSON],
    agent: Agent,
    path: str = "/output/0/content",
    check: Check | None = None,
    agent_args: Sequence[Any] = (),
    on_probe: Callable[[SweepProbe], None] | None = None,
    **agent_kwargs: Any,
) -> SweepResult:
    """Substitute N values at one step and compare the outcomes.

    Finds thresholds: at what fare does it stop booking? `check` is optional --
    without one you get each value's answer verbatim, which is useful when you
    are reading the results rather than automating over them.
    """
    if isinstance(check, str):
        check = parse_check(check)
    if not values:
        raise RetrailError("sweep needs at least one value")

    step = store.get_step(from_sha)

    probes: list[SweepProbe] = []
    for index, value in enumerate(values):
        raw = _probe(
            store,
            step["sha"],
            {"op": "replace", "path": path, "value": value},
            agent,
            agent_args,
            agent_kwargs,
            f"sweep-{index}",
            check,
        )
        raw["value"] = value
        probe = cast(SweepProbe, raw)
        probes.append(probe)
        if on_probe:
            on_probe(probe)

    return {
        "sha": step["sha"],
        "step_number": step["step_number"],
        "path": path,
        "check": describe_check(check) if check else None,
        "probes": probes,
        "re_executions": len(probes),
        "boundaries": _boundaries(probes) if check else [],
    }


def _boundaries(probes: Sequence[SweepProbe]) -> list[SweepBoundary]:
    """Adjacent value pairs where the check flipped - the thresholds.

    Reported in the order the values were given, so a caller sweeping an
    ordered range reads them as the crossing points.
    """
    out: list[SweepBoundary] = []
    for earlier, later in itertools.pairwise(probes):
        if earlier["passed"] is None or later["passed"] is None:
            continue
        if earlier["passed"] != later["passed"]:
            out.append({"from": earlier, "to": later})
    return out
