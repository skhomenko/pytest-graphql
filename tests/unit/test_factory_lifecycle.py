"""The factory's construction and cleanup failure paths (9.1 to 9.3).

``build_client`` acquires three resources in order: the root pool, a probe
transport derived from it for introspection, and the client's own derived
transport. Each is adopted by the cleanup list before it is acquired. These
tests drive a failure into every step and a failure into every close, and
check the one property that covers all of them: every resource the factory
acquired is closed exactly once on every path out of the call, by the
factory when the call fails and by the returned client when it succeeds.

The exception that leaves follows 9.3. An interrupt always wins. Otherwise
the factory raises the construction failure and the client raises the
failure of its earliest-acquired resource, and every other failure stays
reachable through ``reported_errors``.

The root pool is replaced with a fake that logs what it is asked to do, so
these tests open no socket. The last two tests run the real ``HttpxTransport``
over the same paths, still without a request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import client as factory
from pytest_graphql._core.client import ClientConfig, build_client
from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.errors import ArgumentError, SchemaError
from pytest_graphql._core.lifecycle import _close_all, reported_errors
from pytest_graphql._core.transport.base import RawResponse
from pytest_graphql._core.transport.httpx_transport import HttpxTransport
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema
from tests.unit.lifecycle_harness import Failure, Interrupt

URL = "https://example.test/graphql"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


# -- a root pool that logs what it is asked to do -----------------------------


class _Ledger:
    """What the fakes were asked to do, in order, and where each one fails.

    ``fail_at`` names the acquisition step that raises ``failure``: ``root``,
    ``probe``, ``schema`` or ``owner``. ``close_failures`` maps a resource
    name to what its ``close()`` raises.
    """

    def __init__(
        self,
        schema: GraphQLSchema,
        *,
        fail_at: str | None = None,
        failure: BaseException | None = None,
        close_failures: Mapping[str, BaseException] | None = None,
    ) -> None:
        self.schema = schema
        self.fail_at = fail_at
        self.failure = failure
        self.close_failures = dict(close_failures or {})
        self.events: list[str] = []
        self.own_pool: list[bool] = []
        self._derived = iter(("probe", "owner"))

    def step(self, name: str) -> None:
        if self.fail_at == name:
            assert self.failure is not None
            raise self.failure
        self.events.append(f"acquire {name}")

    def new_root(self, config: ClientConfig) -> _Root:  # noqa: ARG002 -- the factory's own shape
        self.step("root")
        return _Root(self, "root")

    def closes(self) -> list[str]:
        return [event for event in self.events if event.startswith("close ")]


class _Logged(FakeGraphQLTransport):
    def __init__(self, ledger: _Ledger, name: str) -> None:
        super().__init__(ledger.schema)
        self.ledger = ledger
        self.name = name

    def close(self) -> None:
        super().close()
        self.ledger.events.append(f"close {self.name}")
        failure = self.ledger.close_failures.get(self.name)
        if failure is not None:
            raise failure


class _Root(_Logged):
    def derive(self, *, own_pool: bool = False) -> _Logged:
        name = next(self.ledger._derived)
        self.ledger.own_pool.append(own_pool)
        self.ledger.step(name)
        return _Logged(self.ledger, name)


class _Source:
    """A schema source whose ``load()`` is the ``schema`` acquisition step."""

    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger

    def load(self) -> GraphQLSchema:
        if self.ledger.fail_at == "schema":
            assert self.ledger.failure is not None
            raise self.ledger.failure
        return self.ledger.schema


class _RefusedMiddleware:
    """A middleware sequence that cannot be read, so the constructor raises."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def __iter__(self) -> Iterator[Any]:
        raise self.failure


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(ledger: _Ledger) -> _Ledger:
        monkeypatch.setattr(factory, "_new_root_pool", ledger.new_root)
        return ledger

    return _install


def _build(ledger: _Ledger, **options: Any) -> Any:
    return build_client(url=URL, schema_source=_Source(ledger), **options)


# -- the success path ---------------------------------------------------------


def test_the_returned_client_closes_every_resource_exactly_once(
    schema: GraphQLSchema, install: Any
) -> None:
    ledger = install(_Ledger(schema))

    client = _build(ledger)
    assert ledger.events == [
        "acquire root",
        "acquire probe",
        "close probe",  # on the success path, before the owner is derived
        "acquire owner",
    ]

    client.close()

    assert ledger.closes() == ["close probe", "close owner", "close root"]
    assert client.owns_transport is True


