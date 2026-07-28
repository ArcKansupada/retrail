# retrial — Design Doc

**Name:** `retrial` — a run put back on trial. (Was `retrail`, "replay" + "trail", through the design phase; renamed 2026-07-27, before the first release.)
**One-liner:** Git for agent trajectories — branch, diff, and bisect LLM agent runs, backed by real re-execution instead of static logs.
**Status:** Draft v0.1

---

## 1. Problem

Agent harnesses (raw SDK loops, LangGraph, custom multi-step pipelines) are non-deterministic and stateful across many steps. When something breaks — wrong tool call, hallucinated fact, bad final answer — the only debugging tool most people have is scrolling through logs. There's no way to:

- Go back to step N, change one fact (a tool result, a retrieved doc, an intermediate decision), and see what the agent *actually would have done differently* from there — with the real model, not a guess.
- Compare two trajectories and see precisely where they diverged and why.
- Automatically localize which step in a long run introduced a regression.

Existing tools (e.g. `agent-replay`) solve the *logging and browsing* half of this well — record a trace, store it locally, diff two stored traces, tag branches. But forking in these tools branches the **stored JSON**, not the **live agent** — you still have to manually re-run your agent and re-ingest the new trace yourself. The re-execution loop, which is the actually hard and actually valuable part, is left to the user.

## 2. Core insight / thesis

An agent step is (mostly) a pure function: `(system prompt, message history up to step N) → next action`. If you record every step's exact input and output, you can:

- **Replay** any prefix of a run exactly, for free, with no API calls (just play back the log).
- **Branch** at any step by truncating history there and substituting a different fact — then resume the *real* loop, making real model calls, from that point forward.

This is the same mental model as git: a commit is a real, checkoutable state you can build forward from — not a screenshot. That's the difference between a log viewer and a version-control system for agent runs.

## 3. Non-goals (v1)

- Not an observability platform (no dashboards, no production monitoring, no alerting).
- Not an eval/guardrail suite. No hallucination scoring, no policy engine. (Possible v2+, not core.)
- Not framework-agnostic on day one. One integration, done well, beats five done shallowly.
- Not a hosted product. Local-first, no account, no telemetry, matches how the target user (developers debugging their own agent) actually wants to work.

## 4. Target user & first integration

**User:** developers building agents with a raw Anthropic/OpenAI SDK loop (`while` loop calling `messages.create`, executing tool calls, appending results) — not a LangGraph/CrewAI user, since those frameworks already have their own checkpointing.

**Why raw-loop first:**
- Simplest, cleanest interception point (two functions: the model call, the tool executor).
- No competing with a framework's own state management.
- Most common pattern once people outgrow off-the-shelf frameworks.

LangChain/LangGraph adapters are a v2 consideration, not blocking v1.

## 5. Core concepts & vocabulary

Built independently from other tools' schemas — designed around what a raw SDK loop actually produces.

| Concept | Meaning |
|---|---|
| **Session** | One full agent run, from start to its current furthest point. Analogous to a branch tip. |
| **Step** | One iteration of the loop: the model call (input messages + output) and the resulting tool call(s) + result(s). Analogous to a commit. |
| **Fork** | A new session created by taking an existing session's steps up to N, substituting an edited fact at step N, and resuming live execution from there. |
| **Trajectory** | The full path of steps from session start to its current tip — what you'd diff or bisect over. |
| **Step SHA** | A short content hash identifying a specific step's exact state (input messages + output), used to address it directly — e.g. `retrial fork a1b2c3d` instead of `retrial fork <session-id> --from-step 4`. |

Step types kept minimal for v1: `model_call`, `tool_call`. Expand only when a real integration needs more (no speculative categories).

**Step addressing via SHA:** each step gets a content hash (of session id, step number, and the serialized input/output) computed at record time, git-commit-style. This gives a stable, short, copy-pasteable reference to an exact step — useful for forking ("fork from `a1b2c3d`"), for diff output (referencing the divergence point by SHA), and later for bisect (recording which SHA was checked at each search iteration). It also means a fork is reproducible and shareable as a single string, without needing to pass around a full session id + step number pair.

## 6. Architecture

### 6.1 Recording layer

