# retrial — road to a published Python library

Status today: the *product* is done and validated live (record / fork / diff / bisect /
ablate / sweep / rerun / cost, 186 offline tests + 8 live). What's missing is almost
entirely the packaging, distribution, and library-citizenship layer around it — plus a
few correctness gaps that only bite once other people's code imports this.

Ordered by "what blocks `pip install retrial`" first.

---

## P0 — blockers before anyone can install it

**Done (2026-07-25).** Everything in this section is implemented and verified;
the two remaining boxes need an action only you can take.

- [x] **Put it under version control.** `git init` + baseline commit. No remote yet.
- [x] **Add a `LICENSE` file.** MIT text at the repo root, declared with SPDX
      `license = "MIT"` + `license-files = ["LICENSE"]`. Verified: the wheel ships it
      at `retrial-0.1.0.dist-info/licenses/LICENSE`.
- [x] **Fill in project metadata.** authors, keywords, 12 classifiers, and
      `[project.urls]` pointing at `github.com/arckansupada/retrial`.
- [x] **Single-source the version.** `[tool.hatch.version] path = "retrial/__init__.py"`
      with `dynamic = ["version"]`; the duplicate in `pyproject.toml` is gone. The
      publish workflow refuses to release if the git tag and `__version__` disagree.
- [x] **Add `retrial/__main__.py`.** `python -m retrial` works, with a subprocess test.
- [x] **Add `retrial --version`** (`-V`), read from `retrial.__version__` rather than
      `importlib.metadata` so an uninstalled source checkout can still answer it.
- [x] **Verify the build artifacts.** `python -m build` + `twine check --strict` both
      pass. Wheel contains only `retrial/` + dist-info; sdist adds tests, examples, and
      the design doc, and excludes `prototype/`. Installed the wheel into a clean venv
      and ran `--version`, `-V`, `python -m retrial`, `init`, `list`, `--help` from a
      temp directory.
- [x] **Settle the Python floor.** Raised `>=3.9` → `>=3.10`: 3.9 was EOL in Oct 2025
      and had never actually been run. Confirmed nothing in the tree uses a 3.11+
      feature; `itertools.pairwise` (3.10) replaced a bare `zip` pair-walk.
- [x] **CI.** `.github/workflows/ci.yml` — 3.10–3.13 on Linux, 3.10 + 3.13 on Windows
      and macOS, plus a dedicated `windows / cp1252 console` job that sets code page
      1252 and drives the real CLI. That job exists because the source-level ASCII
      guard in `tests/test_output_encoding.py` cannot catch a runtime encode failure.
- [x] **Release workflow.** `.github/workflows/publish.yml`, OIDC trusted publishing,
      no API token in secrets. Setup instructions are in the file's header comment.
- [x] **Lint config.** `[tool.ruff]` with `target-version = "py310"`, wired into CI.
      `ruff check .` passes clean (35 findings fixed: import ordering, two lambda
      assignments in `bisect.py`, `zip` → `pairwise`, ambiguous `l` names in tests).

Still needs you:

- [ ] **Create the GitHub repo and push.** `github.com/arckansupada/retrial` is what the
      metadata claims; until it exists those URLs 404 and CI has never run.
- [ ] **Register the PyPI trusted publisher** before the first tag (owner
      `arckansupada`, repo `retrial`, workflow `publish.yml`, environment `pypi`), and
      do a TestPyPI dry run.
- [ ] **Adopt `ruff format` in its own commit.** Deliberately deferred: it rewrites
      ~544 lines of hand-laid-out code, which would have buried the packaging diff.
      `ruff format --check` is commented out in CI until then.

## P1 — things that break once it's a *library*, not a script

- [x] **Type hints + `py.typed`.** *(done 2026-07-26)* Every function in the package
      is annotated and mypy runs clean under `disallow_untyped_defs` /
      `disallow_incomplete_defs`, on 3.10 in CI. `retrial/types.py` declares all 27
      public shapes, re-exported from the top level. Verified end to end: a consumer
      installed from the built wheel gets key typos, an unguarded Optional, a wrong
      argument type, and a missing argument all flagged — with "did you mean"
      suggestions. Three latent issues surfaced and were fixed along the way (see
      below).
- [x] **Find the store by walking up, like git does.** *(done 2026-07-26)*
      `find_db_path()` searches from cwd to the filesystem root; `resolve_db_path()`
      layers precedence over it — `RETRIAL_DB`, then the search, then create-here.
      `--db` still outranks all three. `init` deliberately does *not* search (like
      `git init`), but prints a `note:` when it shadows a store above, because the
      symptom otherwise is "my sessions vanished", a long way from the cause.
      13 tests in `tests/test_discovery.py`; four of them fail if the upward search
      is removed, which is the check that they pin the bug and not just the code.
