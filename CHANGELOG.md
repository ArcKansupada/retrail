# Changelog

All notable changes to retrail are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0, the **public API is the names exported from `retrail/__init__.py`**
plus the CLI. The dict shapes those functions return (step records, session
records, diff/bisect results) are not yet frozen and may change in a minor
release; they will be pinned down with typed definitions before 1.0.

## [Unreleased]

### Added
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
