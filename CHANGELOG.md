# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from `1.0.0` onward. While the version is `0.x`, the API may change between minor
versions. Every change is recorded here with a migration note, and no deprecation
cycle is promised.

## [Unreleased]

## [0.1.0b1] - 2026-10-04

First beta. It adds response matching, seeded fake data, error assertions,
polling, and the full pytest plugin: configuration, fixtures, hooks, failure
reports and xdist support. The API may still change before `0.1.0`.

It still requires `graphql-core` 3.2. graphql-core 3.3 is not supported yet, and
the dependency stays declared as `>=3.2,<3.3`.

### Added

- Response matching. `gql.expect.User(name="Ann")` builds a matcher for a schema
  type, and an unknown field name fails when the matcher is built. Helpers:
  `contains`, `unordered`, `absent`, `any_value`, `any_length`, `length`,
  `matches`, `one_of`, `gt`, `gte`, `lt` and `lte`. `contains` and `unordered`
  pair items with elements by maximum bipartite matching, so duplicates on
  either side are handled and an item never takes the element another item
  needs. `NodeList.where(**filters)` and `NodeList.one(**filters)` match the
  named fields only, or exactly those fields with `strict=True`.
  `Matcher.explain()` renders the difference field by field, with every value
  scrubbed and redacted.
- `DeterministicRandom`, a sampler built on SHA-256 in counter mode, with `bits`,
  `below`, `choice`, `float_unit` and `sample_string`. It does not use `random`,
  so a seed gives the same values on every supported Python.
- `ScalarSpec` and `ScalarRegistry` for custom scalars. `ScalarSpec.fake` takes a
  `DeterministicRandom`. `ScalarRegistry.parsers()` gives the `name -> parse`
  mapping that response decoding already reads.
- `unique()`, which marks a field value that must differ on every call, every
  xdist worker and every run. The `"email"` kind uses the reserved domain
  `example.com`. Its state is one counter, so it does not grow with the number of
  tests.
- `ScalarNotRegisteredError` is exported from `pytest_graphql`. Its message shows
  where the factory needed the scalar and a `ScalarSpec` snippet that registers it.
- `gql.fake`: seeded input payloads for any input object type, with
  `_required_only`, `_depth` and overrides. The seed is `ClientConfig.seed` and the
  node id of the test. The pytest `gql` fixture supplies the node id, the run id and
  the xdist worker id. A client built with `build_client()` makes the same data on
  every run and unique values that differ on every run.
- `GraphQLClient` and `build_client()` take `fake_context=`, which holds the node id
  and the source of `unique()` values. Clones of a client share it, so their
  `unique()` values never repeat. `client.scalars` is the client's registry.
- Variable serialization. `ScalarSpec.serialize` now runs on every variable sent by
  `query()`, `mutation()` and `execute()`, for custom scalars at any depth: in
  lists, in input objects and in lists of input objects. The value on the wire must
  be JSON. A value that is not JSON, such as a `Decimal` with no registered spec,
  raises `ArgumentError` before anything is sent. The error names the variable path
  and the type of the value, never repeats the value, and carries no chained
  exception, so a message raised by `serialize` is in no traceback.
- Golden vectors in `tests/factory/golden.json`, format 1. They are the first
  recorded values of the seed formula, the sampler, every built-in scalar,
  `unique()` and nested input objects. A later change to any of them is a
  versioned change and is listed here.
- `gql.expect_error(code=, path=, message_matches=, count=)`, a context manager that
  asserts a block ends in a `GraphQLExecutionError`. It yields `CapturedErrors`
  with `.errors`, `.response` and `.first`. Every filter must match at least one
  error, and `count` is the exact number of errors returned. A block that does not raise, or a filter that matches nothing, raises
  `ExpectedErrorNotRaised`, which lists every error the server returned. Any
  other exception leaves the block unchanged.
- `gql.wait_until(name, until=, timeout=, interval=, backoff=, ignore=)`, for
  queries only. It has one deadline, always makes one attempt, and sleeps
  `min(interval * backoff ** (attempt - 1), remaining)`. `ignore` takes
  `Exception` subclasses only, so `KeyboardInterrupt` is never swallowed. It
  raises `WaitTimeoutError` with the attempts, the elapsed time, the last response
  and the last ignored exception. The exception text is shown, and the
  exception chained as the cause, only after it was checked against the
  request of the attempt that raised it. A mutation name raises
  `ArgumentError`.
- `GraphQLExecutionError`, `GraphQLPartialDataError`, `ExpectedErrorNotRaised` and
  `WaitTimeoutError` are exported from `pytest_graphql`.
- pytest configuration. Every ini option of the specification except the schema cache
  options has an environment variable, `PYTEST_GQL_` followed by the option name
  without `gql_`, so `gql_max_depth` is `PYTEST_GQL_MAX_DEPTH`. The flags are
  `--gql-url`, `--gql-seed=N|random`, `--gql-no-validate`, `--gql-max-depth` and
  `--gql-timeout`, and `--gql-log`, `--gql-log-level` and `--gql-show-schema-stats`,
  which are described below. Per setting, the order is: per-call
  argument, flag, fixture, environment variable, ini option, built-in default. A
  value that a source refuses stops the run at the start, and the message names the
  setting and the source and never shows the value. `gql_redact_headers` adds names to
  the default list, so `authorization`, `cookie`, `x-api-key` and `proxy-authorization`
  stay redacted whatever a project lists.
