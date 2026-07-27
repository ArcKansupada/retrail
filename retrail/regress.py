"""Re-execute recorded runs against your current code.

Your recorded runs are already a regression suite; nobody wrote them. This
re-runs them against whatever `--agent` resolves to *now* and reports which
outcomes changed.

Which step you resume from decides what the test is worth, and no choice is
both cheap and universally right:

    --from first        the opening state. Re-runs the whole trajectory: most
                        faithful, most expensive, and replay saves almost
                        nothing because the prefix is one step.

    --at-tool NAME      the tool call named NAME, everything before it pinned.
                        Tests exactly the decision that follows that fact, for
                        the cost of the steps after it. Earns its keep.

    --from last         the final tool call. Cheapest, and usually VACUOUS: the
                        consequential decisions are already baked into the
                        replayed history and the model is only summarizing.

`first` is the default because a suite that quietly tests nothing is worse than
a slow one. Measured: a booking agent whose budget prompt tightened from $600
to $300 reported "still passing" at `last` (the `book_flight` call), because
the booking had already happened. Resumed at `search_flight`, where the budget
decision lives, it caught the regression in one model call.

So the economics are real but not free: you have to know which decision you are
testing, and `--at-tool` is how you say it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .bisect import describe_check, forkable_steps, parse_check
from .diff import final_answer
from .errors import RetrailError
from .fork import fork
from .pricing import trajectory_cost
from .trajectory import trajectory
from .types import Agent, Check, RerunOutcome, RerunResult, RerunVerdict, Step

if TYPE_CHECKING:
    from .storage import Store


def recorded_runs(store: Store) -> list[str]:
    """The root sessions that make up the corpus.

    Forks are excluded: they are experiments *about* a run, not runs of their
    own. Without this, every bisect probe and ablation becomes a test case and
    the suite grows every time you use the tool.
    """
    return [
        s["id"]
        for s in store.list_sessions()
        if not s["parent_session_id"] and s["status"] == "complete"
    ]


def _resume_point(
    store: Store, session_id: str, where: str, at_tool: str | None = None
) -> Step | None:
    candidates = forkable_steps(store, session_id)
    if not candidates:
        return None

    if at_tool:
        # Resume where the decision under test happens: before it is pinned,
        # after it is re-decided.
        matches = [
            c
            for c in candidates
            if c["step_type"] == "tool_call"
            and any(
                isinstance(b, dict) and b.get("name") == at_tool for b in c["input"]
            )
        ]
        return matches[-1] if matches else None

    if where == "first":
        return candidates[0]
    if where == "last":
        tool_calls = [c for c in candidates if c["step_type"] == "tool_call"]
        return tool_calls[-1] if tool_calls else candidates[0]
    raise RetrailError(f"--from must be 'first' or 'last', got {where!r}")


def rerun(
    store: Store,
    check: Check,
    agent: Agent,
    sessions: Sequence[str] | None = None,
    where: str = "first",
    at_tool: str | None = None,
    agent_args: Sequence[Any] = (),
    on_result: Callable[[RerunOutcome], None] | None = None,
    **agent_kwargs: Any,
) -> RerunResult:
    """Re-execute recorded runs against the current code and report changes."""
    if isinstance(check, str):
        check = parse_check(check)

    targets = sessions or recorded_runs(store)
    if not targets:
        raise RetrailError(
            "no recorded runs to re-execute. Record some agent runs first - "
            "they are the corpus."
        )

    results: list[RerunOutcome] = []
    for session_id in targets:
        before_answer = final_answer(trajectory(store, session_id))
        before = check(before_answer)

        step = _resume_point(store, session_id, where, at_tool)
        if step is None:
            results.append(
                {
                    "session_id": session_id,
                    "before": before,
                    "after": None,
                    "verdict": "skipped",
                    "error": (
                        f"no forkable {at_tool!r} tool call in this run"
                        if at_tool
                        else "no forkable step"
                    ),
                    "answer": None,
                    "cost_usd": None,
                    "model_calls": 0,
                }
            )
            continue

        try:
            fork_id = fork(
                from_sha=step["sha"],
                edit=None,  # nothing substituted: the CODE is what changed
                agent=agent,
                store=store,
                name=f"rerun-{session_id}",
                agent_args=agent_args,
                **agent_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the suite
            results.append(
                {
                    "session_id": session_id,
                    "before": before,
                    "after": None,
                    "verdict": "errored",
                    "error": f"{type(exc).__name__}: {exc}",
                    "answer": None,
                    "cost_usd": None,
                    "model_calls": 0,
                }
            )
            continue

        after_answer = final_answer(trajectory(store, fork_id))
        after = check(after_answer)
        cost, unpriced = trajectory_cost(
            [e for e in trajectory(store, fork_id) if e["origin"] == "live"]
        )

        result: RerunOutcome = {
            "session_id": session_id,
            "fork_id": fork_id,
            "resumed_at": step["sha"],
            "before": before,
            "after": after,
            "verdict": _verdict(before, after),
            "error": None,
            "answer": after_answer,
            "before_answer": before_answer,
            "cost_usd": None if unpriced else cost,
            "model_calls": len(
                [s for s in store.steps_for(fork_id) if s["step_type"] == "model_call"]
            ),
        }
        results.append(result)
        if on_result:
            on_result(result)

    priced = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    return {
        "check": describe_check(check),
        "resumed_from": f"tool:{at_tool}" if at_tool else where,
        "results": results,
        "regressed": [r for r in results if r["verdict"] == "regressed"],
        "fixed": [r for r in results if r["verdict"] == "fixed"],
        "unchanged": [
            r for r in results if r["verdict"] in ("still passing", "still failing")
        ],
        "errored": [r for r in results if r["verdict"] in ("errored", "skipped")],
        "model_calls": sum(r["model_calls"] for r in results),
        "cost_usd": sum(priced) if priced else None,
    }


def _verdict(before: bool, after: bool) -> RerunVerdict:
    if before and not after:
        return "regressed"
    if not before and after:
        return "fixed"
    return "still passing" if before else "still failing"
