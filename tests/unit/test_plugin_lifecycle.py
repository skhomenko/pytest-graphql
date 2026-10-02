"""The M5d fixture lifecycle, proven through ``pytester`` (9.1, C28).

Each test drives an inner pytest session in this process against a real local
GraphQL server or an in-process fake transport. The close methods of the root
pool and of every ``HttpxTransport`` are patched to append to one ordered event
list, and the inner tests append the transport their client used, so the outer
test sees when each test ran and when each resource closed.

The events hold the objects themselves rather than their ``id()``. A closed
per-test transport is freed once its test ends, and a later one could reuse
its address, which would make two different transports compare equal.

``test_httpx_transport.py`` reloads the transport module, so later in the same
process the factory's root transport and the transports derived from it can be
instances of two different ``HttpxTransport`` classes. The close patch goes on
every class the factory and the transport module currently name, and the type
checks use ``DerivableTransportBase``, which the reload does not replace.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, NoReturn

import httpx
import pytest
from graphql import GraphQLSchema, graphql_sync

from pytest_graphql import plugin
from pytest_graphql._core import client as client_module
from pytest_graphql._core.transport import httpx_transport
from pytest_graphql._core.transport.base import DerivableTransportBase
from tests.schema.resolvers import build_schema
from tests.unit import plugin_probe
from tests.unit.local_http_server import PlannedResponse, local_server

_JSON = (("Content-Type", "application/graphql-response+json"),)
_COOKIE = "session=m5d-cookie-value-0123"

#: The inner test files record the transport each client used.
_RECORD = """
from tests.unit import plugin_probe


def record(name, gql):
    plugin_probe.EVENTS.append(("test", name, gql.transport, gql.owns_transport))
"""


class _GraphQLServer:
    """Executes every request against the hostile schema, as a real server would."""

    def __init__(self, schema: GraphQLSchema, *, set_cookie: bool = False) -> None:
        self._schema = schema
        self._set_cookie = set_cookie
        self.seen: list[tuple[str | None, dict[str, str]]] = []

    def respond(self, body: bytes, headers: dict[str, str]) -> PlannedResponse:
        payload = json.loads(body)
        self.seen.append((payload.get("operationName"), headers))
        result = graphql_sync(
            self._schema,
            payload["query"],
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
        )
        envelope: dict[str, Any] = {"data": result.data}
        if result.errors:
            envelope["errors"] = [error.formatted for error in result.errors]
        extra = (("Set-Cookie", f"{_COOKIE}; Path=/"),) if self._set_cookie else ()
        return PlannedResponse(200, (*_JSON, *extra), json.dumps(envelope).encode())

    def introspections(self) -> int:
        return sum(1 for name, _ in self.seen if name == "IntrospectionQuery")


def _failing_introspection(_body: bytes, _headers: dict[str, str]) -> PlannedResponse:
    body = b'{"data": null, "errors": [{"message": "introspection disabled"}]}'
    return PlannedResponse(200, _JSON, body)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """Record every root pool opening and closing, and every transport close."""
    plugin_probe.EVENTS.clear()
    real_pool_init = httpx.HTTPTransport.__init__
    real_pool_close = httpx.HTTPTransport.close

    def pool_init(self: httpx.HTTPTransport, *args: Any, **kwargs: Any) -> None:
        real_pool_init(self, *args, **kwargs)
        plugin_probe.EVENTS.append(("pool-open", self))

    def pool_close(self: httpx.HTTPTransport) -> None:
        plugin_probe.EVENTS.append(("pool-close", self))
        real_pool_close(self)

    def recording(real_close: Any) -> Any:
        def transport_close(self: DerivableTransportBase) -> None:
            plugin_probe.EVENTS.append(("transport-close", self))
            real_close(self)

        return transport_close

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", pool_init)
    monkeypatch.setattr(httpx.HTTPTransport, "close", pool_close)
    classes = {client_module.HttpxTransport, httpx_transport.HttpxTransport}
    for cls in classes:
        monkeypatch.setattr(cls, "close", recording(cls.close))
    yield plugin_probe.EVENTS
    plugin_probe.EVENTS.clear()


def _of(events: list[tuple[Any, ...]], kind: str) -> list[tuple[Any, ...]]:
    return [event for event in events if event[0] == kind]


def _closed_at(events: list[tuple[Any, ...]], transport: object) -> list[int]:
    return [
        index
        for index, event in enumerate(events)
        if event[0] == "transport-close" and event[1] is transport
    ]


def _assert_one_pool_closed_last(events: list[tuple[Any, ...]]) -> None:
    """One root pool, closed exactly once, after every other event."""
    opened = _of(events, "pool-open")
    closed = _of(events, "pool-close")
    assert len(opened) == 1
    assert len(closed) == 1
    assert closed[0][1] is opened[0][1]
    assert events.index(closed[0]) == len(events) - 1


def test_per_test_clients_close_their_transports_and_the_pool_closes_once(
    pytester: pytest.Pytester, events: list[tuple[Any, ...]]
) -> None:
    server = _GraphQLServer(build_schema())
    pytester.makepyfile(
        test_inner=_RECORD
        + """

