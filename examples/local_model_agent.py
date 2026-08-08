"""The booking agent, against a model running on your own machine.

Same loop as `booking_agent.py` and `live_booking_agent.py`. Only the adapter
line changes, and nothing downstream notices - the run records, forks, diffs,
and bisects exactly as an Anthropic run does. No API key, no spend.

Setup (Ollama, but any OpenAI-compatible server works the same way):

    ollama serve && ollama pull llama3.1
    pip install 'retrial[openai]'
    python examples/local_model_agent.py

Then fork a tool result and watch the local model decide differently:

    retrial fork <sha> --edit examples/edit_price.json \
        --agent examples.local_model_agent:run_agent

Point it elsewhere with RETRIAL_LOCAL_MODEL / RETRIAL_LOCAL_BASE_URL:
vLLM :8000/v1, llama.cpp :8080/v1, LM Studio :1234/v1, or OpenRouter.

A word on small models: the fork machinery is exact, but a 7B model's tool
calling is not. A run that ends without calling a tool is the model, not
retrial - try a larger one before debugging the trace.
"""

import json
import os

from retrial import openai_adapter, record, tool_result, tool_uses
from retrial.pricing import FREE, register_prices

MODEL = os.environ.get("RETRIAL_LOCAL_MODEL", "llama3.1")
BASE_URL = os.environ.get("RETRIAL_LOCAL_BASE_URL", "http://localhost:11434/v1")

# It runs on your hardware, so $0.00000 is a fact worth recording. Without
# this the model is simply unknown, and retrial reports `unpriced`.
register_prices({MODEL: FREE})

SYSTEM = (
    "You are a flight booking assistant. Use the tools available to you. "
    "Search for the flight first, then book it only if the fare is at or "
    "under the traveller's $600 budget. If it is over budget, do not book - "
    "say so and stop."
)

# Declared once, in canonical form. The adapter translates them into whatever
# schema the server expects, so switching providers never edits this list.
TOOLS = [
    {
        "name": "search_flight",
        "description": "Look up the current fare for a route.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Origin airport code"},
                "destination": {"type": "string", "description": "Destination code"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "book_flight",
        "description": "Book a flight and return a confirmation code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "fare_usd": {"type": "number"},
            },
            "required": ["origin", "destination", "fare_usd"],
        },
    },
]

# Safe at import: the adapter builds its client on first call, so `--help`
# never needs a server to be running.
call_model = openai_adapter(
    model=MODEL,
    base_url=BASE_URL,
    api_key="unused",  # local servers ignore it; the SDK insists on one
    system=SYSTEM,
)


def execute_tools(response):
    """`tool_uses` reads live responses and replayed dicts alike."""
    return [
        tool_result(block["id"], _run(block["name"], block["input"]))
        for block in tool_uses(response)
    ]


def _run(name, args):
    if name == "search_flight":
        return {"fare_usd": 450, "carrier": "Southwest", "stops": 0}
    if name == "book_flight":
        return {"confirmation": "QX7R2M", "fare_usd": args.get("fare_usd")}
    return {"error": f"unknown tool {name}"}


@record(session_name="local-booking")
def run_agent(messages, tools=TOOLS, call_model=call_model, execute_tools=execute_tools):
    """The same raw loop as every other example. Nothing here is provider-aware."""
    while True:
        response = call_model(messages, tools)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return response
        messages.append({"role": "user", "content": execute_tools(response)})


if __name__ == "__main__":
    from retrial.cli import echo

    echo(f"Calling {MODEL} at {BASE_URL}")
    try:
        result = run_agent([{"role": "user", "content": "Book me a flight from AUS to SFO."}])
    except Exception as exc:  # noqa: BLE001 - the useful failure is "no server"
        echo(f"\nThe call failed: {exc}")
        echo(f"Is a server running at {BASE_URL}? For Ollama: `ollama serve`.")
        raise SystemExit(1) from exc

    # Not `print`: a model answers with emoji, which a bare print dies on
    # under a cp1252 console. echo() degrades the glyph instead of the run.
    echo(f"\n{result.text}")
    echo(f"\nRecorded session {run_agent.last_session_id}")
    echo(f"  retrial log {run_agent.last_session_id}")
    echo(f"  retrial cost {run_agent.last_session_id}    # $0.00000, and true")
    echo(json.dumps({"model": result.model, "usage": result.usage}, indent=2))