A decorator/context manager wraps the user's existing loop. Two integration points required from the user:
1. The function that calls the model.
2. The function that executes tool calls.

```python
from retrial import record

@record(session_name="booking-agent")
def run_agent(messages, tools, execute_tools):
    while True:
        response = client.messages.create(model=..., messages=messages, tools=tools)
        if response.stop_reason != "tool_use":
            return response
        results = execute_tools(response)
        messages.append(...)
```

No manual export step, no schema to hand-build — steps are logged automatically as the loop runs.

**Decision:** explicit function args (`execute_tools` passed in), not monkey-patching the SDK client. More code for the user to change up front, but zero magic — every recorded step traces back to a line the user wrote, which matters a lot when the whole product's credibility rests on "the replay is exactly what happened."

### 6.2 Storage

SQLite, single local file (`.retrial/sessions.db`), tree-structured from the start:

```
sessions(id, name, parent_session_id, forked_at_step, created_at, status)
steps(id, sha, session_id, step_number, step_type, input_json, output_json,
      tokens_used, cost_usd, duration_ms, created_at)
```

`sha` is indexed and unique — computed as a short hash of `(session_id, step_number, input_json, output_json)` at write time. Supports prefix matching for CLI convenience (`retrial fork a1b2` resolving to the full SHA), same UX as git's short hashes.

Tree structure (`parent_session_id` + `forked_at_step`) means a fork is a *new session row*, not a mutation — original sessions are always intact and comparable.

### 6.3 Replay / fork engine — the core differentiator

```python
from retrial import fork

new_session = fork(
    from_sha="a1b2c3d",   # step SHA, resolves to its session + step number internally
    edit=lambda step: {**step, "output": {"flight_price": 999}},  # exact substitution TBD
)
```

Mechanics:
1. Resolve `from_sha` to its `(session_id, step_number)` and load the message state as of that step.
2. Apply the edit to that step's tool output.
3. Re-invoke the **user's own `run_agent` function**, seeded with the modified history instead of a blank start.
4. Real model calls happen from the next step onward — this is genuine re-execution, not data relabeling.
5. The new session's first re-executed step gets its own SHA; the forked-from step's SHA is stored as `parent_sha` for provenance, so `retrial log` can show exactly which recorded step a fork branched from, permanently, even after the original session is deleted or renamed.

Addressing forks by SHA rather than `session_id + step_number` also means a fork command is a single self-contained, shareable string — useful for bug reports ("this broke, forked from `a1b2c3d`, here's what changed") and for scripting bisect (Section 6.5), which needs to reference many intermediate steps precisely.

**Requires one integration contract from the user:** `run_agent` must accept an initial message state as a parameter (not always assume a blank start). This is the one non-negotiable convention that makes real re-execution possible — needs to be extremely clearly documented, since it's the crux of the whole tool.

### 6.4 Diff engine

Sequence alignment (start with `difflib.SequenceMatcher` on step signatures — model_call vs tool_call, tool name, rough output hash — upgrade to Needleman-Wunsch later if needed) between two sessions sharing a common ancestor. Output: shared prefix, divergence point, per-step diff, final-answer diff.

### 6.5 Bisect — v1 stretch goal, v1.1 if cut

```
retrial bisect <session-id> --check "output contains 'confirmed'"
```

Binary search over a long session's steps: replay-fork from the midpoint, check if the failure condition still reproduces downstream, recurse. Since fork is real re-execution, this is fully automatable and needs no new primitives beyond fork + a user-supplied check function — cheap to build once 6.3 works, and a strong, demoable "this is genuinely useful" feature for launch.

### 6.6 Cut, and why

- **Merge** — ~~synthesizing two diverged branches into one informed run~~. **Cut, not deferred.** This is where the git metaphor stops paying rent. Git merges because branches are *work that must be combined* — both bodies of work are needed and the merged artifact is what ships, so divergence is a problem to resolve. retrial's branches are *questions being asked*, and two forks are usually competing hypotheses of which at most one is true. Merging "the fare was $450" with "the fare was $1,450" feeds the model contradictory facts: not a better-informed run, a confused one. Answers don't merge; once you know what the agent would have done, the fork is disposable and you go fix the real tool or prompt.

  The two readings both fail. The version that would be *novel* — synthesizing a message history representing both branches — fabricates a state no agent ever occupied, which is the exact opposite of the invariant the whole tool rests on (§6.3 raises rather than guess about a single field; merge would make inventing whole histories a feature). The version that is *legitimate* — a new root session whose prompt says "you tried A and got X, you tried B and got Y, now decide" — invents nothing, but is a thin wrapper over `final_answer(trajectory(...))` plus a template, serving a parallel-sample-and-synthesize pattern that belongs in the user's agent loop rather than in a post-hoc debugger (see §3: not an eval suite).