def test_every_transport_the_factory_derives_leaves_the_pool_to_the_list(
    schema: GraphQLSchema, install: Any
) -> None:
    # 9.1: the factory's cleanup list owns the root pool, so no derived
    # transport is granted it.
    ledger = install(_Ledger(schema))

    _build(ledger).close()

    assert ledger.own_pool == [False, False]


def test_the_client_list_closes_the_transport_the_client_declares(
    schema: GraphQLSchema, install: Any
) -> None:
    # The constructor does not check a supplied list (9.1); this test does.
    ledger = install(_Ledger(schema))
    client = _build(ledger)
    declared = client.transport

    client.close()

    assert isinstance(declared, _Logged)
    assert declared.name == "owner"
    assert declared.close_calls == 1


def test_a_supplied_list_is_inherited_and_a_second_sweep_closes_nothing(
    schema: GraphQLSchema, install: Any
) -> None:
    # The session fixture creates the list, registers its teardown, then
    # passes it in. The teardown sweep after the client's own close must
    # close nothing a second time.
    ledger = install(_Ledger(schema))
    cleanup: list[Any] = []

    client = _build(ledger, cleanup=cleanup)
    assert len(cleanup) == 3
    client.close()
    closed = ledger.closes()

    assert _close_all(cleanup) == []
    assert ledger.closes() == closed == ["close probe", "close owner", "close root"]


# -- a failure at each acquisition step ---------------------------------------

_ACQUIRED_BEFORE = {
    "root": [],
    "probe": ["close root"],
    "schema": ["close probe", "close root"],
    "owner": ["close probe", "close root"],
}


@pytest.mark.parametrize("step", list(_ACQUIRED_BEFORE))
def test_a_failed_step_closes_what_was_acquired_and_raises_the_failure(
    schema: GraphQLSchema, install: Any, step: str
) -> None:
    failure = Failure(step)
    ledger = install(_Ledger(schema, fail_at=step, failure=failure))

    with pytest.raises(Failure) as raised:
        _build(ledger)

    assert raised.value is failure
    assert reported_errors(raised.value) == (failure,)
    assert ledger.closes() == _ACQUIRED_BEFORE[step]


def test_a_failed_probe_close_still_closes_the_pool(
    schema: GraphQLSchema, install: Any
) -> None:
    # The probe is closed on the success path. Its wrapper is marked closed
    # before the close runs, so the unwinding sweep does not try it again.
    failure = Failure("probe close")
    ledger = install(_Ledger(schema, close_failures={"probe": failure}))

    with pytest.raises(Failure) as raised:
        _build(ledger)

    assert raised.value is failure
    assert ledger.closes() == ["close probe", "close root"]
    assert "acquire owner" not in ledger.events


def test_a_failed_constructor_closes_all_three_resources(
    schema: GraphQLSchema, install: Any
) -> None:
    failure = Failure("constructor")
    ledger = install(_Ledger(schema))

    with pytest.raises(Failure) as raised:
        _build(ledger, middleware=_RefusedMiddleware(failure))

    assert raised.value is failure
    assert ledger.closes() == ["close probe", "close owner", "close root"]


def test_a_failed_configuration_acquires_nothing(
    schema: GraphQLSchema, install: Any
) -> None:
    ledger = install(_Ledger(schema))

    with pytest.raises(ArgumentError):
        _build(ledger, no_such_option=1)

    assert ledger.events == []


def test_an_injected_transport_is_left_open_when_the_schema_fails(
    schema: GraphQLSchema,
) -> None:
    # The caller owns what the caller supplied, on the failure path too.
    failure = Failure("schema")
    ledger = _Ledger(schema, fail_at="schema", failure=failure)
    injected = FakeGraphQLTransport(schema)

    with pytest.raises(Failure) as raised:
        build_client(url=URL, transport=injected, schema_source=_Source(ledger))

    assert raised.value is failure
    assert injected.close_calls == 0


class _RefusingIntrospection(FakeGraphQLTransport):
    """An injected transport whose introspection request fails."""

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002 -- the protocol's own shape
        self.sent.append(request)
        raise Failure("introspection")


