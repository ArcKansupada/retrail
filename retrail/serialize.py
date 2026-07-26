"""Turning live objects into stable JSON.

Two requirements pull in the same direction here:

1. Step SHAs hash the serialized input/output, so serialization must be
   deterministic - same object, same bytes, forever.
2. A fork replays recorded state back into a live loop, so serialization must
   round-trip losslessly for anything we intend to replay.

Where (2) can't be met we say so loudly rather than storing a lossy shadow of
the object and pretending the replay is faithful.
"""

import dataclasses
import json

from .errors import ReplayIntegrityError

_PRIMITIVES = (str, int, float, bool, type(None))


def to_jsonable(obj, _path="", _seen=None):
    """Convert an arbitrary object into JSON-safe primitives.

    Handles the shapes SDK response objects actually take: Pydantic models
    (`model_dump`), plain SDK objects (`to_dict`), dataclasses, namedtuples,
    and ordinary containers.
    """
    if _seen is None:
        _seen = set()

    if isinstance(obj, _PRIMITIVES):
        return obj

    # Cycles would otherwise recurse forever. A cyclic message history is not
    # something we can replay, so refuse.
    marker = id(obj)
    if marker in _seen:
        raise ReplayIntegrityError(
            f"circular reference at {_path or '<root>'}; cannot record a state "
            "that references itself"
        )
    _seen = _seen | {marker}

    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ReplayIntegrityError(
                    f"non-string dict key {key!r} at {_path or '<root>'}; JSON "
                    "cannot represent it, so the replay would not round-trip"
                )
            out[key] = to_jsonable(value, f"{_path}/{key}", _seen)
        return out

    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v, f"{_path}/{i}", _seen) for i, v in enumerate(obj)]

    # Pydantic v2 (the Anthropic SDK's response models).
    if hasattr(obj, "model_dump"):
        return to_jsonable(obj.model_dump(mode="json"), _path, _seen)

    # SDK objects exposing a dict view.
    if hasattr(obj, "to_dict"):
        return to_jsonable(obj.to_dict(), _path, _seen)

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj), _path, _seen)

    if hasattr(obj, "_asdict"):  # namedtuple
        return to_jsonable(obj._asdict(), _path, _seen)

    if hasattr(obj, "__dict__"):
        public = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        if public:
            return to_jsonable(public, _path, _seen)

    raise ReplayIntegrityError(
        f"cannot serialize {type(obj).__name__} at {_path or '<root>'}. retrail "
        "records exact state so replays are faithful; storing repr() here would "
        "make the recording a description rather than a recording."
    )


def canonical_json(obj):
    """Deterministic JSON. Feeds both storage and step SHAs.

    `sort_keys` is what makes a SHA stable across runs - without it, dict
    ordering would give the same logical step a different SHA.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
