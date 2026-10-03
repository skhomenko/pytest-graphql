# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from `1.0.0` onward. While the version is `0.x`, the API may change between minor
versions. Every change is recorded here with a migration note, and no deprecation
cycle is promised.

## [Unreleased]

## [0.1.0a1] - 2026-10-03

First alpha. It contains a working client, transport, response model and one
pytest fixture.

It requires `graphql-core` 3.2. graphql-core 3.3 is not supported yet, and the
dependency is declared as `>=3.2,<3.3` so that an install never selects it.

### Added

- `GraphQLClient`, `ClientConfig` and `build_client()`. `query()` and
  `mutation()` call an operation by name and build its selection set from the
  schema. `execute()` sends a raw document and returns the response. Names are
  snake_case in Python and the schema's own spelling on the wire.
- Automatic selection with depth, field count and cycle limits, set through
  `SelectionPolicy` and `CyclePolicy`. Explicit selections use `Selection`,
  `Field` and `AUTO`. A field left out of an automatic selection is recorded,
  not dropped silently.
- Local validation of each operation against the schema before it is sent, on
  by default. `validate=False` turns it off. Variables are always checked
  against their declared types.
- Schema loading through `IntrospectionSource` or `SDLFileSource`.
- `HttpxTransport`, behind the `Transport` protocol. Requests are always a JSON
  `POST`. Timeouts, connect retries and the response size are bounded. Redirects
  are not followed, and proxy and other environment settings are ignored unless
  `trust_env=True`.
- Connection isolation with the default `HttpxTransport`. The test clients of
  one pytest session share one connection pool, and a clone shares the pool of
  the client it came from. Each `build_client()` call without `transport=`
  creates its own pool. Every such client has its own cookie jar. Under the
  default `cookie_scope="none"`, no cookie survives a call. A transport you
  supply, through `build_client(transport=...)` or an overridden
  `gql_transport` fixture, keeps its own connection and cookie behavior.
- `GraphQLResponse`, `Node` and `NodeList`. Data is converted using the schema,
  and errors and partial data raise unless that is turned off.
- Diagnostics. `DiagnosticSnapshot` and `as_curl()` describe a call with
  headers, variables and known credential values redacted. Recorded calls are
  bounded, and truncation is always stated.
- `Auth`, `BearerAuth` and `HeaderAuth`, and the `Middleware` protocol with
  `BaseMiddleware`. `with_headers()`, `with_auth()`, `as_()` and `anonymous()`
  return a client with another identity.
- Ownership rules for closing resources. Closing a client closes what it owns,
  exactly once. A failure while building a client closes everything built so
  far.
- The pytest fixtures `gql_url`, `gql_transport` and `gql`, and the
  `--gql-url` flag. The schema loads once per session. Each test gets its own
  client, which is closed after the test, whether it passed or failed.
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

[Unreleased]: https://github.com/skhomenko/pytest-graphql/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/skhomenko/pytest-graphql/releases/tag/v0.1.0a1