class _InvalidSchema(FakeGraphQLTransport):
    """An injected transport that answers introspection with no schema."""

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002 -- the protocol's own shape
        self.sent.append(request)
        return RawResponse(
            status_code=200,
            media_type="application/json",
            data={"__schema": "not a schema"},
            errors=(),
            extensions=None,
            headers={},
        )


_INJECTED_FAILURES: dict[str, tuple[type[FakeGraphQLTransport], type[Exception]]] = {
    "introspection": (_RefusingIntrospection, Failure),
    "invalid-schema": (_InvalidSchema, SchemaError),
    "constructor": (FakeGraphQLTransport, Failure),
}


@pytest.mark.parametrize("step", list(_INJECTED_FAILURES))
def test_an_injected_transport_is_left_open_on_every_construction_failure(
    schema: GraphQLSchema, step: str
) -> None:
    # The same failures that close every owned resource close nothing here,
    # because the factory owns nothing on this path. Each one fails after the
    # introspection request reached the injected transport.
    kind, expected = _INJECTED_FAILURES[step]
    injected = kind(schema)
    options: dict[str, Any] = {}
    if step == "constructor":
        options["middleware"] = _RefusedMiddleware(Failure("constructor"))

    with pytest.raises(expected):
        build_client(url=URL, transport=injected, **options)

    assert len(injected.sent) == 1
    assert injected.close_calls == 0


# -- cleanup failures while the factory unwinds -------------------------------


def test_the_construction_failure_wins_over_ordinary_cleanup_failures(
    schema: GraphQLSchema, install: Any
) -> None:
    failure = Failure("schema")
    probe_close, root_close = Failure("probe close"), Failure("root close")
    ledger = install(
        _Ledger(
            schema,
            fail_at="schema",
            failure=failure,
            close_failures={"probe": probe_close, "root": root_close},
        )
    )

    with pytest.raises(Failure) as raised:
        _build(ledger)

    assert raised.value is failure
    report = reported_errors(raised.value)
    assert report[0] is failure
    assert {id(exc) for exc in report} == {id(failure), id(probe_close), id(root_close)}
    # Every item is attempted, whatever the earlier ones did.
    assert ledger.closes() == ["close probe", "close root"]


def test_a_failed_constructor_wins_over_a_failed_transport_close(
    schema: GraphQLSchema, install: Any
) -> None:
    # The constructor is the one step that can fail once the client's own
    # transport exists. That transport's failed close must not stop the
    # sweep before the root pool, or replace the constructor's failure.
    failure, owner_close = Failure("constructor"), Failure("owner close")
    ledger = install(_Ledger(schema, close_failures={"owner": owner_close}))

    with pytest.raises(Failure) as raised:
        _build(ledger, middleware=_RefusedMiddleware(failure))

    assert raised.value is failure
    report = reported_errors(raised.value)
    assert report[0] is failure
    assert {id(exc) for exc in report} == {id(failure), id(owner_close)}
    assert ledger.closes() == ["close probe", "close owner", "close root"]


def test_an_interrupt_in_cleanup_wins_over_the_construction_failure(
    schema: GraphQLSchema, install: Any
) -> None:
    failure = Failure("schema")
    stop = Interrupt("root close")
    ledger = install(
        _Ledger(
            schema, fail_at="schema", failure=failure, close_failures={"root": stop}
        )
    )

    with pytest.raises(Interrupt) as raised:
        _build(ledger)

    # Raised as itself, so an `except Exception` around the call cannot
    # swallow it, with the construction failure still reachable.
    assert raised.value is stop
    assert not isinstance(raised.value, Exception)
    assert any(exc is failure for exc in reported_errors(raised.value))


def test_an_interrupted_construction_wins_over_an_ordinary_cleanup_failure(
    schema: GraphQLSchema, install: Any
) -> None:
    stop = Interrupt("schema")
    root_close = Failure("root close")
    ledger = install(
        _Ledger(
            schema, fail_at="schema", failure=stop, close_failures={"root": root_close}
        )
    )

    with pytest.raises(Interrupt) as raised:
        _build(ledger)

    assert raised.value is stop
    assert any(exc is root_close for exc in reported_errors(raised.value))
    assert ledger.closes() == ["close probe", "close root"]