def test_one(gql):
    assert gql.query("pingScalar") is True
    record("one", gql)


def test_two(gql):
    assert gql.query("pingScalar") is True
    record("two", gql)


def test_three(gql):
    assert gql.query("pingScalar") is True
    record("three", gql)
"""
    )
    with local_server(server.respond) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(passed=3)

    # The schema is session-scoped: one introspection for three tests.
    assert server.introspections() == 1
    _assert_one_pool_closed_last(events)

    tests = _of(events, "test")
    assert [name for _, name, _, _ in tests] == ["one", "two", "three"]
    transports = [transport for _, _, transport, _ in tests]
    assert all(isinstance(t, DerivableTransportBase) for t in transports)
    assert all(owns for _, _, _, owns in tests)
    assert len({id(t) for t in transports}) == 3
    # Each test's transport closes once, after that test and before the next.
    for position, (event, transport) in enumerate(zip(tests, transports, strict=True)):
        (closed,) = _closed_at(events, transport)
        assert closed > events.index(event)
        if position + 1 < len(tests):
            assert closed < events.index(tests[position + 1])


def test_a_failing_test_closes_its_transport_and_not_the_pool(
    pytester: pytest.Pytester, events: list[tuple[Any, ...]]
) -> None:
    server = _GraphQLServer(build_schema())
    pytester.makepyfile(
        test_inner=_RECORD
        + """

def test_fails(gql):
    record("fails", gql)
    gql.query("pingScalar")
    raise AssertionError("deliberate failure")


def test_after(gql):
    assert gql.query("pingScalar") is True
    record("after", gql)
"""
    )
    with local_server(server.respond) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(passed=1, failed=1)

    failed, after = _of(events, "test")
    (closed,) = _closed_at(events, failed[2])
    assert events.index(failed) < closed < events.index(after)
    # The pool survived the failure: the next test used it, and it closed
    # once, at session end.
    _assert_one_pool_closed_last(events)


def test_a_failing_session_transport_fixture_closes_the_pool_it_created(
    pytester: pytest.Pytester, events: list[tuple[Any, ...]]
) -> None:
    pytester.makepyfile(
        test_inner="""
def test_one(gql):
    pass


def test_two(gql):
    pass
"""
    )
    with local_server(_failing_introspection) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*SchemaError*introspection disabled*"])
    # The pool was created before introspection failed, and closed once.
    _assert_one_pool_closed_last(events)


def test_a_failing_client_construction_closes_its_derived_transport(
    pytester: pytest.Pytester,
    events: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest runs no teardown for a fixture that raised before its ``yield``."""
    built: list[object] = []

    def failing_client(**kwargs: Any) -> NoReturn:
        built.append(kwargs["transport"])
        raise RuntimeError("client construction failed")

    monkeypatch.setattr(plugin, "GraphQLClient", failing_client)
    server = _GraphQLServer(build_schema())
    pytester.makepyfile(
        test_inner="""
def test_one(gql):
    pass


def test_two(gql):
    pass
"""
    )
    with local_server(server.respond) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*client construction failed*"])

    # Each test derived its own transport, and each was closed once, while
    # the pool stayed open for the next test.
    assert len(built) == 2
    assert built[0] is not built[1]
    for transport in built:
        assert len(_closed_at(events, transport)) == 1
    _assert_one_pool_closed_last(events)


