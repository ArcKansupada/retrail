"""Turning recorded usage into dollars.

The rule here is the same one the rest of retrail follows: never guess. An
unknown model returns None rather than a plausible-looking number, because a
silently wrong cost is worse than a missing one - you would act on it.

Prices are USD per million tokens, and they go stale. They are a lookup table,
not a source of truth: check `retrail cost` against your actual bill before
trusting it for anything that matters.
"""

# (input, output) USD per 1M tokens. Cached 2026-06.
PRICES = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Cache reads are ~0.1x input; writes ~1.25x at the default 5-minute TTL.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def normalize(model):
    """Strip provider prefixes and dated suffixes down to a table key."""
    if not isinstance(model, str):
        return None
    name = model.strip()
    for prefix in ("anthropic.", "us.anthropic.", "eu.anthropic."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    if name in PRICES:
        return name
    # Dated snapshots (claude-haiku-4-5-20251001) price as their base model.
    for key in PRICES:
        if name.startswith(key):
            return key
    return None


def cost_of(serialized_response):
    """Cost in USD for one model call, or None if it cannot be known exactly.

    Returns None - not zero, and not an estimate - when the model is unknown or
    usage is absent. Callers must treat None as "unpriced" and say so, rather
    than summing it as free.
    """
    if not isinstance(serialized_response, dict):
        return None

    model = normalize(serialized_response.get("model"))
    usage = serialized_response.get("usage")
    if model is None or not isinstance(usage, dict):
        return None

    input_price, output_price = PRICES[model]

    def tokens(field):
        value = usage.get(field)
        return value if isinstance(value, int) else 0

    billable = (
        tokens("input_tokens")
        + tokens("cache_read_input_tokens") * CACHE_READ_MULTIPLIER
        + tokens("cache_creation_input_tokens") * CACHE_WRITE_MULTIPLIER
    )
    return (billable * input_price + tokens("output_tokens") * output_price) / 1_000_000


def cost_of_step(entry):
    """A recorded step's cost, preferring what was actually paid.

    The stored `cost_usd` is authoritative: it was computed at record time, at
    the prices in force then, and that is what the run really cost. Re-pricing
    an old trace against today's table would quietly rewrite history.

    Falling back to deriving it from the recorded output only matters for
    traces recorded before cost tracking existed, or whose model was unknown at
    the time. The response carries its own model and usage, so the number is
    still exact - just computed later.
    """
    if entry.get("step_type") != "model_call":
        return None
    stored = entry.get("cost_usd")
    if stored is not None:
        return stored
    return cost_of(entry.get("output"))


def trajectory_cost(entries):
    """(cost, unpriced_calls) over a trajectory's model calls.

    Reports how many calls could not be priced instead of hiding them, so a
    partial total is never mistaken for a complete one.
    """
    total = 0.0
    unpriced = 0
    for entry in entries:
        if entry.get("step_type") != "model_call":
            continue
        cost = cost_of_step(entry)
        if cost is None:
            unpriced += 1
        else:
            total += cost
    return total, unpriced


def fmt(cost):
    if cost is None:
        return "unpriced"
    if cost < 0.01:
        return f"${cost:.5f}"
    return f"${cost:.4f}"