def test_an_unwound_supplied_list_closes_nothing_on_its_second_sweep(
    schema: GraphQLSchema, install: Any
) -> None:
    failure = Failure("owner")
    ledger = install(_Ledger(schema, fail_at="owner", failure=failure))
    cleanup: list[Any] = []

    with pytest.raises(Failure):
        _build(ledger, cleanup=cleanup)
    closed = ledger.closes()

    assert len(cleanup) == 3  # the owner was adopted before it failed
    assert _close_all(cleanup) == []
    assert ledger.closes() == closed == ["close probe", "close root"]


# -- cleanup failures when the returned client closes -------------------------


def test_the_client_raises_the_failure_of_its_earliest_resource(
    schema: GraphQLSchema, install: Any
) -> None:
    owner_close, root_close = Failure("owner close"), Failure("root close")
    ledger = install(
        _Ledger(schema, close_failures={"owner": owner_close, "root": root_close})
    )
    client = _build(ledger)

    with pytest.raises(Failure) as raised:
        client.close()

    assert raised.value is root_close
    assert any(exc is owner_close for exc in reported_errors(raised.value))
    assert ledger.closes() == ["close probe", "close owner", "close root"]


def test_an_interrupt_in_the_client_close_wins_and_close_stays_idempotent(
    schema: GraphQLSchema, install: Any
) -> None:
    stop, root_close = Interrupt("owner close"), Failure("root close")
    ledger = install(
        _Ledger(schema, close_failures={"owner": stop, "root": root_close})
    )
    client = _build(ledger)

    with pytest.raises(Interrupt) as raised:
        client.close()
    client.close()

    assert raised.value is stop
    assert any(exc is root_close for exc in reported_errors(raised.value))
    assert ledger.closes() == ["close probe", "close owner", "close root"]


# -- the real transport over the same paths -----------------------------------


class _PoolCloses:
    """Counts closes of the one real root pool the factory built."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.roots: list[HttpxTransport] = []
        self.calls: dict[int, int] = {}
        build_root = factory._new_root_pool

        def recording_root(config: ClientConfig) -> HttpxTransport:
            root = build_root(config)
            self.roots.append(root)
            return root

        real_close = httpx.HTTPTransport.close

        def counting_close(transport: httpx.HTTPTransport) -> None:
            self.calls[id(transport)] = self.calls.get(id(transport), 0) + 1
            real_close(transport)

        monkeypatch.setattr(factory, "_new_root_pool", recording_root)
        monkeypatch.setattr(httpx.HTTPTransport, "close", counting_close)

    def of_root(self) -> int:
        (root,) = self.roots
        pool = root._pool
        assert isinstance(pool, httpx.HTTPTransport)
        return self.calls.get(id(pool), 0)


def test_the_real_root_pool_is_closed_once_by_the_returned_client(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _PoolCloses(monkeypatch)
    client = build_client(url=URL, schema=schema)
    assert pool.of_root() == 0

    client.close()
    client.close()

    assert pool.of_root() == 1
    assert isinstance(client.transport, HttpxTransport)
    assert client.transport._client.is_closed


def test_the_real_root_pool_is_closed_once_when_the_schema_fails(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _PoolCloses(monkeypatch)
    failure = Failure("schema")
    ledger = _Ledger(schema, fail_at="schema", failure=failure)

    with pytest.raises(Failure) as raised:
        build_client(url=URL, schema_source=_Source(ledger))

    assert raised.value is failure
    assert pool.of_root() == 1


# -- pools a failing transport constructor built -------------------------------

_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


class _Pools:
    """Every real ``httpx.HTTPTransport`` built, in order, and its closes.

    ``fail_build_at`` is the one-based build that raises ``failure`` before
    the pool exists. ``close_failures`` maps a zero-based build index to what
    that pool's ``close()`` raises after it has closed.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_build_at: int | None = None,
        failure: BaseException | None = None,
        close_failures: Mapping[int, BaseException] | None = None,
    ) -> None:
        self.built: list[httpx.HTTPTransport] = []
        self.calls: dict[int, int] = {}
        failures = dict(close_failures or {})
        real_init = httpx.HTTPTransport.__init__
        real_close = httpx.HTTPTransport.close

        def counting_init(
            transport: httpx.HTTPTransport, *args: Any, **kwargs: Any
        ) -> None:
            if failure is not None and len(self.built) + 1 == fail_build_at:
                raise failure
            real_init(transport, *args, **kwargs)
            self.built.append(transport)

        def counting_close(transport: httpx.HTTPTransport) -> None:
            self.calls[id(transport)] = self.calls.get(id(transport), 0) + 1
            real_close(transport)
            index = next(i for i, pool in enumerate(self.built) if pool is transport)
            if index in failures:
                raise failures[index]

        monkeypatch.setattr(httpx.HTTPTransport, "__init__", counting_init)
        monkeypatch.setattr(httpx.HTTPTransport, "close", counting_close)

    def closes(self) -> list[int]:
        return [self.calls.get(id(pool), 0) for pool in self.built]


