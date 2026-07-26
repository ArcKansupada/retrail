"""Bisect - automatically localize which step made a failure inevitable.

Since a fork is real re-execution, this needs no new primitives: fork from a
step with no edit, let the agent re-decide from there, and see whether it still
lands badly. Binary search does the rest.

What it actually finds
----------------------
Forking from step N replays steps 0..N and re-runs everything after. So as N
grows, more of the original (bad) trajectory is baked in and less is left for
the agent to get right. Bisect finds the boundary: the earliest step from which
the agent can no longer recover. That step is where the run went wrong.

`check` describes what a GOOD run looks like, the way a test does. A probe that
satisfies it recovered; one that doesn't reproduced the failure.

The honest caveat
-----------------
Binary search assumes monotonicity - that if the failure reproduces at step N
it also reproduces at every later step. Replay is exact, so the prefix is not a
source of noise, but the re-executed suffix is a real model and genuinely
non-deterministic. A step near the boundary may recover on one run and not the
next, so a single probe per step can land a step or two off.

This is not papered over: every probe is recorded as its own session, so the
result is auditable rather than a bare answer to trust. `samples` re-probes each
candidate and requires unanimity before calling it good, which trades API calls
for confidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from .diff import final_answer
from .errors import RetrailError
from .fork import fork
from .trajectory import trajectory
from .types import (
    Agent,
    BisectProbe,
    BisectResult,
    Check,
    CheckFunction,
    Step,
)

if TYPE_CHECKING:
    from .storage import Store


class CheckError(RetrailError):
    pass


_CHECK = re.compile(
    r"^\s*output\s+(?P<negate>not\s+)?(?P<op>contains|matches)\s+"
    r"(?P<quote>['\"])(?P<value>.*)(?P=quote)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_check(expression: str) -> CheckFunction:
    """Parse a check expression into a predicate over the final answer.

        output contains 'confirmed'
        output not contains 'error'
        output matches '\\$[0-9]+'

    A predicate is data the CLI can accept; a Python callable is the escape
    hatch for anything this doesn't express - same split as the fork edit API.
    """
    match = _CHECK.match(expression)
    if not match:
        raise CheckError(
            f"could not parse check {expression!r}. Expected one of:\n"
            "  output contains 'text'\n"
            "  output not contains 'text'\n"
            "  output matches 'regex'\n"
            "For anything else, use the Python API with a callable."
        )

    value = match.group("value")
    negate = bool(match.group("negate"))
    if match.group("op").lower() == "matches":
        try:
            pattern = re.compile(value)
        except re.error as exc:
            raise CheckError(f"invalid regex {value!r}: {exc}") from None
        def test(answer: str | None) -> bool:
            return bool(pattern.search(answer or ""))

    else:

        def test(answer: str | None) -> bool:
            return value in (answer or "")

    def check(answer: str | None) -> bool:
        return not test(answer) if negate else test(answer)

    # A plain function has no `expression` attribute statically; CheckFunction
    # is the protocol that says the returned object does.
    check.expression = expression  # type: ignore[attr-defined]
    return cast(CheckFunction, check)


def describe_check(check: Callable[..., Any]) -> str:
    """How a result should name the check it applied.

    A parsed check reports its source expression; a user's callable reports its
    name. Neither should ever surface as `<function check at 0x7f...>`.
    """
    return str(getattr(check, "expression", getattr(check, "__name__", "<callable>")))


def forkable_steps(store: Store, session_id: str) -> list[Step]:
    """Steps we can honestly resume from.

    A trailing tool_call is excluded: the message state after it was never
    observed, so there is nothing to replay. See fork.py.
    """
    steps = store.steps_for(session_id)
    if steps and steps[-1]["step_type"] == "tool_call":
        steps = steps[:-1]
    return steps


def bisect(
    store: Store,
    session_id: str,
    check: Check,
    agent: Agent,
    agent_args: Sequence[Any] = (),
    samples: int = 1,
    on_probe: Callable[[BisectProbe], None] | None = None,
    **agent_kwargs: Any,
) -> BisectResult:
    """Find the earliest step from which the agent can no longer recover.

    Returns a result dict with the culprit step, every probe that was run, and
    the number of re-executions it cost.
    """
    if isinstance(check, str):
        check = parse_check(check)
    if samples < 1:
        raise CheckError("samples must be at least 1")

    candidates = forkable_steps(store, session_id)
    if not candidates:
        raise RetrailError(f"session {session_id} has no steps to bisect")

    original_answer = final_answer(trajectory(store, session_id))
    if check(original_answer):
        raise RetrailError(
            "the check already passes on the original run, so there is no "
            f"failure to localize. Final answer was: {original_answer!r}"
        )

    probes: list[BisectProbe] = []
    cache: dict[int, bool] = {}

    def probe(index: int) -> bool:
        """True if the agent recovered when resumed from candidates[index]."""
        if index in cache:
            return cache[index]
        step = candidates[index]
        recovered = True
        for _ in range(samples):
            fork_id = fork(
                from_sha=step["sha"],
                edit=None,  # pure replay-and-resume; no substituted facts
                agent=agent,
                store=store,
                name=f"bisect-probe-{step['step_number']}",
                agent_args=agent_args,
                **agent_kwargs,
            )
            answer = final_answer(trajectory(store, fork_id))
            passed = check(answer)
            record: BisectProbe = {
                "step_number": step["step_number"],
                "sha": step["sha"],
                "session_id": fork_id,
                "passed": passed,
                "answer": answer,
            }
            probes.append(record)
            if on_probe:
                on_probe(record)
            if not passed:
                # One bad sample is enough: the failure is reachable from here.
                recovered = False
                break
        cache[index] = recovered
        return recovered

    # Find the first index that does NOT recover.
    lo, hi = 0, len(candidates) - 1
    culprit = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if probe(mid):
            lo = mid + 1  # recovered here, so the cause is later
        else:
            culprit = mid  # reproduced here, so look earlier for the boundary
            hi = mid - 1

    return {
        "session_id": session_id,
        "check": describe_check(check),
        "original_answer": original_answer,
        "candidates": candidates,
        "probes": probes,
        "re_executions": len(probes),
        "culprit": candidates[culprit] if culprit is not None else None,
        "inherent": culprit == 0,
        "unreproducible": culprit is None,
        "samples": samples,
    }
