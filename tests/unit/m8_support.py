"""Shared helpers for the error-assertion and polling tests (M8).

A scripted transport plays one planned step per call, so a test states the
exact sequence of responses and failures it needs, and a fake clock lets a
poll run through minutes of simulated time without sleeping in real time.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from graphql import GraphQLSchema
from graphql import build_schema as build_graphql_schema

from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.middleware import Middleware
from pytest_graphql._core.transport.base import RawResponse

SDL = """
type User { id: ID!, name: String, status: String }
type Query {
  user(id: ID!): User
  order(id: ID!): User
  createReport(id: ID!): String
  reset: String
}
type Mutation {
  updateUser(id: ID!, name: String): User
  reset: String
}
"""

URL = "https://example.test/graphql"


def build_test_schema() -> GraphQLSchema:
    return build_graphql_schema(SDL)


def envelope(
    data: Any, errors: tuple[dict[str, Any], ...] = (), *, status: int = 200
) -> RawResponse:
    return RawResponse(
        status_code=status,
        media_type="application/graphql-response+json",
        data=data,
        errors=errors,
        extensions=None,
        headers={},
    )


def user(status: str = "NEW") -> RawResponse:
    return envelope({"user": {"id": "u1", "name": "Ann", "status": status}})


def failure(
    message: str = "boom",
    *,
    code: str | None = None,
    path: list[str | int] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"message": message}
    if code is not None:
        item["extensions"] = {"code": code}
    if path is not None:
        item["path"] = path
    return item


def rejected(*errors: dict[str, Any]) -> RawResponse:
    """An execution error: errors and no data."""
    return envelope(None, errors or (failure(),))


class ScriptedTransport:
    """Plays one step per call. The last step repeats once the list is spent.

    A step is a ``RawResponse`` to return or an exception to raise.
    ``on_send`` runs before each step, so a test can advance a fake clock by
    what the call "cost".
    """

    def __init__(
        self,
        *steps: RawResponse | BaseException,
        on_send: Callable[[RequestInfo], None] | None = None,
    ) -> None:
        self._steps = steps or (user(),)
        self._on_send = on_send
        self.sent: list[RequestInfo] = []

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002
        index = min(len(self.sent), len(self._steps) - 1)
        self.sent.append(request)
        if self._on_send is not None:
            self._on_send(request)
        step = self._steps[index]
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self) -> None:
        return None


def make_client(
    schema: GraphQLSchema,
    *steps: RawResponse | BaseException,
    on_send: Callable[[RequestInfo], None] | None = None,
    middleware: Sequence[Middleware] = (),
    **config: Any,
) -> tuple[GraphQLClient, ScriptedTransport]:
    transport = ScriptedTransport(*steps, on_send=on_send)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url=URL, **config),
        middleware=middleware,
    )
    return client, transport


class FakeClock:
    """A monotonic clock and a sleep that only moves the clock.

    The polling module reads time through two private names, so a test
    installs this in their place and nothing sleeps in real time.
    """

    #: A poll that never ends would hang the suite. Past this many reads the
    #: clock fails the test instead, so a broken loop is a failure, not a hang.
    MAX_READS = 10_000

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        if self.reads > self.MAX_READS:
            raise AssertionError("the poll read the clock endlessly")
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds
