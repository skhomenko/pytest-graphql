# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from `1.0.0` onward. While the version is `0.x`, the API may change between minor
versions. Every change is recorded here with a migration note, and no deprecation
cycle is promised.

## [Unreleased]

### Added

- Packaging skeleton: `pyproject.toml` with a hatchling backend, PEP 621
  metadata and a `src/` layout, the `pytest_graphql` package with `__version__`
  and `py.typed`, and the `pytest11` entry point.
- Tooling configuration: ruff lint and format, `mypy --strict` on `src/`, and
  pytest defaults.
- `scripts/check_core_purity.py`, which fails when any module outside the
  pytest plugin package imports pytest.
- The `test` workflow, running the representative CI matrix on Linux, the test
  suite on macOS and Windows at the newest supported Python, the core purity
  check and the no-pytest install job.
- The `release` workflow, which builds one artifact set, records a digest for
  every file, runs the artifact checks, and verifies the release back from
  TestPyPI before any upload to PyPI. It publishes nothing until the first
  release gate.
- `CONTRIBUTING.md` with the development setup and the release checklist.
- `requirements/build.in` and a pinned `requirements/build.txt` covering the
  build backend and its dependencies, used both as build constraints for the
  release build and as the environment for the sdist install-back.

[Unreleased]: https://github.com/skhomenko/pytest-graphql/compare/main...HEAD
