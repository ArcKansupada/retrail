# The 60-second tour

A toy three-tool booking agent (`search_flight`, `check_budget`, `book_flight`) you can fork, diff, and bisect right now. It ships with a scripted model, so **no API key and no spend** — swap `call_model` for `client.messages.create` and nothing else changes.

```bash
pip install -e .
retrial init
```

## 1. Record a run

```bash
$ python examples/booking_agent.py
Confirmed: AUS-SFO booked for $450, reference QX7R2M.

Recorded session s_23f11ef6dd
```

No export step. The decorator logged every step as the loop ran:

```bash
$ retrial log s_23f11ef6dd
session s_23f11ef6dd  (booking-agent)
  status: complete

  578922a  step 0  model_call  (450 tok)
            1 messages in -> tool_use [text, tool_use]
  d66697c  step 1  tool_call
            ran search_flight
  9206681  step 2  model_call  (574 tok)
            3 messages in -> tool_use [text, tool_use]
  e9cc78c  step 3  tool_call
            ran book_flight
  79e97d8  step 4  model_call  (722 tok)
            5 messages in -> end_turn [text]
```

## 2. Ask a counterfactual

The flight cost $450. What would the agent have done if it cost $1200?

```bash
$ cat examples/edit_price.json
{"op": "replace", "path": "/output/0/content", "value": "{\"flight_price\": 1200}"}

$ retrial fork d66697c --agent examples.booking_agent:run_agent --edit-file examples/edit_price.json
Forked into session s_afbdbacc45
```

`d66697c` is the `search_flight` step. retrial replays everything up to it, splices in the $1200, and **re-enters your real loop** from there.

## 3. See where they diverged

```bash
$ retrial diff s_23f11ef6dd s_afbdbacc45
common ancestor: s_23f11ef6dd
shared prefix:   1 step(s), through 578922a

diverged at d66697c
  cause: replace /output/0/content = "{\"flight_price\": 1200}"

  = 1 shared step(s)
  - A  d66697c  tool_call  [live]      ran search_flight
  - A  9206681  model_call [live]      3 messages in -> tool_use [text, tool_use]
  - A  e9cc78c  tool_call  [live]      ran book_flight
  - A  79e97d8  model_call [live]      5 messages in -> end_turn [text]
  + B  d66697c* tool_call  [replayed]  ran search_flight
  + B  642238f  model_call [live]      3 messages in -> tool_use [text, tool_use]
  + B  ee3f788  tool_call  [live]      ran check_budget
  + B  ac8126b  model_call [live]      5 messages in -> end_turn [text]

final answer
  A  Confirmed: AUS-SFO booked for $450, reference QX7R2M.
  B  That's over the $600 limit. I need approval before booking.
```

**This is the part a log viewer cannot do.** The fork called `check_budget` — a tool the original run never touched. Branching stored JSON can only ever replay steps that already happened; the agent genuinely re-decided and took a path that never existed.

## 4. Localize a failure automatically

Record a run while the airline API is "down":

```bash
$ RETRIAL_DEMO_OUTAGE=1 python examples/booking_agent.py
I couldn't reach the airline to book that.

Recorded session s_cc6b0dc420
```

Now the outage is over. Which step doomed that run?

```bash
$ retrial bisect s_cc6b0dc420 --check "output contains 'Confirmed'" \
    --agent examples.booking_agent:run_agent

bisecting s_cc6b0dc420 against: output contains 'Confirmed'

  probe step 2 (126fdf9): recovered     -> 'Confirmed: AUS-SFO booked for $450, reference QX7R2M.'
  probe step 3 (6becb65): still broken  -> "I couldn't reach the airline to book that."

2 re-execution(s) over 5 candidate step(s).

First step that could not recover: 6becb65
  step 3  tool_call
  ran book_flight
```

Bisect forks from a step, re-runs the agent for real, and checks whether the failure still happens. Resuming from step 2 the tool gets called again — the outage is over, so it **recovers**. Resuming from step 3 replays the recorded timeout, so it stays broken. The boundary is the culprit.

Each probe is a real re-execution recorded as its own session, so you can `retrial log` any of them. Two probes localized it across five steps.

`--check` describes what a **good** run looks like, like a test. Also available: `output not contains '...'`, `output matches '<regex>'`.

## 5. Ask which facts actually mattered

Bisect explains a run that **failed**. Ablation explains a run that **worked**: which of the recorded facts was the answer load-bearing on?

