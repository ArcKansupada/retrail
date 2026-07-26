"""A toy three-tool booking agent you can fork, diff, and bisect immediately.

Deliberately a raw SDK-shaped loop — a `while` loop calling the model,
executing tool calls, appending results — because that's retrail's target user.

It ships with a scripted `call_model` so you can try everything with no API key
and no spend. Swap it for the real thing and nothing else changes:

    import anthropic
    client = anthropic.Anthropic()

    def call_model(messages, tools):
        return client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            tools=tools,
            messages=messages,
        ).model_dump()

See examples/README.md for the 60-second tour.
"""

import json
import os

from retrail import record

TOOLS = [
    {
        "name": "search_flight",
        "description": "Look up the current price of a flight.",
        "input_schema": {
            "type": "object",
            "properties": {"route": {"type": "string"}},
            "required": ["route"],
        },
    },
    {
        "name": "check_budget",
        "description": "Check whether an amount is within the travel budget.",
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
    {
        "name": "book_flight",
        "description": "Book the flight and return a confirmation code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["route", "amount"],
        },
    },
]


def call_model(messages, tools=None):
    """A scripted stand-in for `client.messages.create`. No API key needed.

    It branches on what the tools return, which is what makes forking
    interesting: substitute a fact and the agent genuinely takes another path.
    """
    last = messages[-1]

    if last["role"] == "user" and isinstance(last["content"], str):
        return _reply(
            "Let me look up that flight.",
            tool=("toolu_search", "search_flight", {"route": "AUS-SFO"}),
            usage=(412, 38),
        )

    content = last["content"] if isinstance(last["content"], list) else []
    results = [b for b in content if b.get("type") == "tool_result"]
    if not results:
        raise AssertionError(f"call_model has no scripted reply for: {last}")

    latest = results[-1]
    payload = json.loads(latest["content"])

    if latest["tool_use_id"] == "toolu_search":
        # Handle a missing fare rather than assuming the schema. A real model
        # copes with a tool that returned nothing useful, and `retrail ablate`
        # relies on that: it blanks each fact in turn to see which ones matter,
        # so an agent that hard-crashes on a blank result can't be ablated.
        if "flight_price" not in payload:
            return _reply("I couldn't get a fare for that route.", usage=(520, 12))
        price = payload["flight_price"]
        if price > 500:
            return _reply(
                f"${price} looks high. Checking the budget.",
                tool=("toolu_budget", "check_budget", {"amount": price}),
                usage=(530, 41),
            )
        return _reply(
            f"${price} is within budget. Booking it.",
            tool=("toolu_book", "book_flight", {"route": "AUS-SFO", "amount": price}),
            usage=(530, 44),
        )

    if latest["tool_use_id"] == "toolu_budget":
        if payload["approved"]:
            return _reply(
                "Approved. Booking it.",
                tool=("toolu_book", "book_flight",
                      {"route": "AUS-SFO", "amount": payload["amount"]}),
                usage=(640, 30),
            )
        return _reply(
            f"That's over the ${payload['limit']} limit. I need approval before "
            "booking.",
            usage=(640, 18),
        )

    if latest["tool_use_id"] == "toolu_book":
        if "error" in payload:
            return _reply("I couldn't reach the airline to book that.", usage=(700, 12))
        return _reply(
            f"Confirmed: AUS-SFO booked for ${payload['amount']}, "
            f"reference {payload['confirmation']}.",
            usage=(700, 22),
        )

    raise AssertionError(f"call_model has no scripted reply for: {latest}")


def _reply(text, tool=None, usage=(0, 0)):
    content = [{"type": "text", "text": text}]
    if tool:
        tool_id, name, tool_input = tool
        content.append(
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        )
    return {
        "stop_reason": "tool_use" if tool else "end_turn",
        "content": content,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def execute_tools(response):
    """The tool executor. In a real agent this hits your APIs."""
    results = []
    for block in response["content"]:
        if block.get("type") != "tool_use":
            continue
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(_run(block["name"], block["input"])),
            }
        )
    return results


def _run(name, args):
    if name == "search_flight":
        return {"flight_price": 450}
    if name == "check_budget":
        return {"approved": False, "limit": 600, "amount": args["amount"]}
    if name == "book_flight":
        # Simulates the airline API being down. Set RETRAIL_DEMO_OUTAGE=1 to
        # record a failing run, then bisect it with the variable unset — the
        # outage is over, so re-execution can recover, which is exactly the
        # transient failure bisect exists to localize.
        if os.environ.get("RETRAIL_DEMO_OUTAGE"):
            return {"error": "airline API timed out"}
        return {"confirmation": "QX7R2M", "amount": args["amount"]}
    return {"error": f"unknown tool {name}"}


@record(session_name="booking-agent")
def run_agent(messages, tools=TOOLS, call_model=call_model, execute_tools=execute_tools):
    """A raw agent loop.

    Two things make this forkable, and they're the whole integration contract:

    1. `messages` is a parameter, so a fork can seed it with edited history
       instead of always starting blank.
    2. The model call and the tool executor are passed in, so @record can
       intercept them without monkey-patching the SDK.

    The defaults are what let `retrail fork --agent ...` and `retrail bisect`
    call this with only the seeded message list.
    """
    while True:
        response = call_model(messages, tools)
        messages.append({"role": "assistant", "content": response["content"]})
        if response["stop_reason"] != "tool_use":
            return response
        messages.append({"role": "user", "content": execute_tools(response)})


if __name__ == "__main__":
    result = run_agent([{"role": "user", "content": "Book me a flight from AUS to SFO."}])
    print(result["content"][-1]["text"])
    print(f"\nRecorded session {run_agent.last_session_id}")
    print(f"  retrail log {run_agent.last_session_id}")