- **Blame** — ~~tracing a span of final output back to the specific tool call/step that grounds it~~. **Cut as specified; replaced by ablation (§6.7).** Attribution is heuristic and fuzzy: it *infers* which step grounds an answer from the text. Because retrial has real re-execution, the same question can be answered causally instead — perturb the fact and see whether the outcome actually changes. Intervention beats inference, and it needs no attribution logic at all.

### 6.7 Ablation, sweep, and rerun

Both are "fork many times over one axis, compare outcomes", and like bisect they need no new primitives beyond fork + a user-supplied check.

- **Ablate** — sweep over *steps*. Perturb each recorded fact one at a time, re-execute, and report which ones flip the check. Answers "which facts is this answer load-bearing on?" — the question blame was reaching for — but causally.
- **Sweep** — sweep over *values*. Fork one step across N substituted values to find a threshold ("at what fare does it stop booking?").

**Both compare on the check, never on answer text.** A real model rewords itself on every run, so text equality would report every step as load-bearing regardless of the perturbation. The check is what makes the signal robust to non-determinism — the same reason bisect uses one.

- **Rerun** - sweep over *recorded runs*. Re-execute the whole corpus against the current code and report which outcomes changed. The recorded runs are the regression suite; nobody writes the cases.

**Where a rerun resumes decides whether it tests anything.** Resuming at the last tool call is cheapest and usually vacuous - the consequential decisions are already in the replayed history, so the model only summarizes. Measured against live Opus 4.8 with a budget prompt tightened $600 -> $300: `--from last` reported 0 regressions for $0.0106; `--at-tool search_flight` caught it for $0.0077; `--from first` caught it for $0.0129. So `first` is the default (a cheap test that tests nothing is worse than a slow one), and `--at-tool` is the option that is both cheap and meaningful - it beats the full re-run on price *and* correctness, because it resumes where the decision under test actually lives.

**Cost is computed at record time and never re-priced.** The stored figure is what the run actually cost, at the prices in force then; re-pricing an old trace against today's table would quietly rewrite history. Traces recorded before cost tracking existed are derived on read from the recorded model + usage, which is exact rather than estimated. An unknown model prices as None, never a guess, and a partial total is reported as partial - summing None as zero would produce a number you would act on.

**The signal is asymmetric, and the output says so.** A step whose ablation does *not* flip the check is soundly not load-bearing: the answer survived without that fact. A step whose ablation *does* flip it is only *possibly* load-bearing, since the agent may be reacting to the perturbation itself (an errored tool) rather than to the lost value. Ablation rules facts out rigorously and rules them in suggestively.

## 7. CLI surface (v1)

```
retrial init                                  # creates .retrial/ + sqlite db
retrial list                                  # sessions, tree view
retrial log <session-id>                      # step-by-step history, SHA per step
retrial show <sha>                            # full detail on one step, by SHA
retrial fork <sha> --edit-file edit.json      # fork from a step SHA, not session+step
retrial diff <session-a> <session-b>
retrial bisect <session-id> --check "<condition>"   # which step doomed a failed run
retrial ablate <session-id> --check "<condition>"   # which facts a good run needed (6.7)
retrial sweep <sha> --values-file values.json      # find a threshold (6.7)
```

SHA prefix matching applies throughout, same convention as git (`retrial show a1b2` resolves uniquely as long as the prefix is unambiguous).

## 8. Milestones

**Order is deliberately inverted from a typical build sequence: prototype the riskiest, most load-bearing piece first, before any storage layer, CLI, or polish exists.** If real re-execution turns out to be awkward or unreliable, everything downstream (SHA addressing, diff, bisect) needs to be rethought — better to find that out on day one with throwaway code than after building the scaffolding around it.