- The fixtures `gql_config`, `gql_schema_source`, `gql_schema`, `gql_scalars`,
  `gql_headers`, `gql_auth` and `gql_seed`, next to `gql`, `gql_url` and
  `gql_transport`. Each can be overridden. `gql_headers` is per test and sits above
  the ini and environment headers and below `gql_auth` and `with_headers()`. It never
  reaches schema loading. Overriding `gql_schema` sends no introspection request.
- `gql_schema_source` as a dotted path in the ini file or the environment. It names a
  `SchemaSource` instance. A missing module, a missing attribute and an object that is
  not a source each fail with a message that says which.
- Six hooks: `pytest_graphql_configure`, `pytest_graphql_schema_loaded`,
  `pytest_graphql_before_request`, `pytest_graphql_after_response`,
  `pytest_graphql_register_scalars` and `pytest_graphql_report_section`. The request
  and response hooks fold: each implementation receives the result of the one before.
  A scalar registered twice by hooks replaces the first and warns. The report section
  hook runs for each failed test that made a call and received a response, and each
  result becomes a section of its own, in hook order.
- Reporting. A failed test that made a GraphQL call carries a `GraphQL calls` section
  in its report. It lists every call the test made, up to `max_recorded_calls`: the
  kind and name, status, duration, document, variables, the fields automatic selection
  left out, the data (cut at `max_diagnostic_bytes`, with the field count), and the
  errors (the first `max_recorded_errors`, with the number left out). The last call is
  marked `<-- FAILED HERE` and has a `reproduce:` line with its `curl` command. Redacted
  values show as markers everywhere, including inside response data, and a line that
  would show a secret is withheld. `RecordedCall` gains `errors`, `data`, `data_fields`,
  `data_cut` and `curl`, built where the live request is in hand.
- A failed `actual == matcher` assertion shows the matcher diff of `Matcher.explain`
  in the pytest report, with the values scrubbed and redacted like every other output.
- A session header with the endpoint, the seed (and whether `random` chose it) and the
  run id. The schema is loaded when the first test needs it, so its fingerprint, type
  and operation counts and load time are printed in a `GraphQL` section at the end of
  the run. `--gql-show-schema-stats` adds the breakdown by kind and the field count.
- `--gql-log` logs every call to the logger `pytest_graphql.calls` at `INFO`.
  `--gql-log-level=summary` (the default) logs one line, and `full` adds the document,
  variables, skipped fields, data and errors. Only recorded, redacted text is logged.
- xdist. Each worker loads the schema once, and the end of the run lists each worker's
  schema and says so. `--gql-seed=random` is chosen once on the controller and every
  worker uses it. Every worker shares the xdist run id. `pytest-xdist` joins the `dev`
  extra and is not a dependency of the package.

### Changed

- `gql_url` and the `--gql-url` flag keep their behaviour. The URL can now also come
  from `PYTEST_GQL_URL` or the `gql_url` ini option, below the fixture and the flag.
  Migration: none.
- `GraphQLClient(parsers=...)` and `build_client(parsers=...)` are replaced by
  `scalars=`, a `ScalarRegistry`. One registry serves response decoding, variable
  serialization and `gql.fake`, and a scalar registered after the client is built is
  used on the next call. Migration: a mapping `{"Money": Decimal}` becomes
  `ScalarRegistry([ScalarSpec(name="Money", serialize=str, fake=..., parse=Decimal)])`.
- A variable is sent as it was serialized. Before, the value that the schema's
  coercion returned was sent, which put a custom scalar's parser output (for
  example a `Decimal`), an enum's internal value and a stored input field default
  on the wire. Now an input field the caller leaves out is not sent, so the server
  applies its default. `ID`, `Int` and `Float` values are still sent in their JSON
  form, and a single value for a list is still sent as a list of one.
- Field arguments of an explicit selection that hold a value with no GraphQL literal,
  such as a `Decimal` for a custom scalar, no longer raise a `TypeError` while the
  selection is normalized.
- `GraphQLExecutionError` and `GraphQLPartialDataError` raised by the client carry
  the `response` that failed and its `errors`. Building one from a message alone
  still works, and then `response` is `None`. Migration: none.

### Fixed

- The release workflow marks a PEP 440 prerelease tag (alpha, beta, release
  candidate or dev) as a GitHub prerelease, so it no longer becomes the latest
  release. `v0.1.0a1` had to be marked by hand. A tag that is not a PEP 440
  version now stops the release build.

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

[Unreleased]: https://github.com/skhomenko/pytest-graphql/compare/v0.1.0b1...HEAD
[0.1.0b1]: https://github.com/skhomenko/pytest-graphql/compare/v0.1.0a1...v0.1.0b1
[0.1.0a1]: https://github.com/skhomenko/pytest-graphql/releases/tag/v0.1.0a1
