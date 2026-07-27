"""Content-addressed step identity.

A step SHA hashes (session id, step number, serialized input, serialized
output), git-commit style, making a step a copy-pasteable handle: fork, show,
or diff from one string, with no session id + step number pair to carry around.

Full SHAs are stored; short ones displayed. Prefix lookups resolve like git's:
unique prefix wins, ambiguity is an error rather than a guess.
"""

from __future__ import annotations

import hashlib

from .serialize import canonical_json
from .types import JSON

SHORT = 7


def compute_sha(
    session_id: str,
    step_number: int,
    step_type: str,
    input_obj: JSON,
    output_obj: JSON,
) -> str:
    payload = canonical_json(
        {
            "session_id": session_id,
            "step_number": step_number,
            "step_type": step_type,
            "input": input_obj,
            "output": output_obj,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short(sha: str) -> str:
    return sha[:SHORT]