**Status:** all six milestones are implemented (`prototype/`, `retrial/`, `tests/`, `examples/`). 122 tests pass against a scripted model; live-API validation is the outstanding gap (see Section 11).

0. ✅ **Prototype fork re-execution — no SQLite, no CLI, no SHAs, just a script.** Hand-write two hardcoded "steps" of message history in memory, manually splice an edited tool result at step 1, and re-invoke a real minimal agent loop from there against the live API. Confirm the resumed loop behaves exactly as if that edit had actually happened — no leftover state from the original run leaking in, no missing context. This is a throwaway script, not production code; its only job is to de-risk Section 6.3 before anything is built on top of it.
1. ✅ **Recording works.** Decorator logs a real raw-loop run to SQLite (with SHA per step); `retrial log` prints it. Demoable on its own.
2. ✅ **Fork actually re-executes, for real, through the CLI.** Same mechanic validated in milestone 0, now wired through storage and SHA addressing.
3. ✅ **Diff output.** Tree/side-by-side view, divergence point highlighted by SHA.
4. ✅ **One polished example repo.** A toy multi-tool agent (e.g. flight booking, 2–3 tools) people can clone and fork against immediately — this is the thing people will actually try in the first 60 seconds.
5. ✅ **Bisect**, if time allows — strong launch-post feature, cheap once milestone 2 is solid.
6. ✅ Docs, README with the differentiation framing up front, PyPI packaging.

## 9. Differentiation summary (for README / launch post)

- **Real re-execution, not log branching.** Fork re-enters your live agent loop; it doesn't relabel stored JSON.
- **Zero-export integration.** A decorator, not a manual JSON schema to hand-build.
- **Narrow and deep.** One thing (branch/diff/bisect via real execution) done well, not six things done shallowly.
- **Python-native**, matching where most agent/RAG/ML tooling already lives.

## 10. Decisions log

- **Recording mechanism:** explicit function args, not monkey-patching. (Section 6.1)
- **Step/fork addressing:** content-hash SHA per step, git-short-hash-style, resolved via prefix matching. Forks reference a `from_sha`, not a `session_id + step_number` pair. (Sections 5, 6.2, 6.3)
- **Build order:** prototype fork re-execution first (Milestone 0), before storage, SHAs, or CLI exist — highest-risk piece gets validated in isolation before anything is built on top of it. (Section 8)
- **Both integration points are explicit args.** Section 6.1's numbered list says the model call *and* the tool executor are intercepted, but its code sample only passes `execute_tools` and leaves `client.messages.create` inline — which the decorator cannot see. Resolved in favour of the numbered list: `run_agent(messages, tools, call_model, execute_tools)`. Giving the non-`messages` params defaults is what lets `retrial fork --agent mod:fn` call the agent with only the seeded history. (Section 6.1)
- **Replay is read back, never reconstructed.** Milestone 0 tested both. Reconstructing state from parts (base + assistant turn + tool results) bakes in an assumption about how the user's loop assembles history, which makes replay a plausible imitation rather than a recording. Instead the recorder snapshots `messages` verbatim at each model call; a fork reads that snapshot back, patches the one recorded fact, and **verifies the patch landed**. If the recorded output doesn't appear in the history verbatim (the loop transformed it), retrial refuses rather than guessing. (Section 6.3)
- **Edit API: hybrid.** Structured JSON patch is the native shape — it's serializable, so it's stored on the fork's session row and `retrial log` can show *what* changed, not just where. A callback is accepted from Python for content-dependent edits and for bisect, recorded as `{"op": "callback", ...}` with an explicit note that it does not round-trip from the record. The CLI forces the patch shape into existence regardless, so callback-only would have deferred the work rather than saved it. (Sections 6.3, 11)
- **Fork sessions store only the re-executed suffix.** A fork's steps begin at the resume point; the replayed prefix stays in the parent, reachable via `parent_sha`. Same shape as a git branch — new commits only. (Sections 6.2, 11)
- **The interception points must be callable when the agent is invoked, not built lazily inside it.** `@record` wraps the `call_model` and `execute_tools` *arguments* before the body runs, so a `None` sentinel you intended to swap out inside the body gets wrapped instead and dies as `'NoneType' object is not callable` several frames deep in retrial. This is now rejected at the boundary with an explanatory error, *before* a session row is created — an earlier version validated too late and stranded an empty session that then showed up in `retrial list` and got picked up by a diff. The pattern for lazy setup (e.g. constructing an API client without demanding credentials at import) is a real module-level function that builds its client on first call; see `examples/live_booking_agent.py`. (Section 6.1)
- **All CLI output is sanitized for the console encoding.** Everything retrial prints is downstream of model output — diff shows final answers, bisect shows probe answers — and a real model emits emoji freely. On a cp1252 Windows console an emoji *in an answer* crashed the command reporting it. Every print now goes through `cli.echo()`, which degrades an un-encodable glyph rather than losing the output; a test forbids raw `click.echo` from reappearing. (Found only by running live; a scripted model never emits emoji.)

