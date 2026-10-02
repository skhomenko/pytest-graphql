"""A transport derived for a new client is never stranded (9.1 to 9.3).

``_client_over`` is the one place a clone or a per-test client derives its
transport. A derived transport is adopted before it is derived, so a failure
while the client is built closes it once, and the failure the caller receives
is the construction failure unless an interrupt outranks it. Both failure
kinds are driven from both sides: the construction step and the derived
transport's own ``close()``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import GraphQLClient, _client_over
from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.lifecycle import reported_errors
from pytest_graphql._core.transport.base import (
    DerivableTransportBase,
    RawResponse,
    Transport,
)
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema


class _Derived:
    """A derived transport that counts its closes and can fail one."""

    def __init__(self, close_error: BaseException | None) -> None:
        self.closes = 0
        self._close_error = close_error

    def send(self, _request: RequestInfo, *, timeout: float) -> RawResponse:
        del timeout
        raise AssertionError("never sent")

    def close(self) -> None:
        self.closes += 1
        if self._close_error is not None:
            raise self._close_error


class _Root(DerivableTransportBase):
    def __init__(self, close_error: BaseException | None = None) -> None:
        self.derived: list[_Derived] = []
        self._close_error = close_error

    def send(self, _request: RequestInfo, *, timeout: float) -> RawResponse:
        del timeout
        raise AssertionError("never sent")

    def close(self) -> None:
        raise AssertionError("the root is never closed here")

    def derive(self) -> _Derived:
        transport = _Derived(self._close_error)
        self.derived.append(transport)
        return transport


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


def _failing(error: BaseException) -> Callable[[Transport, bool], GraphQLClient]:
    def build(_transport: Transport, _owns: bool) -> GraphQLClient:
        raise error

    return build


@pytest.mark.parametrize(
    "error", [RuntimeError("construction failed"), KeyboardInterrupt()]
)
def test_a_failed_build_closes_the_derived_transport_once(
    error: BaseException,
) -> None:
    root = _Root()
    with pytest.raises(type(error)) as raised:
        _client_over(root, _failing(error))
    assert raised.value is error
    (derived,) = root.derived
    assert derived.closes == 1


@pytest.mark.parametrize(
    ("build_error", "close_error", "winner"),
    [
        (RuntimeError("construction failed"), OSError("close failed"), "build"),
        (KeyboardInterrupt(), OSError("close failed"), "build"),
        (RuntimeError("construction failed"), KeyboardInterrupt(), "close"),
    ],
)
def test_a_failing_close_is_reported_and_the_right_failure_wins(
    build_error: BaseException, close_error: BaseException, winner: str
) -> None:
    root = _Root(close_error)
    with pytest.raises(BaseException) as raised:
        _client_over(root, _failing(build_error))
    (derived,) = root.derived
    assert derived.closes == 1
    expected, other = (
        (build_error, close_error) if winner == "build" else (close_error, build_error)
    )
    assert raised.value is expected
    reachable = {id(error) for error in reported_errors(raised.value)}
    assert id(other) in reachable


def test_a_built_client_owns_its_derived_transport(schema: GraphQLSchema) -> None:
    root = _Root()
    client = _client_over(
        root,
        lambda transport, owns: GraphQLClient(
            transport=transport, schema=schema, owns_transport=owns
        ),
    )
    (derived,) = root.derived
    assert client.transport is derived
    assert client.owns_transport is True
    assert derived.closes == 0
    client.close()
    assert derived.closes == 1


def test_a_plain_transport_is_shared_and_never_closed(schema: GraphQLSchema) -> None:
    plain = FakeGraphQLTransport(schema)
    with pytest.raises(RuntimeError):
        _client_over(plain, _failing(RuntimeError("boom")))
    client = _client_over(
        plain,
        lambda transport, owns: GraphQLClient(
            transport=transport, schema=schema, owns_transport=owns
        ),
    )
    assert client.transport is plain
    assert client.owns_transport is False
    client.close()
    assert plain.close_calls == 0


def test_a_clone_whose_construction_fails_closes_its_derived_transport(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _Root()
    parent = GraphQLClient(transport=root, schema=schema)
    real_init = GraphQLClient.__init__

    def init(self: GraphQLClient, **kwargs: object) -> None:
        if kwargs.get("owns_transport"):
            raise RuntimeError("construction failed")
        real_init(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(GraphQLClient, "__init__", init)
    with pytest.raises(RuntimeError, match="construction failed"):
        parent.anonymous()
    (derived,) = root.derived
    assert derived.closes == 1
