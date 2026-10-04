"""The pytest layer.

This package is the only place in the distribution that may import pytest. It is
loaded through the ``pytest11`` entry point, which only pytest reads, so the
entry point is inert when pytest is absent.

M5d is the minimal vertical slice: the ``--gql-url`` flag, the ``gql_url`` and
``gql_transport`` session fixtures, and the function-scoped ``gql`` client. It
proves the fixture lifecycle in ``docs/reference/DESIGN_DECISIONS.md`` section
9 before later milestones depend on it. Every other option, fixture and hook
arrives at M9.

Ownership follows the 9.1 table. The session transport fixture owns the root
pool and closes it at session teardown. Each per-test client derives its own
transport from that pool when the transport supports derivation, owns that
derived transport only, and closes it at test teardown, whatever the test did.
An overridden ``gql_transport`` that does not support derivation is shared, and
the per-test client closes nothing it holds.

The ``gql`` client also gets what ``gql.fake`` needs. The node id is the pytest
node id of the test, so each test has its own seeded data. The run id and the
worker id belong to the session, so one ``UniqueSource`` serves every test of
the session in a process, and every clone of a test's client shares it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from functools import partial
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import (
    ClientConfig,
    GraphQLClient,
    _client_over,
    build_client,
)
from pytest_graphql._core.factory import FakeContext, UniqueSource
from pytest_graphql._core.lifecycle import Closable, _close_all, _report
from pytest_graphql._core.transport.base import Transport

#: The client the default ``gql_transport`` built, so the session schema
#: fixture reuses its schema instead of introspecting a second time.
_ROOT = pytest.StashKey[GraphQLClient]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("graphql", "GraphQL testing (pytest-graphql)")
    group.addoption(
        "--gql-url",
        dest="gql_url",
        default=None,
        metavar="URL",
        help="GraphQL endpoint. Overrides the gql_url fixture.",
    )


def _endpoint(config: pytest.Config, fixture_url: str) -> str:
    """The effective URL: the CLI flag above the fixture, per settings precedence."""
    flag: str | None = config.getoption("gql_url")
    return flag if flag else fixture_url


def _worker_input(config: Any, key: str) -> str | None:
    """A non-empty string from the xdist worker input, or ``None``."""
    workerinput = getattr(config, "workerinput", None)
    if isinstance(workerinput, Mapping):
        value = workerinput.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _worker_id(config: Any) -> str:
    """The xdist worker id, or ``"main"`` when the run is not distributed."""
    return (
        _worker_input(config, "workerid")
        or os.environ.get("PYTEST_XDIST_WORKER")
        or "main"
    )


def _run_id(config: Any) -> str:
    """The id of this run. xdist gives every worker the same one.

    Outside xdist it is new for each session, so ``unique()`` values differ
    between runs and still trace back to the run that made them.
    """
    return _worker_input(config, "testrunuid") or uuid.uuid4().hex


def _sweep(cleanup: list[Closable]) -> None:
    """Close every item once, through the shared sweep and report (9.3)."""
    errors = _close_all(cleanup)
    if errors:
        _report(errors, errors[-1])


@pytest.fixture(scope="session")
def gql_url(pytestconfig: pytest.Config) -> str:
    """The endpoint. Override this fixture for a URL known only at run time."""
    flag: str | None = pytestconfig.getoption("gql_url")
    if not flag:
        pytest.fail(
            "pytest-graphql: no GraphQL endpoint. Pass --gql-url=URL or "
            "override the gql_url fixture.",
            pytrace=False,
        )
    return flag


@pytest.fixture(scope="session")
def gql_transport(request: pytest.FixtureRequest, gql_url: str) -> Transport:
    """The session transport. It owns the root pool and closes it at session end.

    The cleanup list exists and its teardown is registered before any step that
    can fail, and the list is then passed to the factory (9.7). A failure while
    the factory builds the pool, loads the schema or derives the session
    transport is swept by the factory, and the teardown sweeps the same list
    again. Every wrapper on it closes at most once, so the pool is closed
    exactly once either way, including when this fixture raises.
    """
    cleanup: list[Closable] = []
    request.addfinalizer(partial(_sweep, cleanup))
    root = build_client(url=_endpoint(request.config, gql_url), cleanup=cleanup)
    request.config.stash[_ROOT] = root
    return root.transport


@pytest.fixture(scope="session")
def _gql_schema(
    pytestconfig: pytest.Config, gql_url: str, gql_transport: Transport
) -> GraphQLSchema:
    """The schema, loaded once per session.

    The default transport fixture already loaded it. An overridden transport
    fixture did not, so the schema is loaded over that transport, which this
    fixture neither owns nor closes.
    """
    root = pytestconfig.stash.get(_ROOT, None)
    if root is not None and root.transport is gql_transport:
        return root.schema
    url = _endpoint(pytestconfig, gql_url)
    return build_client(url=url, transport=gql_transport).schema


@pytest.fixture(scope="session")
def _gql_unique_source(pytestconfig: pytest.Config) -> UniqueSource:
    """The one source of ``unique()`` values for this session in this process."""
    return UniqueSource(_run_id(pytestconfig), _worker_id(pytestconfig))


@pytest.fixture
def gql(
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
    gql_url: str,
    gql_transport: Transport,
    _gql_schema: GraphQLSchema,
    _gql_unique_source: UniqueSource,
) -> Iterator[GraphQLClient]:
    """A client for this test, closed at teardown whether the test passed or not.

    It derives its own transport from the session pool when the session
    transport supports derivation, so cookies and identity never cross tests,
    and closing it closes that derived transport and never the pool. Over any
    other transport it shares the session transport and closes nothing. A
    failure while the client is built closes the derived transport before it
    leaves this fixture, because pytest runs no teardown for a fixture that
    raised before its ``yield``.
    """
    config = ClientConfig(url=_endpoint(pytestconfig, gql_url))
    fake_context = FakeContext(request.node.nodeid, _gql_unique_source)
    client = _client_over(
        gql_transport,
        lambda transport, owns: GraphQLClient(
            transport=transport,
            schema=_gql_schema,
            config=config,
            fake_context=fake_context,
            owns_transport=owns,
        ),
    )
    yield client
    client.close()
