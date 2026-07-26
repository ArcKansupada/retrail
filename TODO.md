# retrail — road to a published Python library

Status today: the *product* is done and validated live (record / fork / diff / bisect /
ablate / sweep / rerun / cost, 186 offline tests + 8 live). What's missing is almost
entirely the packaging, distribution, and library-citizenship layer around it — plus a
few correctness gaps that only bite once other people's code imports this.

Ordered by "what blocks `pip install retrail`" first.

---

## P0 — blockers before anyone can install it

- [ ] **Put it under version control.** There is no git repo at all. `git init`, first
      commit, push. Everything below assumes a remote exists (CI, PyPI trusted
      publishing, issue links in metadata).
- [ ] **Add a `LICENSE` file.** `pyproject.toml` declares MIT via
      `license = { text = "MIT" }` — that form is deprecated in modern packaging and
      there's no license file in the tree. Move to SPDX `license = "MIT"` +
      `license-files = ["LICENSE"]` and ship the actual text.
- [ ] **Fill in project metadata.** Currently missing: `authors`, `keywords`,
      `classifiers` (Python versions, Development Status, Topic, OS), and
      `[project.urls]` (Homepage / Repository / Issues / Changelog). This is what the
      PyPI page is made of.
- [ ] **Single-source the version.** `__version__ = "0.1.0"` in `retrail/__init__.py`
      and `version = "0.1.0"` in `pyproject.toml` will drift. Use hatch's
      `[tool.hatch.version] path = "retrail/__init__.py"` with `dynamic = ["version"]`.
- [ ] **Add `retrail/__main__.py`.** `cli.py`'s own docstring promises the CLI works
      "however it's entered — console script, `python -m`, or a test harness", but
      `python -m retrail` fails today because there's no `__main__.py`.
- [ ] **Add `retrail --version`.** No version flag exists. First thing anyone types
      when filing a bug.
- [ ] **Verify the build artifacts.** `python -m build`, `twine check dist/*`, then
      install the *wheel* into a clean venv and run the 60-second tour from a temp
      directory. Confirm the wheel contains only `retrail/` (hatch is configured for
      that) and that the README renders on PyPI's markdown parser.
- [ ] **Verify the `>=3.9` floor is real.** It's asserted, never tested — nothing has
      ever run on 3.9. Either confirm it or raise the floor to what you actually
      support.
- [ ] **CI.** GitHub Actions matrix over 3.9–3.13 × {ubuntu, windows, macos} running
      the offline suite. Windows especially: the cp1252 encoding degradation in
      `cli.echo()` / `_glyphs()` is real, hard-won logic that currently has no
      automated coverage on the OS it exists for.
- [ ] **Release workflow.** Tag-triggered publish via PyPI trusted publishing (no API
      token in secrets). Do a TestPyPI dry run first.
- [ ] **Lint/format config.** `ruff` + a `[tool.ruff]` block with `target-version`
      matching `requires-python`, wired into CI.

## P1 — things that break once it's a *library*, not a script

- [ ] **Type hints + `py.typed`.** There is not one annotation in the package. For a
      library whose entire public contract is *dicts* — a step dict, a session dict,
      the shapes returned by `diff()` / `bisect()` / `ablate()` — this is the single
      biggest usability gap. Annotate the public surface, add `TypedDict`s for
      `Step`/`Session`/`DiffResult`/`BisectResult`, ship `py.typed`, run mypy or
      pyright in CI. Right now those shapes are documented only in prose, which means
      they aren't documented at all.
- [ ] **Find the store by walking up, like git does.** `default_db_path()` is
      `os.getcwd() + "/.retrail"`, full stop. Consequences: running `retrail log` from
      a subdirectory silently creates a *second, empty* store instead of finding the
      one above it, and running your agent from a different working directory records
      into a different database than the CLI reads. Walk parents for `.retrail/`, and
      honour a `RETRAIL_DB` environment variable.
- [ ] **Schema versioning.** The schema is applied with `CREATE TABLE IF NOT EXISTS`,
      so a database written by a future retrail opens happily and then misbehaves. Set
      `PRAGMA user_version`, refuse a newer db with a clear message, and leave room for
      migrations. Do this *before* release — after release it's a compatibility break.
- [ ] **Decide the thread-safety story and enforce it.** `sqlite3.connect()` defaults
      to `check_same_thread=True`, and `record._default_stores` is a shared module-level
      dict. An agent that fans tool calls out to a thread pool, or a web app recording
      two runs concurrently, will hit a raw sqlite error several frames deep. Either
      make connections thread-local, or detect the cross-thread case and raise a real
      `IntegrationError` explaining the boundary.
