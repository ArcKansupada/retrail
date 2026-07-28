"""Async agents are refused, because recording one would lie.

A sync wrapper cannot record an `async def`. Calling it only builds a
coroutine: the body has not run, so the session is stamped `complete` with zero
steps, and the `except BaseException` that marks a crashed run `failed` never
sees the failure - it happens later, inside the event loop. The result is a run
that raised partway through, stored as a successful one.

Worse than a crash: every other retrial failure is loud, but this one leaves a
trace that quietly disagrees with what happened. So it is refused at decoration
time, before any of it can occur.
"""

import asyncio

import pytest
from conftest import TOOLS, fake_model, make_executor, raw_agent

from retrial import record
from retrial.errors import IntegrationError


def test_decorating_an_async_agent_is_refused(store):
    with pytest.raises(IntegrationError) as excinfo:

        @record(session_name="async-agent", store=store)
        async def agent(messages, tools, call_model, execute_tools):
            return None

    assert "async" in str(excinfo.value)
    # Point at the fix, not just the problem.
    assert "asyncio.run" in str(excinfo.value)


def test_the_refusal_happens_before_a_session_exists(store):
    """Decoration-time, so an import fails - not a run that half-recorded."""
    with pytest.raises(IntegrationError):

        @record(session_name="async-agent", store=store)
        async def agent(messages, tools, call_model, execute_tools):
            return None

    assert store.list_sessions() == []


def test_an_async_call_model_is_refused(store, opening):
    """The sync-agent variant: it would record an un-awaited coroutine."""

    async def async_model(messages, tools):
        return None

    agent = record(session_name="sync-agent", store=store)(raw_agent)
    with pytest.raises(IntegrationError) as excinfo:
        agent(opening, TOOLS, async_model, make_executor(450))

    assert "async" in str(excinfo.value)
    assert "call_model" in str(excinfo.value)


def test_an_async_execute_tools_is_refused(store, opening):
    async def async_tools(response):
        return []

    agent = record(session_name="sync-agent", store=store)(raw_agent)
    with pytest.raises(IntegrationError) as excinfo:
        agent(opening, TOOLS, fake_model, async_tools)

    assert "async" in str(excinfo.value)
    assert "execute_tools" in str(excinfo.value)


def test_refusing_an_async_call_model_leaves_no_session_behind(store, opening):
    """Same rule as the not-callable check: reject before a row exists."""

    async def async_model(messages, tools):
        return None

    agent = record(session_name="sync-agent", store=store)(raw_agent)
    with pytest.raises(IntegrationError):
        agent(opening, TOOLS, async_model, make_executor(450))

    assert store.list_sessions() == []


def test_an_async_callable_object_is_refused_too(store, opening):
    """`async def __call__` is a shape real SDK clients ship, and such an object
    is not a coroutine function - only its `__call__` is."""

    class AsyncClient:
        async def __call__(self, messages, tools):
            return None

    agent = record(session_name="sync-agent", store=store)(raw_agent)
    with pytest.raises(IntegrationError) as excinfo:
        agent(opening, TOOLS, AsyncClient(), make_executor(450))

    assert "async" in str(excinfo.value)


def test_a_sync_agent_is_unaffected(store, opening):
    """The guard must not cost the supported case anything."""
    agent = record(session_name="sync-agent", store=store)(raw_agent)
    agent(opening, TOOLS, fake_model, make_executor(450))

    sessions = store.list_sessions()
    assert [s["status"] for s in sessions] == ["complete"]
    assert store.steps_for(sessions[0]["id"])


def test_the_wrapper_never_returns_a_coroutine(store, opening):
    """The property the refusal protects, stated directly. If it fails, a
    session is being marked complete before the work it describes happened."""
    agent = record(session_name="sync-agent", store=store)(raw_agent)
    result = agent(opening, TOOLS, fake_model, make_executor(450))
    assert not asyncio.iscoroutine(result)