```bash
$ retrial ablate s_e8dda5f4bd --check "output contains 'QX7R2M'" \
    --agent examples.booking_agent:run_agent

  step 1 (ff9b57a) search_flight: outcome FLIPPED   -> possibly load-bearing
  step 3 (6ce60a4) book_flight:   outcome FLIPPED   -> possibly load-bearing
```

It blanks each tool result in turn and re-executes. A tool that *ran* isn't a tool that *mattered* — and the only way to know which is to remove the fact and watch what the agent does. (Both facts matter here, which is the boring correct answer for a three-step agent. See `tests/test_explore.py` for an agent with a tool that runs but is provably *not* load-bearing.)

Note that ablation only works on an agent that copes with a tool returning nothing useful. One that assumes its tools' schema and hard-crashes on a blank result can't be ablated — you'll get `could not probe (KeyError: ...)`, which is retrial telling you your agent has no fallback, not retrial failing.

**The signal is asymmetric, and the output says so.** "Not load-bearing" is sound: the run reached the same outcome without that fact. "Possibly load-bearing" is weaker — the agent may be reacting to the perturbation itself rather than the value it lost. Ablation rules facts out rigorously, in suggestively.

Ablate and bisect are duals, and each refuses the other's job — ablate against a failing run tells you to use bisect, and vice versa.

## 6. Find the threshold

```bash
$ cat fares.json
["{\"flight_price\": 200}", "{\"flight_price\": 450}",
 "{\"flight_price\": 550}", "{\"flight_price\": 1400}"]

$ retrial sweep ff9b57a --values-file fares.json --check "output contains 'QX7R2M'" \
    --agent examples.booking_agent:run_agent

  {"flight_price": 200}:  PASS -> 'Confirmed: AUS-SFO booked for $200, reference QX7R2M.'
  {"flight_price": 450}:  PASS -> 'Confirmed: AUS-SFO booked for $450, reference QX7R2M.'
  {"flight_price": 550}:  FAIL -> "That's over the $600 limit. I need approval before booking."
  {"flight_price": 1400}: FAIL -> "That's over the $600 limit. I need approval before booking."

Threshold: the check flips between {"flight_price": 450} and {"flight_price": 550}
```

Sweep found the agent's real rule, which is **not** the $600 the answer text mentions — that's `check_budget`'s limit. The model escalates to a budget check above $500 and never books after that. The behavior and the explanation disagree, and only re-execution surfaces which one is true.

Against the live model (`examples/live_booking_agent.py`) the same command locates Opus 4.8's actual $600 boundary: the check flips between $550 and $650.

Bisect searches over **resume points**, ablate over **steps**, sweep over **values**. Same machinery each time: fork plus a check.

## What makes this agent forkable

Two things, and they're the whole integration contract:

```python
@record(session_name="booking-agent")
def run_agent(messages, tools=TOOLS, call_model=call_model, execute_tools=execute_tools):
    while True:
        response = call_model(messages, tools)
        messages.append({"role": "assistant", "content": response["content"]})
        if response["stop_reason"] != "tool_use":
            return response
        messages.append({"role": "user", "content": execute_tools(response)})
```

1. **`messages` is a parameter**, not a blank list built inside the function. That's what lets a fork seed your loop with edited history instead of starting over. It's the one non-negotiable convention.
2. **The model call and tool executor are passed in**, so `@record` can intercept them without monkey-patching your SDK client.

The defaults are what let `retrial fork` and `retrial bisect` call your agent with just the seeded history.

## Going live

Replace the scripted `call_model`:

```python
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
```

Everything above works identically. Note that fork and bisect then cost real tokens — bisect roughly `log2(steps)` re-executions, times `--samples`.

## Going live on something other than Anthropic

An adapter is the whole change — the loop, the tools, and every command above stay as they are:

```python
from retrial import openai_adapter, gemini_adapter

call_model = openai_adapter(model="gpt-5")
call_model = gemini_adapter(model="gemini-2.5-pro")
```

`local_model_agent.py` in this directory is this same booking agent pointed at a model on your own machine, which keeps fork and bisect free:

```bash
ollama serve && ollama pull llama3.1
pip install 'retrial[openai]'
python examples/local_model_agent.py
```

Any OpenAI-compatible server works the same way — vLLM, llama.cpp, LM Studio, OpenRouter — by changing `base_url`. Set `RETRIAL_LOCAL_MODEL` and `RETRIAL_LOCAL_BASE_URL` to redirect it without editing the file.
