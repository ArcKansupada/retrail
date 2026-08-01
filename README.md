# retrial

**Git for agent trajectories.** Branch, diff, and bisect LLM agent runs — backed by real re-execution, not static logs.

When an agent does something wrong, the only debugging tool most people have is scrolling through logs. retrial lets you go back to step N, change one fact — a tool result, a retrieved doc, an intermediate decision — and see what your agent *actually would have done differently* from there. With the real model. Not a guess.

```bash
retrial fork a1b2c3d --agent myapp.agent:run_agent --edit-file edit.json
retrial diff s_a8d4f64945 s_3f9c02ab1e
retrial bisect s_cc6b0dc420 --check "output contains 'confirmed'" --agent myapp.agent:run_agent
```

## Why this is different

- **Real re-execution, not log branching.** Forking re-enters your live agent loop and makes real model calls from the fork point onward. It does not relabel stored JSON and call it a branch.
- **Zero-export integration.** A decorator, not a manual JSON schema to hand-build.
- **Narrow and deep.** One thing — branch/diff/bisect via real execution — done well.
- **Python-native**, local-first. No account, no telemetry, no dashboard. One SQLite file in `.retrial/`.

## Status

Early but complete end to end: record, fork, diff, and bisect all work, and all four are **validated against `claude-opus-4-8`**, not just a stand-in. Forking a real \$450 booking with a \$1,450 fare spliced in makes Opus decline to book, explain the budget breach, and never call `book_flight` — a path the original never took. See [the design doc](https://github.com/ArcKansupada/retrial/blob/main/retrial-design-doc.md).

**Fastest way in:** the [60-second tour](https://github.com/ArcKansupada/retrial/blob/main/examples/README.md) — a toy booking agent you can fork, diff, and bisect with **no API key**.

```bash
pytest              # 335 offline tests, deterministic and free
pytest -m live      # 8 tests against the real API (costs ~$0.30)
```

## Install

```bash
pip install -e .
```

## Integrate

retrial needs two integration points from your loop: the function that calls the model, and the function that executes tool calls. You pass both in explicitly — there's no monkey-patching of your SDK client, so every recorded step traces back to a line you wrote.

```python
from retrial import record

@record(session_name="booking-agent")
def run_agent(messages, tools=TOOLS, call_model=call_model, execute_tools=execute_tools):
    while True:
        response = call_model(messages, tools)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return response
        messages.append({"role": "user", "content": execute_tools(response)})
```

**The one non-negotiable convention: `messages` must be a parameter**, not a blank list created inside the function. That's what lets a fork seed your loop with edited history and get genuine re-execution instead of a fresh run. Everything else is a normal Python function.

Give the other parameters defaults if you want to fork from the CLI — that's what lets `retrial fork` call your agent with just the seeded history.

Then run it as usual. Steps are logged as the loop runs; there is no export step.

**The loop must be synchronous.** `@record` refuses an `async def` agent, and refuses an async `call_model` or `execute_tools`, with an error explaining why rather than recording something untrue: calling an async function returns a coroutine without running its body, so retrial would mark the session complete before your loop had taken a step, and a run that later failed would be stored as a successful one. If your loop is async today, wrap it in a synchronous function that calls `asyncio.run` and decorate that. An async-aware recorder is planned.

## Fork

Every step gets a content-hash SHA, so you address one by a short, copy-pasteable string — the same UX as git's short hashes, prefix matching included.

```bash
$ retrial log s_a8d4f64945
session s_a8d4f64945  (booking-agent)
  status: complete

  4f0c1e2  step 0  model_call  (312ms, 450 tok)
            1 messages in -> tool_use [text, tool_use]
  a1b2c3d  step 1  tool_call  (41ms)
            ran search_flight
  9e77b10  step 2  model_call  (288ms, 544 tok)
            3 messages in -> end_turn [text]
```

What if that flight had cost \$1200 instead of \$450?

```bash
$ cat edit.json
{"op": "replace", "path": "/output/0/content", "value": "{\"flight_price\": 1200}"}

$ retrial fork a1b2c3d --agent examples.booking_agent:run_agent --edit-file edit.json
Forked into session s_3f9c02ab1e
```

The fork resumes your real loop from that point with the edited fact spliced in. The model decides again, and can take a path the original never took:

```bash
$ retrial list
s_a8d4f64945  booking-agent  (3 steps, complete)
└── s_3f9c02ab1e  booking-agent-fork  (3 steps, complete)  forked from a1b2c3d [replace /output/0/content = "{\"flight_price\": 1200}"]
```

The original session is never mutated — a fork is a new session row, so you can branch off the same step as many times as you like and compare the results.

### Editing

The `edit` is a JSON patch, which is what lets `retrial log` show not just *where* you forked but *what* you changed. Two forks from the same step with opposite edits are otherwise indistinguishable.

For edits that depend on the recorded content — double the price, truncate a document — the Python API also takes a callback:

```python
from retrial import fork

fork(
    from_sha="a1b2c3d",
    agent=run_agent,
    edit=lambda step: {**step, "output": [{**step["output"][0], "content": "..."}]},
)
```

Callback forks work and are recorded, but they don't round-trip from the record alone — retrial stores that a callback ran, not what it did. Prefer a patch when you can.

## Diff

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

The fork called `check_budget`, a tool the original never touched — the kind of divergence only real re-execution can produce.

Steps are aligned on a signature folding in the step type, the tool name, and a hash of the output, so the shared prefix and the divergence point fall out of the alignment rather than being asserted. `[replayed]` vs `[live]` is not a heuristic: a fork stores only the steps it actually re-executed, so a step's origin is a fact about which session it lives in.

## Bisect

Which step made a failure inevitable?

```bash
$ retrial bisect s_cc6b0dc420 --check "output contains 'Confirmed'" \
    --agent examples.booking_agent:run_agent

  probe step 2 (126fdf9): recovered     -> 'Confirmed: AUS-SFO booked for $450...'
  probe step 3 (6becb65): still broken  -> "I couldn't reach the airline to book that."

2 re-execution(s) over 5 candidate step(s).

First step that could not recover: 6becb65
  step 3  tool_call
  ran book_flight
```

Each probe forks the run and re-executes it for real, then checks the outcome. Resuming from step 2, the agent calls the tool again and recovers; from step 3 the recorded failure is replayed and it stays broken. The boundary is the culprit.

`--check` describes what a **good** run looks like, like a test. Also: `output not contains '...'`, `output matches '<regex>'`, or any callable via the Python API.

Bisect costs roughly `log2(steps)` real re-executions. Binary search assumes the good/bad boundary is clean, which a non-deterministic model doesn't strictly guarantee — so every probe is recorded as its own session you can `retrial log`, and `--samples N` re-probes each step and requires unanimity before calling it recovered.

## Ablate

Which recorded facts is a good answer actually load-bearing on? Don't infer it from the text — **perturb each fact and find out**.

```bash
$ retrial ablate s_a8d4... --check "output contains 'QX7R2M'" --agent myapp.agent:run_agent

  step 1 (eec0274) search_flight: outcome FLIPPED   -> possibly load-bearing
  step 3 (0725654) book_flight:   outcome FLIPPED   -> possibly load-bearing
  step 5 (a91cf30) check_budget:  outcome held      -> not load-bearing
```

`check_budget` ran, but the answer never depended on it. That distinction — a tool that *ran* versus a tool that *mattered* — is causal, not heuristic, and it's only available because re-execution is real.

**The signal is asymmetric, and the output says so.** "Not load-bearing" is a sound conclusion: the run reached the same outcome without that fact. "Possibly load-bearing" is weaker — the agent may be reacting to the perturbation itself rather than to the value it lost. Ablation rules facts out rigorously and rules them in suggestively.

**Ablate and bisect are duals**, and each refuses the other's job. Bisect is for a run that *failed* (which step doomed it?) and refuses if the check already passes. Ablate is for a run that *worked* (which facts did it need?) and refuses if the check doesn't pass — pointing you at bisect.

## Sweep

Fork one step across N values to find a threshold.

```bash
$ retrial sweep eec0274 --values-file fares.json --check "output contains 'QX7R2M'" \
    --agent myapp.agent:run_agent

  {"fare_usd": 200}:  PASS -> 'Your flight is booked! ... $200 (well under your $600 budget)'
  {"fare_usd": 550}:  PASS -> 'Your flight is booked! ... $550 (under your $600 budget)'
  {"fare_usd": 650}:  FAIL -> "The fare of $650 exceeds your $600 limit by $50, so I won't book it."
  {"fare_usd": 1400}: FAIL -> "Since the fare of $1,400 exceeds your $600 budget, I won't book it."

Threshold: the check flips between {"fare_usd": 550} and {"fare_usd": 650}
```

That's a real decision boundary in a live model's behavior, found by re-execution rather than by reading the prompt.

Bisect searches over *resume points*; sweep searches over *values*; ablate searches over *steps*. Same machinery each time — fork plus a check.

### Why both compare on a check, never on answer text

A real model rewords itself on every run. Comparing answers by text equality would flag every step as load-bearing no matter what you perturbed — it would be measuring non-determinism, not causation. The check collapses the answer to a stable predicate.

## Rerun — your recorded runs are the regression suite

Nobody writes the test cases. They're just what your agent already did. Edit a prompt or swap a model, then re-execute every recorded run against your **current code**:

```bash
$ retrial rerun --check "output contains 'QX7R2M'" --at-tool search_flight \
    --agent myapp.agent:run_agent

re-executing 40 recorded run(s) against current code
  resuming at: the 'search_flight' tool call

  s_3ca5a1df75  REGRESSED      1 call(s)
  ...

40 run(s), 40 model call(s), $0.31
  still passing : 37
  REGRESSED     : 3

REGRESSED  s_3ca5a1df75  (resumed at eec0274)
  was: Your flight is booked! Confirmation code: QX7R2M
  now: The fare of $450 exceeds your $300 limit, so I won't book it.
  retrial diff s_3ca5a1df75 s_9c1e04b7a2
```

Exits non-zero on a regression, so CI fails without extra plumbing.

### Where you resume decides what the test is worth

This is the part that matters, and there's no option that's both cheap and universally right:

| | cost | what it tests |
|---|---|---|
| `--from first` *(default)* | full trajectory | does the new config handle this case at all |
| `--at-tool NAME` | the steps after NAME | exactly the decision that follows that fact |
| `--from last` | ~1 model call | **usually nothing** — see below |

`--from last` is a trap, and it's measured, not theoretical. Against live Opus 4.8 with a budget prompt tightened from \$600 to \$300:

```
--from last              -> regressions: 0  |  1 call  $0.0106   <- misses it
--at-tool search_flight  -> regressions: 1  |  1 call  $0.0077   <- catches it, cheaper
--from first             -> regressions: 1  |  2 calls $0.0129   <- catches it, dearer
```

Resuming at the last tool call lands on `book_flight` — by then the booking already happened and its confirmation sits in the replayed history, so the model just reports a code the new config would never have produced. **A cheap test that quietly tests nothing is worse than a slow one**, which is why `first` is the default.

`--at-tool` is the option that earns its keep: it beats the full re-run on price *and* catches the regression, because it resumes exactly where the decision under test lives. The economics are real, but you have to know which decision you're testing.

## Cost

```bash
$ retrial cost s_3ca5a1df75
  step  sha           ms   tokens        cost
     0  ad8e658     2745      713    $0.00562
     2  e0bf5d9     2028      872    $0.00686
     4  0276998     2615     1007    $0.00713

  total: $0.0196
```

An unknown model prices as `unpriced`, never as a guess — and a partial total is reported as partial rather than summed as though the missing calls were free. A silently wrong cost is worse than a missing one, because you'd act on it.

`ablate` joins cost to causation, which is the pairing nothing else can do: **"this tool call burns 900 tokens every run and provably does not change the outcome."** That's a deletion recommendation backed by evidence, in dollars.

## What isn't here, deliberately

**Merge was cut, not deferred.** This is where the git metaphor stops paying rent. Git merges because branches are *work that must be combined* — the merged artifact is what ships. retrial's branches are *questions being asked*, and two forks are usually competing hypotheses of which at most one is true. Merging "the fare was \$450" with "the fare was \$1,450" feeds the model contradictory facts: not a better-informed run, a confused one. Answers don't merge. Full reasoning in [the design doc](https://github.com/ArcKansupada/retrial/blob/main/retrial-design-doc.md) §6.6.

**Blame-by-attribution was cut too** — replaced by `ablate`, which answers the same question causally instead of heuristically.

## What retrial refuses to do

The whole product rests on the replay being exactly what happened, so retrial raises rather than guesses when it can't verify that:

- If your loop **transforms a tool result** before appending it to the history, retrial refuses to fork that step — its patch would land on a value you never saw.
- If an edit **invents or drops** a tool result the original run never produced, it refuses — there's no honest place in the recorded history to put it.
- If a run **crashed mid-loop**, its trailing tool call can't be forked: the message state after it was never observed.
- If your agent or its model call is **async**, it refuses to record at all rather than stamp a session `complete` before the loop has run a step.
- If the database was written by a **newer retrial**, it refuses to open it rather than write rows that version may not read back.
- If an imported step's **content doesn't match its SHA**, it refuses — a trace that changed in transit is not a recording. Same for a file that would **merge two different runs** under one session ID.

A wrong replay would be worse than no replay.

## CLI

```
retrial init                                   # create .retrial/ + sqlite db
retrial list                                   # sessions, tree view
retrial log <session-id>                       # step-by-step history, SHA per step
retrial show <sha>                             # full detail on one step
retrial fork <sha> --agent M:F --edit-file e.json
retrial diff <session-a> <session-b>           # --full to expand shared steps
retrial bisect <session-id> --check EXPR --agent M:F   # which step doomed a failed run
retrial ablate <session-id> --check EXPR --agent M:F   # which facts a good run needed
retrial sweep <sha> --values-file v.json --agent M:F   # find a threshold; --check optional
retrial rerun --check EXPR --agent M:F                 # regression-test recorded runs
retrial cost <session-id>                              # cost/token breakdown per step
retrial export <session-id> > trace.jsonl             # a session + its ancestors, portable
retrial import trace.jsonl                            # read one back, all of it or none
```

SHA prefix matching applies throughout. An ambiguous prefix is an error asking for a longer one — same as git.

### Where the store lives

Commands search upward for `.retrial/` from the current directory, so running one deep inside a project finds the project's store rather than making a new one beside you — the same rule git uses to find `.git/`. In precedence order:

1. `--db PATH` on the command line.
2. `RETRIAL_DB` in the environment, for pointing a shell or a CI job at one store.
3. The nearest `.retrial/sessions.db` at or above the current directory.
4. Otherwise `./.retrial/sessions.db`, created on first use.

`retrial init` is the exception: like `git init`, it always creates a store *here*. If that shadows one further up, it says so.

### Sharing a trace

`retrial export` writes a session and its ancestors to a JSONL file; `retrial import` reads one into another store. This is the collaboration story with no server in it — *here is my trace, fork it yourself and see* — and the only lossless way to keep a session before deleting `.retrial/`.

```bash
retrial export s_ab12cd34ef > trace.jsonl   # the fork plus every ancestor it needs
retrial export --all -o backup.jsonl        # the whole store
cat trace.jsonl | retrial import -           # from a pipe, or a path
```

A fork travels with its ancestors, because a fork without its parents can't be diffed or replayed. Descendants don't travel — exporting a root doesn't hand over every experiment you ran on top of it.

Import is safe to repeat. Every step's SHA is recomputed and checked, so a file altered in transit is refused rather than trusted; sessions already present with identical content are skipped, so re-importing the same file is a no-op and a later export appends only what's new. It happens in one transaction — a file rejected on its last line leaves the store exactly as it was. Session IDs are preserved, so a SHA you quote in a code review means the same step on both machines; two different runs claiming one ID is a refusal, not a silent merge (`--db` puts the incoming trace in a separate store instead).

## Python API

```python
from retrial import record, fork, diff, bisect, ablate, sweep, rerun, trajectory, Store
from retrial import export, import_   # the same JSONL transfer, in-process
```

`trajectory(store, session_id)` materializes the full path from root to tip, walking the parent chain and tagging each step `replayed` or `live`.

`export(store, session_ids)` yields the file's lines; `import_(store, lines)` reads them back and returns a summary of what changed.

### Typed

retrial ships `py.typed`, so the annotations are visible to your type checker rather than collapsing to `Any` at the import boundary. Every function returns a plain dict — they stay JSON-shaped and printable — but the shapes are declared, so a typo in a key is an error and not a `KeyError` at 3am:

```python
from retrial import Step, DiffResult, TrajectoryEntry, Session   # and the rest

result = diff(store, a, b)
result["divergance"]   # error: TypedDict "DiffResult" has no key "divergance"
                       # note:  Did you mean "divergence"?
```

`@record` preserves your agent's own signature, so your call sites stay checked too.

## Try it without an API key

The bundled example ships a scripted model, so you can fork, diff, and bisect with no spend. See the **[60-second tour](https://github.com/ArcKansupada/retrial/blob/main/examples/README.md)**:

```bash
retrial init
python examples/booking_agent.py
retrial list
```

## License

MIT
