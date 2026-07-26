# Changelog

All notable changes to retrail are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0, the **public API is the names exported from `retrail/__init__.py`**
plus the CLI. The dict shapes those functions return are now declared in
`retrail/types.py`; before 1.0 they may **gain** keys in a minor release, but an
existing key will not silently change meaning.

## [Unreleased]

### Added
- **Schema versioning.** The database now records which schema wrote it
  (`PRAGMA user_version`), and a file written by a newer retrail is refused at
  open time with a `SchemaVersionError` naming both versions, instead of being
  opened and misbehaving later. Databases recorded by 0.1.0 carry no stamp; that
  layout *is* v1, so they are adopted and labelled rather than rejected. There is
  a `_migrate` hook for the next bump.
- **Type hints throughout, and a `py.typed` marker.** The package is fully
  annotated and checked with mypy under `disallow_untyped_defs`. `retrail/types.py`
  declares every shape the public API returns — `Step`, `Session`,
  `TrajectoryEntry`, `DiffResult`, `BisectResult`, `AblateResult`, `SweepResult`,
  `RerunResult`, and the patch/check/agent types — all re-exported from the top
  level. The records are still plain dicts; a TypedDict is a dict at runtime, so
  nothing about their behaviour changed.
- `python -m retrail` as an entry point, matching the `retrail` console script.
- `retrail --version` (`-V`).
- Packaging metadata for PyPI: SPDX license, `LICENSE` file, authors, project
  URLs, classifiers, and keywords.
- CI across Python 3.10–3.13 on Linux, plus Windows and macOS at the ends of
  the range — the Windows jobs cover the cp1252 console-encoding paths.
- Tag-triggered PyPI publishing via OIDC trusted publishing.

### Changed
- Minimum Python is now 3.10. The previous `>=3.9` floor was declared but never
  tested, and 3.9 reached end of life in October 2025.
- The package version is single-sourced from `retrail/__init__.py`; the
  duplicate in `pyproject.toml` is gone.

## [0.1.0]

First working version: record, fork, diff, bisect, ablate, sweep, rerun, and
cost, all backed by real re-execution against a live model. Validated against
`claude-opus-4-8`.