- [ ] **Async agent loops.** Deferred in v1 (design doc §11) — but the current failure
      mode is silent corruption, not a refusal: `@record` on an `async def` times
      coroutine *construction*, serializes a coroutine object as the "response", and
      marks the session `complete` before the loop has executed a single step. Minimum
      bar for release: detect `inspect.iscoroutinefunction` on the agent, `call_model`,
      or `execute_tools` and raise. Real fix: an async-aware recorder. This is the
      most-likely-requested feature from anyone with a production loop.
- [ ] **Close connections.** `_default_stores` entries are opened and never closed —
      no `atexit` hook, no public way to reset. Add cleanup plus a public
      `retrail.use_store(...)` / `set_default_store(...)` so applications and test
      suites can control it instead of relying on cwd.
- [ ] **Use `logging`, not silence.** A `logging.getLogger("retrail")` emitting what
      was recorded, what was refused and why, and what a fork spliced. Libraries that
      print nothing and explain nothing are hard to trust when the replay looks odd.
- [ ] **One exception hierarchy.** `_load_agent()` raises `click.BadParameter`, so the
      same user error surfaces as a click error from the CLI and something else from
      Python. Keep click at the edge; raise `IntegrationError` underneath.

## P2 — features worth adding

**Sharing and archival**
- [ ] **`retrail export` / `retrail import`** — a session tree (plus its ancestors) as
      JSONL. This is the collaboration story with no server in it: "here's my trace,
      fork it yourself and see." Also the only safe way to keep anything before
      deleting `.retrail/`. High value, low complexity, fits the local-first thesis.
- [ ] **`retrail diff --format html|md`** — a pasteable divergence report for a PR or
      an issue. The diff output is the most convincing artifact retrail produces and
      right now it only exists in a terminal.

**Store hygiene** *(gets urgent fast in real use)*
- [ ] **`retrail rm` / `retrail prune`.** There is no way to delete anything. Every
      bisect, ablate, and sweep probe mints its own session by design — a couple of
      real bisects and `retrail list` is unreadable. Tag probe sessions with their
      origin (`bisect`/`ablate`/`sweep`/`rerun`), hide them from `list` by default
      behind `--all`, and let `prune` collect them.
- [ ] **`retrail list` filtering + `retrail grep`.** Filter by name, status, date,
      parent; search recorded message/tool content across sessions. Once you have 200
      recorded runs, finding the one you care about is the actual bottleneck.

**Making the analysis commands stronger**
- [ ] **Richer `--check` expressions.** Today: `contains` / `not contains` / `matches`.
      Add `tool called X` and `tool not called X` — cheap to implement, and it's
      exactly the predicate the ablate story is built on ("a tool that *ran* versus a
      tool that *mattered*"). Optionally an opt-in LLM-judge check for fuzzy criteria,
      clearly labelled as costing money and being non-deterministic.
- [ ] **Numeric sweeps with real threshold search.** `--values-file` is a hand-written
      linear scan. `retrail sweep --range 100:2000` doing binary search on the check
      flip finds the $600 boundary in ~4 calls instead of N, and it's the same fork+check
      machinery already there.
- [ ] **`--values` inline** so a quick sweep doesn't require creating a file.
- [ ] **Parallel probes.** bisect/ablate/sweep/rerun are N independent forks executed
      serially, each one a live model call. `rerun` over 40 recorded sessions is the
      obvious win and the one that runs in CI. Depends on the thread-safety item above.
- [ ] **Spend guardrails.** This is a library whose loops spend real money — a `rerun`
      over a few hundred sessions is a genuine footgun. Add `--max-cost` /
      `RETRAIL_MAX_SPEND`, plus an up-front estimate ("~40 calls, ~$0.31, continue?")
      and `--dry-run` for every re-executing command.
- [ ] **`retrail replay <sha> --dry-run`** — print the exact seeded message history a
      fork *would* send, without spending a token. Both a debugging tool and the direct
      answer to "do I actually trust the splice?", which is the question the whole
      product rests on.

**Pricing**
- [ ] **Make the price table overridable and dated in the output.** `PRICES` is a
      hardcoded dict cached 2026-06 and it will be wrong; retrail is deliberately
      provider-agnostic but only prices Anthropic models. Support a `RETRAIL_PRICES`
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

- [ ] **CHANGELOG.md** + a stated stability policy: say plainly which returned dicts are
      public API and which are internal. At 0.1 you're allowed to break things — but only
      if you said which things.
- [ ] **Docstring pass on the public API** so `help(retrail.fork)` is genuinely enough.
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

### If you only do six things

1. `git init` + LICENSE + metadata + CI *(P0, mechanical, blocks everything)*
2. Type hints + `py.typed` *(the dict-shaped API is unusable without them)*
3. Store discovery walks up to find `.retrail/` *(silent wrong-database bug)*
4. Raise on async agents *(silent corruption today)*
5. `retrail export` / `import` *(the sharable-repro feature, cheap to build)*
6. `prune` + hiding probe sessions *(the store becomes unreadable within a day of real use)*