## 11. Open questions

- [x] ~~Exact `edit` API shape for fork~~ — **resolved: hybrid.** Patch native + callback escape hatch. See decisions log.
- [x] ~~How to handle non-determinism in the *replayed* prefix vs. the *live* suffix~~ — **resolved by the storage layout, not a heuristic.** A fork session contains *only* re-executed steps; the replayed prefix is never copied into it. So "replay vs. real new generation" is answered by which session a step lives in. `trajectory()` materializes the full path and tags every step `replayed` or `live` by construction, and diff renders that boundary directly.
- [x] ~~Package/project name~~ — **resolved: `retrial`, unclaimed on PyPI** (checked 2026-07-27; `retrace` was taken back in the 2026-07-16 pass, which is what ruled it out). Built under `retrail` and renamed before the first release — deliberately, because the cost of that is a find-replace today and a deprecation shim plus a permanently squatted name once anything is published.
- [x] ~~SHA collision handling~~ — **resolved as proposed.** Full sha256 stored and unique-indexed; short forms are display-only; prefix lookups resolve like git's — unique prefix wins, ambiguity raises `AmbiguousSha` asking for a longer prefix.
- [ ] Async agent loops — deferred entirely from v1, sync only.
- [x] ~~live-API validation~~ — **done against `claude-opus-4-8`** (`examples/live_booking_agent.py`, `tests/test_live.py`, opt in with `pytest -m live`). The thesis holds on a real model: forking a $450 booking with a $1,450 fare spliced in made Opus decline to book, explain the budget breach, and never call `book_flight` — a path the original never took. Confirmed: the serializer round-trips real Pydantic `Message` objects; real `tool_use`/`tool_result` blocks carry the `tool_use_id` the splice matches on; adaptive-thinking blocks survive the snapshot → JSON → SQLite → echo round-trip with signatures intact (the API rejects tampered signatures, so a completed multi-turn run is the proof). Three real bugs it surfaced, all now fixed and regression-tested — see the decisions log.
- [ ] **New:** forking a step whose tool result the loop transformed before appending. Currently refused (correctly — the patch would land on a value the user never saw). If this turns out to be common in real loops, the fix is for the recorder to snapshot *after* the append too, not to start guessing.
- [ ] **New:** bisect's monotonicity assumption. Binary search needs "if it fails at step N it fails at every later step", but the re-executed suffix is a real model, so the good/bad boundary is fuzzy rather than sharp. v1 is honest about this instead of hiding it — every probe is recorded as its own session, and `--samples N` requires unanimity before calling a step recovered. If real use shows the boundary is noisy enough to mislead, the upgrade is a linear scan near the boundary or a statistical stopping rule, not more binary search.
- [x] ~~bisecting over *edits* rather than over resume points~~ — **built as §6.7.** `ablate` sweeps over steps (which facts mattered?), `sweep` sweeps over values (where's the threshold?). Both reuse fork + a check, as predicted. Validated live: sweeping a fare against Opus 4.8 located its $600 budget rule empirically, with the check flipping between $550 and $650.

## 12. Distribution plan

- PyPI (`pip install retrial`), matches existing publishing plan for `chunklet`.
- Launch: Show HN + r/LocalLLaMA / r/LangChain, leading with an asciinema/GIF of fork-and-diff on the example repo.
- README opens with the differentiation framing (Section 9) — don't bury it.