- [x] **Schema versioning.** *(done 2026-07-26)* `SCHEMA_VERSION` stamped into the
      file via `PRAGMA user_version`; a newer database raises `SchemaVersionError`
      naming both versions and pointing at `pip install -U retrial`. Pre-versioning
      0.1.0 databases are adopted, since that layout is v1 — refusing them would have
      meant refusing every trace recorded before the marker existed. `_migrate` is
      the hook for the next bump. A failed open closes its connection, so a refusal
      does not leave the file locked on Windows.
- [x] **Thread safety.** *(done 2026-07-26)* Chose "make it work" over "refuse it":
      sharing one store across threads is a legitimate thing to want, so an
      `IntegrationError` would have been rejecting a reasonable pattern. `Store` holds
      an `RLock` across each statement *and* its commit — spanning the commit matters,
      since one connection carries one implicit transaction and interleaved writers
      would otherwise decide each other's fate — which is what makes
      `check_same_thread=False` safe. Probing first turned up three races the note
      above missed: the `_default_stores` check-then-set (8 threads → 8 connections,
      7 orphaned, callers holding uncached twins), `_pending` as a module dict letting
      concurrent forks cross-assign sessions, and read-then-write step numbering.
      9 tests. One of them passed against the broken code until a delay was added to
      widen the window — worth remembering that a concurrency test proves nothing
      until you have watched it fail.
- [x] **Refuse async agent loops.** *(done 2026-07-26)* Raised at decoration time for
      an `async def` agent, and at call time for an async `call_model` /
      `execute_tools`, including a class with an `async def __call__` — which
      `iscoroutinefunction` alone does not catch. Measured before writing anything,
      and the note above was half wrong: an async *model call* did raise, as "cannot
      serialize coroutine" from the serializer — true but silent about the cause. The
      genuinely silent case was an async agent with a **sync** model call, where the
      session is stamped `complete` before the body runs, so a run that raised partway
      through was stored as successful. 8 tests; 6 fail without the guard.
- [ ] **Async agent loops, actually supported.** Deferred in v1 (design doc §11). The
      refusal above is the release bar, not the feature: an async-aware recorder still
      needs `wrap_model`/`wrap_tools` to await, and the wrapper to be a coroutine
      function whose `except BaseException` wraps the awaited body. Most-likely
      request from anyone with a production loop.
- [ ] **Close connections.** `_default_stores` entries are opened and never closed —
      no `atexit` hook, no public way to reset. Add cleanup plus a public
      `retrial.use_store(...)` / `set_default_store(...)` so applications and test
      suites can control it instead of relying on cwd.
- [ ] **Use `logging`, not silence.** A `logging.getLogger("retrial")` emitting what
      was recorded, what was refused and why, and what a fork spliced. Libraries that
      print nothing and explain nothing are hard to trust when the replay looks odd.
- [ ] **One exception hierarchy.** `_load_agent()` raises `click.BadParameter`, so the
      same user error surfaces as a click error from the CLI and something else from
      Python. Keep click at the edge; raise `IntegrationError` underneath.

## P2 — features worth adding

**Sharing and archival**
- [x] **`retrial export` / `retrial import`** *(done 2026-07-27)* — a session tree
      (plus its ancestors) as JSONL, `export`/`import_` in the Python API too. SHAs
      survive because session ids are preserved; the import verifies each SHA against
      its content, is idempotent and incremental, and runs in one transaction.
      `retrial/portable.py` (format) and `retrial/transfer.py` (store) keep "is this
      file well formed?" answerable without a database. ~90 tests across five files;
      the trap worth remembering is that an export is *data*, so it bypasses the
      cp1252-degrading `echo()` — a replaced glyph would break the step's own SHA.
- [ ] **`retrial diff --format html|md`** — a pasteable divergence report for a PR or
      an issue. The diff output is the most convincing artifact retrial produces and
      right now it only exists in a terminal.

**Store hygiene** *(gets urgent fast in real use)*
- [ ] **`retrial rm` / `retrial prune`.** There is no way to delete anything. Every
      bisect, ablate, and sweep probe mints its own session by design — a couple of
      real bisects and `retrial list` is unreadable. Tag probe sessions with their
      origin (`bisect`/`ablate`/`sweep`/`rerun`), hide them from `list` by default
      behind `--all`, and let `prune` collect them.
- [ ] **`retrial list` filtering + `retrial grep`.** Filter by name, status, date,
      parent; search recorded message/tool content across sessions. Once you have 200
      recorded runs, finding the one you care about is the actual bottleneck.

