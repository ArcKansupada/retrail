# `retrail export` / `retrail import` — implementation plan

The collaboration story with no server in it: *here is my trace, fork it
yourself and see.* Also the only safe way to keep anything before deleting
`.retrail/`.

Written before the code so the decisions are settled in one place rather than
discovered halfway through. Delete this file once the feature ships and the
reasoning has moved into the source and CHANGELOG.

---

## The measurement everything else follows from

Two independent runs of a deterministic agent produce **byte-identical step
content**. `compute_sha` hashes `(session_id, step_number, step_type, input,
output)`, and `steps.sha` is globally `UNIQUE` — so `session_id` is the only
thing keeping those two runs' SHAs apart. Verified, not assumed:

```
step content identical:                True
SHAs differ (session_id in hash):      True
SHA without session_id would collide:  True
```

This is not a corner case — temperature-0 runs, cached responses, and no-edit
forks all reach it. Three consequences:

1. SHAs cannot be made portable by hashing content alone. That option is closed.
2. **SHAs survive an import iff session IDs are preserved.** Stability is a
   consequence of decision 2, not an independent choice.
3. Remapping IDs invalidates every SHA in the file, which is why there is no
   remap flag (see *Rejected* below).

---

## File format

JSONL. One JSON object per line, so a file streams, appends, diffs in git, and
survives a truncated write with everything before the tear still readable.

**Line 1 — header.**

```json
{"kind": "header", "format": 1, "schema": 1, "requires": [],
 "exported_at": 1753660800.0, "retrail": "0.1.0"}
```

- `format` — version of *this file layout*. Bumped when the envelope changes.
- `schema` — the database `SCHEMA_VERSION` the rows came from.
- `requires` — feature names an importer **must** understand to read this file
  correctly. Empty today. This is what makes forward compatibility safe rather
  than optimistic: an unknown field that is *not* named here is inert and can be
  carried through with a warning; anything named here that we do not recognize
  is a refusal.
- `retrail` — producing version, for bug reports. Never used for decisions.

**Then session lines, ancestors first**, so a parent is always defined before
the child that references it:

```json
{"kind": "session", "id": "s_ab12cd34ef", "name": "booking-agent",
 "parent_session_id": null, "parent_sha": null, "forked_at_step": null,
 "edit": null, "created_at": 1753660000.0, "status": "complete"}
```

**Then step lines**, grouped by session, ascending `step_number`:

```json
{"kind": "step", "sha": "9f2c...", "session_id": "s_ab12cd34ef",
 "step_number": 0, "step_type": "model_call", "input": {...}, "output": {...},
 "tokens_used": 812, "cost_usd": 0.0041, "duration_ms": 1830.4,
 "created_at": 1753660001.0}
```

`created_at` is carried so an imported trace reports when the run actually
happened rather than when it arrived — the alternative is a store full of
sessions that all claim to be from the day someone imported them. The step's
local `id` is *not* carried: it is an autoincrement rowid, meaningless in
another database.

`input`/`output` are the parsed objects, not the stored JSON strings — the file
is readable, and re-serializing through `canonical_json` on import reproduces
the exact bytes the SHA was computed over. That round trip is what the SHA
check below verifies.

### Ordering is a guarantee, not an accident

Ancestors before descendants, steps ascending. An importer may rely on it, so
export must enforce it and import must verify it. A file whose parent line
comes after its child is malformed, not merely inconvenient.

---

## `retrail export`

```
retrail export <session-id>...        # sessions plus their ancestor chains
retrail export --all                  # the whole store
retrail export ... -o trace.jsonl     # default: stdout
```

**Ancestors are included by default** (decision 1). A fork without its parents
cannot be diffed or have its trajectory materialized, which is the entire
reason to send someone a fork. Descendants are *not* included: exporting a root
should not hand over every experiment you ran on top of it.

- `--no-ancestors` for the rare "just this session" case. Emits a warning,
  because the result is not independently useful.
- Writing to stdout means `retrail export s_ab12 | gh gist create -` works,
  which is the sharing path with the least ceremony. All progress and warning
  output goes to **stderr** so the pipe stays clean.
- Unknown session id → `NotFound`, before writing a byte.

---

## `retrail import`

```
retrail import trace.jsonl            # into the discovered store
retrail import trace.jsonl --db other.db
retrail import -                      # from stdin
```

### Per-row, content-checked. Three outcomes per row:

| state | action |
| --- | --- |
| id absent locally | insert |
| id present, content identical | skip |
| id present, content differs | **refuse** |

Identity is decided by the step SHA, which is what content addressing is for.

