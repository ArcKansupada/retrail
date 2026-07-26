"""Content-addressed step identity.

A step SHA hashes (session id, step number, serialized input, serialized
output) - git-commit style. That makes a step a copy-pasteable handle: you can
fork, show, or diff from a single string with no session id + step number pair
to carry around.

Full SHAs are stored; short ones are displayed. Prefix lookups resolve like
git's: unique prefix wins, ambiguity is an error rather than a guess.
"""

import hashlib

from .serialize import canonical_json

SHORT = 7


def compute_sha(session_id, step_number, step_type, input_obj, output_obj):
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


def short(sha):
    return sha[:SHORT]
