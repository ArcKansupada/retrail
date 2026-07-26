import pytest

from retrail.errors import AmbiguousSha, NotFound, ReplayIntegrityError
from retrail.serialize import canonical_json, to_jsonable
from retrail.sha import compute_sha


def test_sha_is_stable_across_dict_ordering():
    """Key order must not change a step's identity.

    Without this, the same logical step gets a different SHA on every run and
    SHA addressing is worthless.
    """
    a = compute_sha("s_1", 0, "model_call", {"b": 1, "a": 2}, {"z": 1, "y": 2})
    b = compute_sha("s_1", 0, "model_call", {"a": 2, "b": 1}, {"y": 2, "z": 1})
    assert a == b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": "s_2"},
        {"step_number": 1},
        {"step_type": "tool_call"},
        {"input_obj": {"a": 99}},
        {"output_obj": {"z": 99}},
    ],
)
def test_every_component_changes_the_sha(kwargs):
    base = dict(
        session_id="s_1",
        step_number=0,
        step_type="model_call",
        input_obj={"a": 1},
        output_obj={"z": 1},
    )
    assert compute_sha(**base) != compute_sha(**{**base, **kwargs})


def test_canonical_json_is_deterministic():
    assert canonical_json({"b": [3, 1], "a": {"d": 1, "c": 2}}) == (
        '{"a":{"c":2,"d":1},"b":[3,1]}'
    )


def test_serializer_handles_sdk_shaped_objects():
    class Pydanticish:
        def model_dump(self, mode=None):
            return {"stop_reason": "end_turn", "content": [{"type": "text"}]}

    class SdkIsh:
        def to_dict(self):
            return {"ok": True}

    assert to_jsonable(Pydanticish())["stop_reason"] == "end_turn"
    assert to_jsonable(SdkIsh()) == {"ok": True}


def test_serializer_refuses_rather_than_storing_a_lossy_shadow():
    """repr() would make the recording a description, not a recording."""

    class Opaque:
        __slots__ = ()

    with pytest.raises(ReplayIntegrityError, match="cannot serialize"):
        to_jsonable({"resp": Opaque()})


def test_serializer_refuses_cycles():
    d = {}
    d["self"] = d
    with pytest.raises(ReplayIntegrityError, match="circular"):
        to_jsonable(d)


# --- storage ---------------------------------------------------------------


def test_sha_prefix_resolves_like_git(store):
    sid = store.create_session("x")
    sha = store.add_step(sid, 0, "model_call", {"a": 1}, {"b": 2})
    assert store.resolve_sha(sha[:7]) == sha
    assert store.get_step(sha[:4])["sha"] == sha


def test_ambiguous_prefix_errors_instead_of_guessing(store):
    """§11's collision decision: error and ask for a longer prefix."""
    sid = store.create_session("x")
    shas = [store.add_step(sid, i, "model_call", {"i": i}, {}) for i in range(50)]

    # Find a 1-char prefix shared by at least two steps.
    prefix = next(
        p for p in "0123456789abcdef" if sum(s.startswith(p) for s in shas) > 1
    )
    with pytest.raises(AmbiguousSha, match="ambiguous"):
        store.resolve_sha(prefix)


def test_unknown_sha_errors(store):
    with pytest.raises(NotFound):
        store.resolve_sha("deadbeef")


def test_fork_is_a_new_row_not_a_mutation(store):
    parent = store.create_session("orig")
    sha = store.add_step(parent, 0, "tool_call", [], [])
    child = store.create_session(
        "fork", parent_session_id=parent, parent_sha=sha, forked_at_step=0
    )
    assert store.get_session(parent)["parent_session_id"] is None
    assert store.get_session(child)["parent_sha"] == sha
    assert len(store.list_sessions()) == 2