This makes import **idempotent** and **incremental** for free. Re-importing the
same file changes nothing. Importing a later export of a still-running session
appends only the steps that are new.

### Why "collision" was the wrong frame

Session ids are 40 random bits, so an accidental collision essentially never
happens. The case that happens constantly is *the same session arriving again*:
you export, a colleague forks it, and sends back a file containing your
original plus their fork. That is overlap, not conflict, and it is the main
flow. A design that treats every reappearance as a collision would refuse the
thing the feature exists to do.

### The one permitted mutation

A session's `status` legitimately differs between two exports of one run:
`running` on the first, `complete` on the second. Allow `running` → any
terminal status. Refuse every other field mismatch, naming the field.

### Every step's SHA is verified on import

Recompute from the row's own content and compare against the file. Mismatch is
a refusal. A few lines, and it upgrades an imported trace from *well-formed* to
*provably the one that was exported*. Same rule the store already applies to its
own schema version.

### Referential integrity

A `parent_session_id` must resolve to a session either in the file or already
in the store. Otherwise refuse: a fork whose parent is missing cannot be
diffed or walked, and importing it would create exactly the quiet
half-usable state this project refuses elsewhere.

### Atomicity

The whole import is one transaction. A file that fails validation at line 900
leaves the store untouched — no half-imported trace to reason about. Validate
in a first pass, write in a second, commit once.

---

## Version handling (decision 4: translate, do not just refuse)

**Older `format`** → translate forward through a chain, the same shape as
`storage._migrate`: `_translate_1_to_2`, and so on. Each step is a pure
function on the parsed rows.

**Newer `format`** → cannot be translated in general, because an unknown field
may be load-bearing and nothing about it says so. Resolved by `requires`:

- unknown field, not named in `requires` → accept, warn once, carry it through
- anything in `requires` we do not recognize → refuse, naming the feature and
  pointing at `pip install -U retrail`

That makes "try to translate" safe rather than optimistic, and gives future
versions a way to say *this extension is not optional* without breaking every
older importer by default.

**`schema` newer than ours** → refuse, reusing `SchemaVersionError`'s reasoning.

---

## Errors

New: `ExportFormatError(RetrailError)` — malformed file, bad ordering, SHA
mismatch, unknown required feature. Carries the **line number**, because a
1-in-900-lines problem needs a location and not just a description.

Reused: `NotFound`, `SchemaVersionError`.

Every refusal message states what was wrong, which line, and what to do —
matching the existing errors (`SchemaVersionError`, the `@record` refusals).

---

## Rejected, and why

- **`--allow-remap`.** Remapping ids invalidates every SHA in the file, so the
  copy-pasteable handle dies exactly when it is being shared. Doing it honestly
  also means recording the original id, which is a new column and a schema v2.
  It buys a case that needs a 40-bit collision or a hand-edited file, and
  `--db other.db` already handles that losslessly. Refuse and point there.
- **Dropping `session_id` from the SHA** to make handles portable. Measured
  above: it collides against `UNIQUE(sha)` for two deterministic runs.
- **Exporting descendants by default.** Handing over every experiment run on
  top of a session is a surprise, and a bad one if a fork name is candid.
- **A binary or archive format.** JSONL is greppable, diffable in a PR, and
  readable without retrail installed. Losing that costs more than the bytes it
  saves.

---

## Order of work

Each step is a commit, each verified on 3.13 and the 3.10 floor, each with the
guard removed once to confirm the tests fail.

1. ~~`retrail/portable.py` — header/session/step row shapes in `types.py`,
   serialize and parse, ordering guarantee.~~ **Done.** 30 tests. Ordering
   turned out to need a third rule the plan had not named: a session's steps
   must be *contiguous*, not merely ascending, or a streaming importer has to
   buffer the whole file to know when a session is finished.
2. `export()` + ancestor walk. Tests: ancestors included, descendants excluded,
   ordering enforced, `--no-ancestors` warns.
3. `import_()` validation pass — SHA verification, referential integrity,
   ordering, version handling. Tests: every refusal, each with its own file.
4. `import_()` write pass — insert/skip/refuse, status advance, atomicity.
   Tests: idempotent re-import, incremental append, conflict refused, failed
   import leaves the store untouched.
5. CLI wiring: `export`, `import`, stdout/stdin, stderr for diagnostics.
6. README section, CHANGELOG, and delete this file.

**The test that matters most** and should exist before step 5 is done: export a
forked session, import into an empty store, and assert `diff` and `trajectory`
produce identical output on both sides. That is the actual promise — not that
the file parses, but that the trace still *works* after the trip.