**Making the analysis commands stronger**
- [ ] **Richer `--check` expressions.** Today: `contains` / `not contains` / `matches`.
      Add `tool called X` and `tool not called X` — cheap to implement, and it's
      exactly the predicate the ablate story is built on ("a tool that *ran* versus a
      tool that *mattered*"). Optionally an opt-in LLM-judge check for fuzzy criteria,
      clearly labelled as costing money and being non-deterministic.
- [ ] **Numeric sweeps with real threshold search.** `--values-file` is a hand-written
      linear scan. `retrial sweep --range 100:2000` doing binary search on the check
      flip finds the $600 boundary in ~4 calls instead of N, and it's the same fork+check
      machinery already there.
- [ ] **`--values` inline** so a quick sweep doesn't require creating a file.
- [ ] **Parallel probes.** bisect/ablate/sweep/rerun are N independent forks executed
      serially, each one a live model call. `rerun` over 40 recorded sessions is the
      obvious win and the one that runs in CI. Depends on the thread-safety item above.
- [ ] **Spend guardrails.** This is a library whose loops spend real money — a `rerun`
      over a few hundred sessions is a genuine footgun. Add `--max-cost` /
      `RETRIAL_MAX_SPEND`, plus an up-front estimate ("~40 calls, ~$0.31, continue?")
      and `--dry-run` for every re-executing command.
- [ ] **`retrial replay <sha> --dry-run`** — print the exact seeded message history a
      fork *would* send, without spending a token. Both a debugging tool and the direct
      answer to "do I actually trust the splice?", which is the question the whole
      product rests on.

**Pricing**
- [ ] **Make the price table overridable and dated in the output.** `PRICES` is a
      hardcoded dict cached 2026-06 and it will be wrong; retrial is deliberately
      provider-agnostic but only prices Anthropic models. Support a `RETRIAL_PRICES`
      JSON file / documented `pricing.PRICES` update, so a non-Anthropic user gets
      costs instead of `unpriced` forever.

**Integration reach** *(explicitly a v1 non-goal — revisit for 1.0)*
- [ ] **One thin adapter as an extra.** A `record`-shaped wrapper for a common loop
      (OpenAI-SDK-style, or a LangGraph node). "Not framework-agnostic on day one" is a
      good v1 call; it reads differently once the package is public and the first three
      issues are "how do I use this with X?".
- [ ] **CI-friendly `rerun` output** — junit-xml or GitHub annotations. It already
      exits non-zero; this makes the failure legible in a PR.

## P3 — docs, and the launch itself

- [x] **CHANGELOG.md** + a stated stability policy — written during the P0 pass because
      `[project.urls]` links to it. The policy currently says: the public API is the
      names exported from `__init__.py` plus the CLI, and the returned dict shapes are
      *not* frozen until they're typed (P1).
- [ ] **Docstring pass on the public API** so `help(retrial.fork)` is genuinely enough.
      No docs site needed at 0.1; the README plus docstrings can carry it.
- [ ] **Test the 60-second tour from a clean clone**, on Windows and macOS, exactly as
      written. It's the entire first impression.
- [ ] **CONTRIBUTING.md**, issue templates, and a security/contact line.
- [ ] **README badges** (PyPI version, Python versions, CI, license).
- [ ] **asciinema / GIF of fork → diff** on the example agent, per the distribution
      plan §12. The launch post lives or dies on that clip.
- [ ] **Move `prototype/` and `bench/` out of the repo root** or document what they're
      for. `prototype/m0_fork_prototype.py` is explicitly throwaway code; at the root of
      a published project it reads as part of the library.

---

### What the typing pass turned up

Three things mypy found that were latent, not stylistic. All fixed:

- `cli.py` built a step summary with `', '.join(b.get("type") ...)` over model
  output. A content block without a `type` key put a `None` in that list and
  `str.join` raises on it — `retrial log` would die printing a trace, which is the
  same failure shape as the cp1252 encoding bug.
- `diff._divergence` ended with `(first_a or first_b)["sha"]`. difflib never emits
  an empty non-equal block so it cannot fire, but the invariant existed nowhere
  except in that expression not crashing. Now explicit.
- `_render_bisect` indexed `result["culprit"]` behind a guard on a *different* key
  (`unreproducible`). The two are equivalent by construction in `bisect()`; the
  guard now says so directly.

### If you only do six things

1. ~~`git init` + LICENSE + metadata + CI~~ — **done**, minus pushing to GitHub.
2. ~~Type hints + `py.typed`~~ — **done.**
3. ~~Store discovery walks up to find `.retrial/`~~ — **done.**
4. ~~Raise on async agents~~ — **done.**
5. ~~`retrial export` / `import`~~ — **done.**
6. `prune` + hiding probe sessions *(the store becomes unreadable within a day of real use)*
