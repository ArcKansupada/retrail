"""The diff engine.

Sequence alignment over step signatures, per section 6.4. `difflib.Sequence
Matcher` is deliberately the starting point - Needleman-Wunsch is a later
upgrade if real trajectories turn out to need it, and there is no evidence of
that yet.

The signature is what makes the alignment meaningful: it folds in the step
type, the tool name, and a hash of the output. Two steps match only if they did
the same thing *and* got the same answer, so a shared prefix falls out as a
leading run of equal signatures and the divergence point is the first step
where the runs stop agreeing.
"""

import hashlib
from difflib import SequenceMatcher

from .serialize import canonical_json
from .trajectory import trajectory


def signature(entry):
    """A step's identity for alignment: what it did, and what came back."""
    if entry["step_type"] == "model_call":
        out = entry["output"] if isinstance(entry["output"], dict) else {}
        return f"model_call|{out.get('stop_reason')}|{_digest(out.get('content'))}"
    return f"tool_call|{','.join(tool_names(entry))}|{_digest(entry['output'])}"


def tool_names(entry):
    if not isinstance(entry["input"], list):
        return []
    return [b["name"] for b in entry["input"] if isinstance(b, dict) and b.get("name")]


def _digest(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:12]


def final_answer(entries):
    """The last thing the agent actually said."""
    for entry in reversed(entries):
        if entry["step_type"] != "model_call":
            continue
        content = entry["output"].get("content") if isinstance(entry["output"], dict) else None
        if not isinstance(content, list):
            continue
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("text")]
        if texts:
            return texts[-1]
    return None


def common_ancestor(store, a_id, b_id):
    """The nearest session both trajectories descend through."""
    a_chain = _ancestry(store, a_id)
    b_chain = set(_ancestry(store, b_id))
    for sid in a_chain:
        if sid in b_chain:
            return sid
    return None


def _ancestry(store, session_id):
    chain = []
    seen = set()
    current = session_id
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = store.get_session(current)["parent_session_id"]
    return chain


def diff(store, a_id, b_id):
    a = trajectory(store, a_id)
    b = trajectory(store, b_id)

    sig_a = [signature(e) for e in a]
    sig_b = [signature(e) for e in b]
    opcodes = SequenceMatcher(None, sig_a, sig_b, autojunk=False).get_opcodes()

    blocks = [
        {"tag": tag, "a": a[i1:i2], "b": b[j1:j2]}
        for tag, i1, i2, j1, j2 in opcodes
    ]

    # The shared prefix is only the *leading* run of equal steps. A later equal
    # block means the runs re-converged, which is interesting but is not prefix.
    shared = blocks[0]["a"] if blocks and blocks[0]["tag"] == "equal" else []
    diverged = next((blk for blk in blocks if blk["tag"] != "equal"), None)

    return {
        "a": {"id": a_id, "session": store.get_session(a_id), "steps": a},
        "b": {"id": b_id, "session": store.get_session(b_id), "steps": b},
        "common_ancestor": common_ancestor(store, a_id, b_id),
        "shared_prefix": shared,
        "divergence": _divergence(diverged),
        "blocks": blocks,
        "final": {"a": final_answer(a), "b": final_answer(b)},
        "identical": all(blk["tag"] == "equal" for blk in blocks),
    }


def _divergence(block):
    if block is None:
        return None
    first_a = block["a"][0] if block["a"] else None
    first_b = block["b"][0] if block["b"] else None
    edited = next(
        (e for e in (block["b"] or []) + (block["a"] or []) if e.get("edited")), None
    )
    return {
        "a": first_a,
        "b": first_b,
        "edit": edited["edit"] if edited else None,
        "sha": (first_a or first_b)["sha"],
    }