def test_the_cli_flag_overrides_an_overridden_url_fixture(
    pytester: pytest.Pytester,
) -> None:
    server = _GraphQLServer(build_schema())
    pytester.makeconftest(
        """
import pytest


@pytest.fixture(scope="session")
def gql_url():
    return "http://127.0.0.1:9/not-this-one"
"""
    )
    pytester.makepyfile(
        test_inner="""
def test_query(gql):
    assert gql.query("pingScalar") is True
"""
    )
    with local_server(server.respond) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(passed=1)
    # Introspection and the query both reached the flag's server.
    assert [name for name, _ in server.seen] == ["IntrospectionQuery", "pingScalar"]


def test_an_overridden_url_fixture_is_used_without_the_flag(
    pytester: pytest.Pytester,
) -> None:
    server = _GraphQLServer(build_schema())
    with local_server(server.respond) as url:
        pytester.makeconftest(
            f"""
import pytest


@pytest.fixture(scope="session")
def gql_url():
    return {url!r}
"""
        )
        pytester.makepyfile(
            test_inner="""
def test_query(gql):
    assert gql.query("pingScalar") is True
"""
        )
        result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert server.introspections() == 1


def test_a_missing_url_is_a_clear_setup_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_inner="""
def test_query(gql):
    pass
"""
    )
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*no GraphQL endpoint*--gql-url=URL*"])


def test_an_overridden_plain_transport_is_shared_and_never_closed(
    pytester: pytest.Pytester,
) -> None:
    plugin_probe.EVENTS.clear()
    pytester.makeconftest(
        """
import pytest

from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema
from tests.unit import plugin_probe


@pytest.fixture(scope="session")
def gql_transport():
    transport = FakeGraphQLTransport(build_schema())
    plugin_probe.EVENTS.append(("fake", transport))
    return transport
"""
    )
    pytester.makepyfile(
        test_inner=_RECORD
        + """

def test_one(gql):
    assert gql.query("pingScalar") is True
    record("one", gql)


def test_two(gql):
    assert gql.query("pingScalar") is True
    record("two", gql)
"""
    )
    # The URL is only a label here: the fake transport opens no socket.
    result = pytester.runpytest("--gql-url=http://127.0.0.1:9/graphql")
    result.assert_outcomes(passed=2)

    (fake_event,) = _of(plugin_probe.EVENTS, "fake")
    fake = fake_event[1]
    for _, _, transport, owns in _of(plugin_probe.EVENTS, "test"):
        assert transport is fake
        assert owns is False
    assert fake.close_calls == 0
    introspections = [r for r in fake.sent if r.operation == "IntrospectionQuery"]
    assert len(introspections) == 1
    plugin_probe.EVENTS.clear()


def test_identity_does_not_cross_tests(pytester: pytest.Pytester) -> None:
    """C4: one test's auth, headers and cookies never reach the next test."""
    server = _GraphQLServer(build_schema(), set_cookie=True)
    pytester.makepyfile(
        test_inner="""
def test_first(gql):
    with gql.as_("first-token-0123").with_headers({"X-Tenant": "first"}) as client:
        assert client.query("pingScalar") is True
        assert client.query("pingScalar") is True


def test_second(gql):
    assert gql.query("pingScalar") is True
"""
    )
    with local_server(server.respond) as url:
        result = pytester.runpytest(f"--gql-url={url}")
    result.assert_outcomes(passed=2)

    calls = [headers for name, headers in server.seen if name != "IntrospectionQuery"]
    assert len(calls) == 3
    first, again, second = calls
    assert first["authorization"] == "Bearer first-token-0123"
    assert first["x-tenant"] == "first"
    # The default cookie scope keeps no cookie, even inside one test.
    assert "cookie" not in again
    assert "authorization" not in second
    assert "x-tenant" not in second
    assert "cookie" not in second


def test_the_plugin_registers_the_flag(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(["*--gql-url=URL*"])
