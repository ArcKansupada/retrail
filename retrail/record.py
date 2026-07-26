"""The recording layer.

A decorator wraps the user's existing loop. It needs two integration points:
the function that calls the model, and the function that executes tool calls.
Both are passed in as explicit arguments rather than monkey-patched onto the
SDK client. That's more code for the user up front, but every recorded step
traces back to a line they wrote - which matters when the whole product's
credibility rests on "the replay is exactly what happened".
"""

from __future__ import annotations

import copy
import functools
import inspect
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from .errors import IntegrationError
from .pricing import cost_of
from .serialize import to_jsonable
from .storage import Store
from .types import JSON

P = ParamSpec("P")
R = TypeVar("R")

# Set by fork() so a re-executed run records into the fork's session (with its
# parent provenance) instead of minting a fresh root session.
_pending: dict[str, Any] = {}

# One connection per database per process. Without this, every recorded run
# opens a new sqlite connection and never closes it.
_default_stores: dict[str, Store] = {}


def _default_store() -> Store:
    from .storage import default_db_path

    path = default_db_path()
    if path not in _default_stores:
        _default_stores[path] = Store(path)
    return _default_stores[path]


def record(
    session_name: str | None = None,
    store: Store | None = None,
    model_arg: str = "call_model",
    tools_arg: str = "execute_tools",
    messages_arg: str = "messages",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Record every step of a raw agent loop.

        @record(session_name="booking-agent")
        def run_agent(messages, tools, call_model, execute_tools):
            while True:
                response = call_model(messages, tools)
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    return response
                messages.append({"role": "user", "content": execute_tools(response)})

    `messages` must be a parameter, not a blank start assumed inside the body.
    That is the one non-negotiable convention: it's what lets a fork seed the
    loop with edited history and get genuine re-execution.

    The decorated function keeps its own signature, so your call sites stay
    checked. It also gains `last_session_id` and `__retrail_agent__`; those are
    set with `type: ignore` because a function object has no such attributes
    statically, which is exactly what `types.Agent` describes.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(fn)
        for name in (messages_arg, model_arg, tools_arg):
            if name not in signature.parameters:
                raise IntegrationError(
                    f"@record expected {fn.__name__!r} to take a {name!r} parameter "
                    f"but its signature is {signature}. retrail intercepts the model "
                    "call and the tool executor by name; pass them in explicitly "
                    "(or set the *_arg options on @record)."
                )

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()

            # Validate BEFORE touching the store. Both interception points are
            # wrapped here, before the body runs, so a lazily-constructed
            # callable cannot work: retrail wraps whatever the argument is at
            # call time, and a `None` sentinel meant to be replaced inside the
            # body gets wrapped instead, failing as "'NoneType' object is not
            # callable" several frames deep. Say so at the boundary - and say
            # it before a session row exists, or a rejected call would strand
            # an empty session in the store.
            for name in (model_arg, tools_arg):
                if not callable(bound.arguments[name]):
                    raise IntegrationError(
                        f"{fn.__name__}() got {name}={bound.arguments[name]!r}, which "
                        f"is not callable. @record wraps {name} before your function "
                        "body runs, so it must already be a callable when the agent "
                        "is invoked - it cannot be built lazily inside the body. "
                        "Give it a real function as its default and do any lazy "
                        "setup (client construction, auth) on first call inside "
                        "that function."
                    )

            ctx = _pending.pop("session", None)
            active_store = ctx["store"] if ctx else (store or _default_store())
            session_id = (
                ctx["session_id"]
                if ctx
                else active_store.create_session(name=session_name or fn.__name__)
            )

            recorder = _Recorder(active_store, session_id)
            bound.arguments[model_arg] = recorder.wrap_model(bound.arguments[model_arg])
            bound.arguments[tools_arg] = recorder.wrap_tools(bound.arguments[tools_arg])

            wrapper.last_session_id = session_id  # type: ignore[attr-defined]
            try:
                result = fn(*bound.args, **bound.kwargs)
            except BaseException:
                # A crashed run is the one you most want to inspect, so keep
                # every step recorded so far and mark why it stopped.
                active_store.set_status(session_id, "failed")
                raise

            active_store.set_status(session_id, "complete")
            return result

        wrapper.last_session_id = None  # type: ignore[attr-defined]
        wrapper.__retrail_agent__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorator


class _Recorder:
    def __init__(self, store: Store, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def _next(self) -> int:
        return self.store.next_step_number(self.session_id)

    def wrap_model(self, call_model: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(call_model)
        def wrapped(messages: Any, *args: Any, **kwargs: Any) -> Any:
            # Snapshot the history VERBATIM, exactly as the user's loop built
            # it, before the model sees it. This snapshot is what a fork later
            # replays - we never reconstruct history from parts, because
            # guessing how the loop assembles messages would make the replay an
            # imitation rather than a recording.
            snapshot = to_jsonable(copy.deepcopy(list(messages)))
            started = time.perf_counter()
            response = call_model(messages, *args, **kwargs)
            elapsed_ms = (time.perf_counter() - started) * 1000

            serialized = to_jsonable(response)
            tokens, cost = _usage(serialized)
            self.store.add_step(
                self.session_id,
                self._next(),
                "model_call",
                {"messages": snapshot, "extra": to_jsonable(list(args))},
                serialized,
                tokens_used=tokens,
                cost_usd=cost,
                duration_ms=elapsed_ms,
            )
            return response

        return wrapped

    def wrap_tools(self, execute_tools: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(execute_tools)
        def wrapped(response: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            results = execute_tools(response, *args, **kwargs)
            elapsed_ms = (time.perf_counter() - started) * 1000

            self.store.add_step(
                self.session_id,
                self._next(),
                "tool_call",
                _tool_uses(to_jsonable(response)),
                to_jsonable(results),
                duration_ms=elapsed_ms,
            )
            return results

        return wrapped


def _tool_uses(serialized_response: JSON) -> JSON:
    content = serialized_response.get("content") if isinstance(
        serialized_response, dict
    ) else None
    if not isinstance(content, list):
        return serialized_response
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _usage(serialized_response: JSON) -> tuple[int | None, float | None]:
    """Best-effort token/cost accounting. Absent usage is fine, not an error.

    Cost is None whenever it cannot be known exactly - an unknown model, or a
    response with no usage. Never an estimate: a wrong cost is worse than a
    missing one, because you would act on it.
    """
    if not isinstance(serialized_response, dict):
        return None, None
    usage = serialized_response.get("usage")
    if not isinstance(usage, dict):
        return None, None

    tokens = None
    parts = [usage.get("input_tokens"), usage.get("output_tokens")]
    if any(isinstance(p, int) for p in parts):
        tokens = sum(p for p in parts if isinstance(p, int))
    return tokens, cost_of(serialized_response)
