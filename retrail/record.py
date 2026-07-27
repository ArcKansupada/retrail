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
import threading
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


def _is_async(obj: object) -> bool:
    """True for anything that returns a coroutine when called.

    `iscoroutinefunction` already sees through `functools.partial`, but not
    through a class with an `async def __call__` - a shape real SDK clients do
    use. Checking both keeps the refusal honest rather than merely typical.
    """
    if inspect.iscoroutinefunction(obj):
        return True
    if not callable(obj) or inspect.isroutine(obj):
        return False
    # Being callable is exactly what guarantees type(obj).__call__ exists, so
    # this cannot raise. It is the object's own __call__ we need to inspect -
    # `callable()` answers a different question and would miss this entirely.
    return inspect.iscoroutinefunction(type(obj).__call__)

class _Pending(threading.local):
    """Handoff from fork() to the decorator, per thread.

    Dict-shaped because that is how fork() uses it, but thread-local because
    the value is a *destination*: it says which session the very next recorded
    run belongs to. A plain module dict makes that a race with a silent wrong
    answer - two concurrent forks, and the second overwrites the first, so one
    agent's steps are recorded into the other fork's session. Wrong provenance
    is worse than a crash here: the trace looks fine and describes a run that
    never happened. `threading.local` makes it structurally impossible, since
    fork() sets it and the agent it calls consumes it on the same thread.
    """

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def __setitem__(self, key: str, value: Any) -> None:
        self._values[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        return self._values.pop(key, default)


# Set by fork() so a re-executed run records into the fork's session (with its
# parent provenance) instead of minting a fresh root session.
_pending = _Pending()

# One Store per database per process, shared across threads - Store itself is
# thread-safe. Without this, every recorded run opens a new sqlite connection
# and never closes it.
_default_stores: dict[str, Store] = {}

# Guards the cache, not the stores. `if path not in cache: cache[path] = Store()`
# is two operations: every thread can pass the test, every thread opens a
# connection, and each gets back whichever object happened to land in the dict
# last. Measured with 8 threads: 8 connections opened, 7 orphaned, and callers
# holding stores that are not the cached one.
_stores_lock = threading.Lock()


def _default_store() -> Store:
    from .storage import resolve_db_path

    # Searches upward, so an agent launched from a subdirectory of the project
    # records into the project's store - the same one the CLI will find.
    path = resolve_db_path()
    with _stores_lock:
        store = _default_stores.get(path)
        if store is None:
            store = _default_stores[path] = Store(path)
        return store


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
        # Refuse async at decoration time, so the error arrives when the module
        # is imported rather than after a run that appeared to work.
        #
        # A sync wrapper around an `async def` does not record it - calling the
        # function only builds a coroutine, so `fn(...)` returns before the body
        # has executed a single step. The session is stamped `complete`
        # immediately, and the `except BaseException` that marks a crashed run
        # `failed` never sees anything, because the body runs later inside the
        # event loop. A run that raised partway through is stored as a
        # successful one. That is the worst failure this project can have: not a
        # crash, but a recording that quietly disagrees with what happened.
        if _is_async(fn):
            raise IntegrationError(
                f"@record cannot record {getattr(fn, '__name__', fn)!r} because it is "
                "an async function, and retrail does not support async agents yet. "
                "Calling an async function returns a coroutine without running its "
                "body, so retrail would mark the session complete before your loop "
                "had taken a step - and a run that later failed would be recorded as "
                "a successful one. Wrap the loop in a synchronous function (for "
                "example, one that calls asyncio.run) and decorate that instead."
            )

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
                # An async call_model or execute_tools inside a sync agent fails
                # too, just further away: the wrapper never awaits it, so what
                # gets recorded is a coroutine object, and the complaint arrives
                # from the serializer as "cannot serialize coroutine" - true, but
                # silent about the actual cause. Name it here instead.
                if _is_async(bound.arguments[name]):
                    raise IntegrationError(
                        f"{fn.__name__}() got an async {name}, which retrail cannot "
                        "record yet. retrail calls it and records what comes back; "
                        "for a coroutine function that is an un-awaited coroutine, "
                        "not the model's response. Give retrail a synchronous "
                        f"{name} (for example one that calls asyncio.run internally) "
                        "so there is a real response to record."
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

        # Step numbers are allocated by add_step(None, ...) inside the store's
        # lock, rather than read here and passed in - see Store.add_step.

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
                None,
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
                None,
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