def _refusing(failure: BaseException) -> Callable[..., httpx.Client]:
    """An ``httpx.Client`` stand-in whose construction raises ``failure``."""

    def refuse(*_args: Any, **_kwargs: Any) -> httpx.Client:
        raise failure

    return refuse


@pytest.fixture
def two_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient proxies for http and https, so the root holds three pools."""
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy-a.example.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-b.example.test:8080")


def test_a_failing_http_client_closes_the_pool_the_root_built(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pools = _Pools(monkeypatch)
    failure = Failure("httpx.Client")

    monkeypatch.setattr(httpx, "Client", _refusing(failure))
    with pytest.raises(Failure) as raised:
        build_client(url=URL, schema=schema)

    assert raised.value is failure
    assert reported_errors(raised.value) == (failure,)
    assert len(pools.built) == 1
    assert pools.closes() == [1]


@pytest.mark.usefixtures("two_proxies")
@pytest.mark.parametrize("fail_build_at", [1, 2, 3])
def test_a_failing_proxy_pool_closes_every_pool_built_before_it(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch, fail_build_at: int
) -> None:
    failure = Failure(f"pool {fail_build_at}")
    pools = _Pools(monkeypatch, fail_build_at=fail_build_at, failure=failure)

    with pytest.raises(Failure) as raised:
        build_client(url=URL, schema=schema, trust_env=True)

    assert raised.value is failure
    assert reported_errors(raised.value) == (failure,)
    assert pools.closes() == [1] * (fail_build_at - 1)


@pytest.mark.usefixtures("two_proxies")
def test_the_construction_failure_wins_over_a_pool_close_failure(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure, direct_close = Failure("pool 3"), Failure("direct close")
    pools = _Pools(
        monkeypatch, fail_build_at=3, failure=failure, close_failures={0: direct_close}
    )

    with pytest.raises(Failure) as raised:
        build_client(url=URL, schema=schema, trust_env=True)

    assert raised.value is failure
    assert set(map(id, reported_errors(raised.value))) == {
        id(failure),
        id(direct_close),
    }
    assert pools.closes() == [1, 1]


@pytest.mark.usefixtures("two_proxies")
def test_an_interrupted_pool_close_wins_over_the_construction_failure(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure, stop = Failure("pool 3"), Interrupt("mount close")
    pools = _Pools(
        monkeypatch, fail_build_at=3, failure=failure, close_failures={1: stop}
    )

    with pytest.raises(Interrupt) as raised:
        build_client(url=URL, schema=schema, trust_env=True)

    assert raised.value is stop
    assert any(exc is failure for exc in reported_errors(raised.value))
    assert pools.closes() == [1, 1]


@pytest.mark.usefixtures("two_proxies")
def test_closing_the_client_closes_every_proxy_pool_when_each_close_fails(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures = {index: Failure(f"pool {index} close") for index in range(3)}
    pools = _Pools(monkeypatch, close_failures=failures)
    client = build_client(url=URL, schema=schema, trust_env=True)
    assert pools.closes() == [0, 0, 0]

    with pytest.raises(Failure) as raised:
        client.close()
    client.close()

    # The direct pool was built first, so its failure wins, and the report
    # the router raised stays whole inside the client's own report.
    assert raised.value is failures[0]
    assert set(map(id, reported_errors(raised.value))) == set(
        map(id, failures.values())
    )
    assert pools.closes() == [1, 1, 1]


def test_a_derived_transport_never_closes_the_pool_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools = _Pools(monkeypatch)
    root = HttpxTransport()
    failure = Failure("httpx.Client")

    with monkeypatch.context() as patched:
        patched.setattr(httpx, "Client", _refusing(failure))
        for own_pool in (False, True):
            with pytest.raises(Failure) as raised:
                root.derive(own_pool=own_pool)
            assert raised.value is failure
    assert pools.closes() == [0]

    root.close()
    assert pools.closes() == [1]
