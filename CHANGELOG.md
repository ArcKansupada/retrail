# Changelog

All notable changes to retrial are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0, the **public API is the names exported from `retrial/__init__.py`**
plus the CLI. The dict shapes those functions return are now declared in
`retrial/types.py`; before 1.0 they may **gain** keys in a minor release, but an
existing key will not silently change meaning.

## [Unreleased]

### Added
- **`retrial export` / `retrial import`**, and `export` / `import_` in the
  Python API. A session and its ancestors travel as a JSONL file — the
  collaboration story with no server in it, and the only lossless way to keep a
  trace before deleting `.retrial/`. A fork carries its ancestors (a fork
  without its parents can't be diffed or replayed); descendants stay behind.
  Import is idempotent and incremental: every step's SHA is recomputed and
  checked, so a file altered in transit is refused rather than trusted; matching
  content is skipped; the whole read is one transaction, so a file rejected on
  its last line leaves the store untouched. Session IDs are preserved, so a SHA
  stays a shared handle across machines — two runs claiming one ID is a refusal,
  not a silent merge. Adds `ExportFormatError`.
- **The store is found by searching upward**, the way git finds `.git/`. A
  command run in a subdirectory now uses the project's store instead of quietly
  creating a second, empty one beside itself — see *Fixed* below. `RETRIAL_DB`
  points a whole shell or CI job at one store; `--db` still outranks everything.
  `retrial init` is the deliberate exception: like `git init` it always creates
  here, and now prints a `note:` if that shadows a store further up.
- **Schema versioning.** The database now records which schema wrote it
  (`PRAGMA user_version`), and a file written by a newer retrial is refused at
  open time with a `SchemaVersionError` naming both versions, instead of being
  opened and misbehaving later. Databases recorded by 0.1.0 carry no stamp; that
  layout *is* v1, so they are adopted and labelled rather than rejected. There is
  a `_migrate` hook for the next bump.
- **Type hints throughout, and a `py.typed` marker.** The package is fully
  annotated and checked with mypy under `disallow_untyped_defs`. `retrial/types.py`
  declares every shape the public API returns — `Step`, `Session`,
  `TrajectoryEntry`, `DiffResult`, `BisectResult`, `AblateResult`, `SweepResult`,
  `RerunResult`, and the patch/check/agent types — all re-exported from the top
  level. The records are still plain dicts; a TypedDict is a dict at runtime, so
  nothing about their behaviour changed.
- `python -m retrial` as an entry point, matching the `retrial` console script.
- `retrial --version` (`-V`).
- Packaging metadata for PyPI: SPDX license, `LICENSE` file, authors, project
  URLs, classifiers, and keywords.
- CI across Python 3.10–3.13 on Linux, plus Windows and macOS at the ends of
  the range — the Windows jobs cover the cp1252 console-encoding paths.
- Tag-triggered PyPI publishing via OIDC trusted publishing.

### Changed
- **Renamed from `retrail` to `retrial`.** The package, the `retrial` command,
  the `.retrial/` store directory, and the `RETRIAL_*` environment variables all
  move together. Done before the first release on purpose: nothing is published,
  so this is a rename rather than a deprecation shim plus a squatted name kept
  alive forever. Nobody has a `.retrail/` to migrate except the author, who
  renames the directory by hand; the SQLite file itself is unchanged, since the
  old name was never written into it.
- Minimum Python is now 3.10. The previous `>=3.9` floor was declared but never
  tested, and 3.9 reached end of life in October 2025.
- The package version is single-sourced from `retrial/__init__.py`; the
  duplicate in `pyproject.toml` is gone.

### Fixed
- **A `Store` can be shared between threads.** Recording two runs at once — a
  web service, a harness evaluating prompts in parallel — used to fail as a raw
  `sqlite3.ProgrammingError` several frames deep, because the connection refused
  cross-thread use. Every method now holds a reentrant lock spanning the
  statement *and* its commit, so `check_same_thread=False` is safe. Three
  narrower races went with it: the process-wide store cache was a check-then-set
  that opened one connection per thread and orphaned all but one (measured: 8
  threads, 8 connections, 7 leaked); `fork()`'s handoff to the decorator was a
  module-level dict, so two concurrent forks could record one agent's steps
  under the other's session; and step numbers were read in one statement and
  written in another, which two writers on one session lose to a `UNIQUE`
  violation. `Store.add_step` now accepts `step_number=None` and allocates
  inside the lock — pass None unless you are reconstructing a specific
  numbering.
- **An async agent is refused instead of mis-recorded.** `@record` on an
  `async def` raised only by accident and only sometimes: calling the function
  returns a coroutine without running the body, so the session was stamped
  `complete` before the loop had taken a step, and the handler that marks a
  crashed run `failed` never saw the failure — it happened later, inside the
  event loop. An async agent that raised partway through was stored as a
  successful run. It is now an `IntegrationError` at decoration time, so the
  problem surfaces on import rather than after a run that looked fine. An async
  `call_model` or `execute_tools` inside a *sync* agent is refused at call time
  for the same reason; it previously failed further away, as "cannot serialize
  coroutine" from the serializer.
- **A run recorded from a subdirectory is no longer invisible.** `.retrial/` was
  resolved against the current working directory and nothing else, so `retrial
  log` one level down created a fresh database and truthfully reported no
  sessions while the real trace sat untouched one directory up. Recording split
  the same way — an agent launched from a subdirectory wrote somewhere the CLI
  would never look — and the two halves could disagree without either ever
  raising.

## [0.1.0]

First working version: record, fork, diff, bisect, ablate, sweep, rerun, and
cost, all backed by real re-execution against a live model. Validated against
`claude-opus-4-8`.
