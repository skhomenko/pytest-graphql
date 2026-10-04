"""The fixtures (SPEC 7.4, DESIGN sections 2, 8 and 9).

The session fixtures, each after the ones it reads:

- ``gql_config``, then ``gql_url``, then ``_gql_session_config``, which applies
  the CLI flags and runs ``pytest_graphql_configure``.
- ``gql_schema_source``, then ``gql_transport`` (which reads both of the above
  and opens the pool), then ``gql_schema``, which loads the schema over the
  transport, then ``_gql_ready_schema``, which runs
  ``pytest_graphql_schema_loaded``.
- ``gql_scalars``, then ``_gql_ready_scalars``, which runs
  ``pytest_graphql_register_scalars``.

The function-scoped ``gql`` reads all of those, plus ``gql_headers``,
``gql_auth`` and ``gql_seed``.

Every public fixture can be overridden. The ``_gql_*`` fixtures sit after one
that a user can override, and run what must run whatever the override returns:
the CLI flags, and the hooks. So a project that replaces ``gql_config`` or
``gql_scalars`` still gets its flags and its plugins' hooks.

Ownership follows 9.1. The session transport fixture owns the root pool and
closes it at session teardown. Each per-test client derives its own transport
from that pool when the transport supports derivation, owns that derived
transport only, and closes it at test teardown, whatever the test did. An
overridden ``gql_transport`` that does not support derivation is shared, and
the per-test client closes nothing it holds. No fixture added after M5d opens a
resource of its own: everything one of them reads is settled before
``gql_transport`` or ``gql`` takes its first step.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator, Mapping
from functools import partial
from typing import cast

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.auth import Auth
from pytest_graphql._core.auth import as_ as resolve_auth
from pytest_graphql._core.client import (
    ClientConfig,
    GraphQLClient,
    _client_over,
    _derivation_of,
    _derive,
    _load_schema,
    _new_root_pool,
)
from pytest_graphql._core.errors import SchemaError
from pytest_graphql._core.factory import FakeContext, ScalarRegistry, UniqueSource
from pytest_graphql._core.headers import merge_headers
from pytest_graphql._core.lifecycle import Closable, _close_all, _Owned, _report
from pytest_graphql._core.schema.source import SchemaSource
from pytest_graphql._core.transport.base import Transport
from pytest_graphql.plugin import reporting
from pytest_graphql.plugin.hookspecs import HookMiddleware, ReplacingRegistry
from pytest_graphql.plugin.options import Settings, settings_of
from pytest_graphql.plugin.session import run_id as _run_id
from pytest_graphql.plugin.session import worker_id as _worker_id

_NO_ENDPOINT = (
    "pytest-graphql: no GraphQL endpoint. Pass --gql-url=URL, set the "
    "PYTEST_GQL_URL environment variable or the gql_url ini option, or "
    "override the gql_url fixture."
)


# -- shared helpers -----------------------------------------------------------


def _sweep(cleanup: list[Closable]) -> None:
    """Close every item once, through the shared sweep and report (9.3)."""
    errors = _close_all(cleanup)
    if errors:
        _report(errors, errors[-1])


def _client_class() -> type[GraphQLClient]:
    """The class each per-test client is built from.

    It is read from the entry module on every call, so the constructor can be
    substituted in one place, the one a test patches to fail a construction.
    """
    from pytest_graphql import plugin

    return plugin.GraphQLClient


# -- configuration ------------------------------------------------------------


@pytest.fixture(scope="session")
def gql_config(pytestconfig: pytest.Config) -> ClientConfig:
    """The configuration: the built-in defaults, then ini options, then
    environment variables.

    Override it to change anything at once. A CLI flag still applies on top of
    whatever this returns, because a flag outranks a fixture. A project that
    only adjusts a few fields requests the original, which pytest allows when
    a fixture of the same name asks for it::

        @pytest.fixture(scope="session")
        def gql_config(gql_config):
            return dataclasses.replace(gql_config, max_depth=5)
    """
    return settings_of(pytestconfig).base_config()


@pytest.fixture(scope="session")
def gql_url(pytestconfig: pytest.Config, gql_config: ClientConfig) -> str:
    """The endpoint. Override this fixture for a URL known only at run time.

    By default it is the ``--gql-url`` flag, else the URL of ``gql_config``,
    which the environment variable or the ini option supplied. The flag
    outranks an overridden fixture too: the client uses it either way.
    """
    url = settings_of(pytestconfig).cli_values.get("url") or gql_config.url
    if not url:
        pytest.fail(_NO_ENDPOINT, pytrace=False)
    return str(url)


@pytest.fixture(scope="session")
def _gql_session_config(
    pytestconfig: pytest.Config, gql_config: ClientConfig, gql_url: str
) -> ClientConfig:
    """The configuration every client of the session starts from.

    The fixtures are applied first, then the CLI flags, so a flag outranks
    them, then ``pytest_graphql_configure``, which sees the result and may
    change it. It is a copy, so a hook never edits the user's fixture value or
    the settings.
    """
    if not isinstance(gql_config, ClientConfig):
        pytest.fail(
            "pytest-graphql: the gql_config fixture must return a ClientConfig, "
            f"not {type(gql_config).__name__}.",
            pytrace=False,
        )
    config = settings_of(pytestconfig).apply_cli(
        dataclasses.replace(gql_config, url=gql_url)
    )
    pytestconfig.hook.pytest_graphql_configure(config=config)
    if not config.url:
        pytest.fail(_NO_ENDPOINT, pytrace=False)
    return config


# -- the schema source and the schema -----------------------------------------


class EndpointIntrospection:
    """The default source: introspection over the session transport.

    An ``IntrospectionSource`` needs a transport, and the source is read
    before the schema is loaded, so the default cannot be a ready
    ``IntrospectionSource``. ``gql_schema`` sees this class and introspects over
    the session transport. It is never asked to load itself.
    """

    fingerprint = "introspection:endpoint"

    def load(self) -> GraphQLSchema:
        raise SchemaError(
            "the default schema source loads through the session transport. "
            "Use the gql_schema fixture, or override gql_schema_source."
        )


def _import_source(path: str, where: str) -> SchemaSource:
    """The ``SchemaSource`` instance a dotted path names.

    Importing runs user code, so it happens here, when a test first needs the
    source, and not while pytest is still configuring. The three ways a path
    can fail each get their own message. An exception raised by the imported
    module itself is the user's own failure and propagates unchanged.
    """
    module_name, _, attribute = path.rpartition(".")
    problem = f"pytest-graphql: gql_schema_source, set by {where}, names {path!r}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as missing:
        owner = missing.name or ""
        if module_name == owner or module_name.startswith(owner + "."):
            pytest.fail(
                f"{problem}, but there is no module named {module_name!r}.",
                pytrace=False,
            )
        raise
    try:
        source = getattr(module, attribute)
    except AttributeError:
        pytest.fail(
            f"{problem}, but module {module_name!r} has no attribute {attribute!r}.",
            pytrace=False,
        )
    loader = getattr(source, "load", None)
    if (
        isinstance(source, type)
        or not callable(loader)
        or not isinstance(getattr(source, "fingerprint", None), str)
    ):
        hint = (
            " It is a class. Point at an instance of it."
            if isinstance(source, type)
            else ""
        )
        pytest.fail(
            f"{problem}, which is not a SchemaSource. A SchemaSource has a "
            f"load() method and a fingerprint string.{hint}",
            pytrace=False,
        )
    return cast("SchemaSource", source)


@pytest.fixture(scope="session")
def gql_schema_source(pytestconfig: pytest.Config) -> SchemaSource:
    """Where the schema comes from. Override it to load SDL or a custom source.

    By default it is the instance named by the ``gql_schema_source`` ini option
    or its environment variable, and introspection of the endpoint when neither
    is set.
    """
    settings = settings_of(pytestconfig)
    if settings.schema_source is None:
        return EndpointIntrospection()
    return _import_source(settings.schema_source, settings.schema_source_where or "")


def _factory_source(source: SchemaSource) -> SchemaSource | None:
    """The source the schema loader takes: ``None`` means introspect."""
    return None if isinstance(source, EndpointIntrospection) else source


def _open_session_transport(config: ClientConfig, cleanup: list[Closable]) -> Transport:
    """The root pool and the session transport derived from it, both in ``cleanup``.

    Each is adopted by the list before it is acquired (9.2). Nothing here asks
    the endpoint for anything, so a project that supplies its own schema opens
    no schema request. If acquiring either raises, the list is swept through
    the one sweep and report (9.3) before the error leaves.
    """
    try:
        pool = _Owned(cleanup, _new_root_pool, config)
        return _Owned(cleanup, _derive, pool.value, own_pool=False).value
    except BaseException as failure:
        errors = _close_all(cleanup)
        if errors:
            _report(errors, failure)
        raise


@pytest.fixture(scope="session")
def gql_transport(
    request: pytest.FixtureRequest,
    _gql_session_config: ClientConfig,
    gql_schema_source: SchemaSource,  # noqa: ARG001  (settled before a pool opens)
) -> Transport:
    """The session transport. It owns the root pool and closes it at session end.

    The cleanup list exists and its teardown is registered before any step that
    can fail (9.7). A failure while the pool is built or the transport derived
    is swept at once, and the teardown sweeps the same list again. Every
    wrapper on it closes at most once, so the pool is closed exactly once
    either way, including when this fixture raises.

    This fixture does not load the schema. ``gql_schema`` does, so a project
    that overrides ``gql_schema`` never sends an introspection request. It
    still asks for the schema source, which does no I/O, so a source that
    cannot be used stops the run before any pool opens.
    """
    cleanup: list[Closable] = []
    request.addfinalizer(partial(_sweep, cleanup))
    return _open_session_transport(_gql_session_config, cleanup)


def _load_over_probe(
    transport: Transport, config: ClientConfig, source: SchemaSource | None
) -> GraphQLSchema:
    """Load the schema over a throwaway transport derived from ``transport``.

    Cookies the endpoint sets while the schema loads stay on the probe, which
    is closed before this returns, so they never reach a test (C17). A
    transport that cannot derive is shared and used as it is, and closed by
    nobody here.
    """
    url = str(config.url)
    derive = _derivation_of(transport)
    if derive is None:
        return _load_schema(transport, url, config, source)
    cleanup: list[Closable] = []
    try:
        schema = _load_schema(_Owned(cleanup, derive).value, url, config, source)
    except BaseException as failure:
        errors = _close_all(cleanup)
        if errors:
            _report(errors, failure)
        raise
    _sweep(cleanup)
    return schema


@pytest.fixture(scope="session")
def gql_schema(
    _gql_session_config: ClientConfig,
    gql_transport: Transport,
    gql_schema_source: SchemaSource,
) -> GraphQLSchema:
    """The schema, loaded once per session, and once per xdist worker.

    It is loaded from ``gql_schema_source`` over a probe derived from the
    session transport, which the probe leaves open. Override this fixture to
    supply a schema directly: nothing then asks the endpoint for one.
    """
    return _load_over_probe(
        gql_transport, _gql_session_config, _factory_source(gql_schema_source)
    )


@pytest.fixture(scope="session")
def _gql_ready_schema(
    pytestconfig: pytest.Config,
    _gql_session_config: ClientConfig,
    gql_schema: GraphQLSchema,
    gql_schema_source: SchemaSource,
) -> GraphQLSchema:
    """The schema, after ``pytest_graphql_schema_loaded`` ran once for it.

    It also keeps the facts the report prints about the schema, once for this
    process, which is once for each xdist worker.
    """
    pytestconfig.hook.pytest_graphql_schema_loaded(
        schema=gql_schema, source=gql_schema_source
    )
    reporting.record_schema(
        pytestconfig, gql_schema, gql_schema_source, _gql_session_config
    )
    return gql_schema


# -- scalars ------------------------------------------------------------------


@pytest.fixture(scope="session")
def gql_scalars() -> ScalarRegistry:
    """The custom scalars of the session. Register them here, or in the hook."""
    return ScalarRegistry()


@pytest.fixture(scope="session")
def _gql_ready_scalars(
    pytestconfig: pytest.Config, gql_scalars: ScalarRegistry
) -> ScalarRegistry:
    """The registry, after every ``pytest_graphql_register_scalars`` ran on it."""
    if not isinstance(gql_scalars, ScalarRegistry):
        pytest.fail(
            "pytest-graphql: the gql_scalars fixture must return a "
            f"ScalarRegistry, not {type(gql_scalars).__name__}.",
            pytrace=False,
        )
    pytestconfig.hook.pytest_graphql_register_scalars(
        registry=ReplacingRegistry(gql_scalars)
    )
    return gql_scalars


@pytest.fixture(scope="session")
def _gql_unique_source(pytestconfig: pytest.Config) -> UniqueSource:
    """The one source of ``unique()`` values for this session in this process."""
    return UniqueSource(_run_id(pytestconfig), _worker_id(pytestconfig))


# -- identity and data, per test ----------------------------------------------


@pytest.fixture
def gql_headers() -> dict[str, str]:
    """Headers for this test only. Override it per module, class or test.

    They sit above ``ClientConfig.headers`` and below ``gql_auth`` (C4), and
    they never reach schema loading, because the schema is session-scoped.
    """
    return {}


@pytest.fixture
def gql_auth() -> Auth | str | None:
    """The identity of this test, or ``None`` for none. A string is a bearer token."""
    return None


@pytest.fixture
def gql_seed(_gql_session_config: ClientConfig) -> int:
    """The factory seed for this test, before it is mixed with the node id.

    By default it is the session's effective seed, which is the ``--gql-seed``
    flag, else the environment variable, else the ini option, else ``0``.
    Override it to give one test its own data. The flag outranks an override.
    """
    return _gql_session_config.seed


def _seed_of(settings: Settings, fixture_value: object) -> int:
    """The flag, then the fixture: a flag outranks a fixture (A3)."""
    if "seed" in settings.cli_values:
        return int(settings.cli_values["seed"])
    if isinstance(fixture_value, bool) or not isinstance(fixture_value, int):
        pytest.fail(
            "pytest-graphql: the gql_seed fixture must return an int, not "
            f"{type(fixture_value).__name__}.",
            pytrace=False,
        )
    return fixture_value


def _test_config(
    session: ClientConfig, settings: Settings, headers: object, seed: object
) -> ClientConfig:
    """The configuration of one test's client.

    ``gql_headers`` joins ``ClientConfig.headers`` under the C4 rule. The
    schema identity is pinned first, so a per-test header can never become the
    identity the session schema was loaded with.
    """
    if not isinstance(headers, Mapping):
        pytest.fail(
            "pytest-graphql: the gql_headers fixture must return a mapping of "
            f"header names to values, not {type(headers).__name__}.",
            pytrace=False,
        )
    schema_headers = (
        session.headers if session.schema_headers is None else session.schema_headers
    )
    return dataclasses.replace(
        session,
        headers=merge_headers(session.headers, headers),
        schema_headers=schema_headers,
        seed=_seed_of(settings, seed),
    )


@pytest.fixture
def gql(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    _gql_session_config: ClientConfig,
    gql_transport: Transport,
    _gql_ready_schema: GraphQLSchema,
    _gql_ready_scalars: ScalarRegistry,
    _gql_unique_source: UniqueSource,
    gql_headers: dict[str, str],
    gql_auth: Auth | str | None,
    gql_seed: int,
) -> Iterator[GraphQLClient]:
    """A client for this test, closed at teardown whether the test passed or not.

    It derives its own transport from the session pool when the session
    transport supports derivation, so cookies and identity never cross tests,
    and closing it closes that derived transport and never the pool. Over any
    other transport it shares the session transport and closes nothing. A
    failure while the client is built closes the derived transport before it
    leaves this fixture, because pytest runs no teardown for a fixture that
    raised before its ``yield``.

    Everything the client reads is settled before the transport is derived, so
    a failing ``gql_headers``, ``gql_auth`` or ``gql_seed`` fixture has nothing
    to close.
    """
    config = _test_config(
        _gql_session_config, settings_of(pytestconfig), gql_headers, gql_seed
    )
    auth = None if gql_auth is None else resolve_auth(gql_auth)
    fake_context = FakeContext(request.node.nodeid, _gql_unique_source)
    settings = settings_of(pytestconfig)
    trace = reporting.CallTrace(
        request.node.nodeid,
        config,
        reporting.log_sink(settings.log_level, config) if settings.log else None,
        reporting.session_ledger(pytestconfig),
    )
    middleware = (HookMiddleware(pytestconfig, trace),)
    client = _client_over(
        gql_transport,
        lambda transport, owns: _client_class()(
            transport=transport,
            schema=_gql_ready_schema,
            config=config,
            scalars=_gql_ready_scalars,
            fake_context=fake_context,
            middleware=middleware,
            auth=auth,
            owns_transport=owns,
            recorder=trace.recorder,
        ),
    )
    # The report of a failed test reads this, and the protocol hook clears it
    # once all three reports of the test are made.
    pytestconfig.stash[reporting.CURRENT] = trace
    yield client
    client.close()
