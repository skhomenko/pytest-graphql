"""The C17 two-client cookie test, against a real local HTTP server (8).

Only the connection pool is shared. Every logical client has its own cookie
jar, so a ``Set-Cookie`` on one client's response never reaches another
client, and a clone starts with an empty jar. Under the default
``cookie_scope="none"`` no cookie survives a call at all. Under
``cookie_scope="client"`` the client that received the cookie keeps it, and
no other client sends it.

Two shapes give the second client. A clone under another identity is the
standalone path. A sibling derived from the same root transport is the shape
the session fixture builds. In both, the second client is created only after
the first has received the cookie, so a clone that copied the first client's
jar would send it. The server records the peer address of every request, so
the tests see on the wire that both clients reused one connection, and a
control shows that two separately built clients do not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.auth import BearerAuth
from pytest_graphql._core.client import ClientConfig, GraphQLClient, build_client
from pytest_graphql._core.errors import GraphQLTransportError
from pytest_graphql._core.transport.httpx_transport import CookieScope, HttpxTransport
from tests.schema.resolvers import build_schema
from tests.unit.local_http_server import PlannedResponse, local_server

_COOKIE = "session=c17-cookie-value-0123"
_FIRST, _SECOND = "first-token-0123", "second-token-0123"
_JSON = (("Content-Type", "application/graphql-response+json"),)
_USER = b'{"data": {"user": {"id": "u1"}}}'

#: The first client, and a way to build the second one later.
Pair = tuple[GraphQLClient, Callable[[], GraphQLClient]]
Shape = Callable[[str, CookieScope, GraphQLSchema], AbstractContextManager[Pair]]


@pytest.fixture
def schema() -> GraphQLSchema:
    return build_schema()


class _Server:
    """What the server saw: the token and the cookie of every request."""

    def __init__(self, first: PlannedResponse) -> None:
        self.seen: list[tuple[str | None, str | None]] = []
        self.peers: list[tuple[str, int]] = []
        self._first = first

    def respond(self, _body: bytes, headers: dict[str, str]) -> PlannedResponse:
        """Set the cookie on the first response only, then answer plainly."""
        authorization = headers.get("authorization")
        token = authorization.removeprefix("Bearer ") if authorization else None
        self.seen.append((token, headers.get("cookie")))
        if len(self.seen) == 1:
            return self._first
        return PlannedResponse(200, _JSON, _USER)


def _setting_cookie(status: int = 200, body: bytes = _USER) -> PlannedResponse:
    return PlannedResponse(status, (*_JSON, ("Set-Cookie", f"{_COOKIE}; Path=/")), body)


@contextmanager
def _clone(url: str, scope: CookieScope, schema: GraphQLSchema) -> Iterator[Pair]:
    """The standalone path: the second client is a clone under another token."""
    with ExitStack() as stack:
        first = build_client(
            url=url, schema=schema, auth=BearerAuth(_FIRST), cookie_scope=scope
        )
        stack.callback(first.close)

        def second() -> GraphQLClient:
            clone = first.as_(_SECOND)
            stack.callback(clone.close)
            return clone

        yield first, second


@contextmanager
def _sibling(url: str, scope: CookieScope, schema: GraphQLSchema) -> Iterator[Pair]:
    """The fixture's shape: two clients, each derived from one root transport."""
    with ExitStack() as stack:
        root = HttpxTransport(cookie_scope=scope)
        stack.callback(root.close)

        def client(token: str) -> GraphQLClient:
            built = GraphQLClient(
                transport=root.derive(),
                schema=schema,
                config=ClientConfig(url=url),
                auth=BearerAuth(token),
                owns_transport=True,
            )
            stack.callback(built.close)
            return built

        yield client(_FIRST), lambda: client(_SECOND)


_SHAPES: dict[str, Shape] = {"clone": _clone, "sibling": _sibling}


def _call(client: GraphQLClient) -> None:
    client.query("user", id="u1", fields=["id"])


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize(
    ("scope", "first_again"),
    [
        pytest.param("none", None, id="none"),
        pytest.param("client", _COOKIE, id="client"),
    ],
)
def test_a_cookie_reaches_only_the_client_it_was_set_on(
    schema: GraphQLSchema, shape: str, scope: CookieScope, first_again: str | None
) -> None:
    server = _Server(_setting_cookie())
    with (
        local_server(server.respond, peers=server.peers) as url,
        _SHAPES[shape](url, scope, schema) as (first, make_second),
    ):
        _call(first)
        second = make_second()
        _call(second)
        _call(first)
        _call(second)

    assert server.seen == [
        (_FIRST, None),
        (_SECOND, None),
        (_FIRST, first_again),
        (_SECOND, None),
    ]
    # Every request reused the one connection the first client opened, so
    # both clients drew it from the same pool.
    assert len(server.peers) == 4
    assert len(set(server.peers)) == 1


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(_setting_cookie(500, b"server error"), id="http-500"),
        pytest.param(_setting_cookie(200, b"{not json"), id="unparsable-body"),
    ],
)
def test_a_cookie_on_a_failed_response_does_not_survive_the_call(
    schema: GraphQLSchema, shape: str, failing: PlannedResponse
) -> None:
    server = _Server(failing)
    with (
        local_server(server.respond) as url,
        _SHAPES[shape](url, "none", schema) as (first, make_second),
    ):
        with pytest.raises(GraphQLTransportError):
            _call(first)
        _call(first)
        _call(make_second())

    assert server.seen == [(_FIRST, None), (_FIRST, None), (_SECOND, None)]


def test_separately_built_clients_do_not_share_a_connection(
    schema: GraphQLSchema,
) -> None:
    # The control for the peer check above: without a shared pool, the
    # second client opens a connection of its own.
    server = _Server(PlannedResponse(200, _JSON, _USER))
    with (
        local_server(server.respond, peers=server.peers) as url,
        build_client(url=url, schema=schema) as first,
        build_client(url=url, schema=schema) as second,
    ):
        _call(first)
        _call(second)

    assert len(set(server.peers)) == 2


_VALUE = _COOKIE.partition("=")[2]


def _echo(where: str) -> PlannedResponse:
    """A response that quotes the cookie value back, in a body or an error.

    An error's ``message`` is raw server data, as ``data`` is, so the echo
    that matters there is in its ``path``, which the response renders.
    """
    if where == "body":
        return PlannedResponse(500, _JSON, f"bad session {_VALUE}".encode())
    error = f'{{"message": "bad session {_VALUE}", "path": ["user", "{_VALUE}"]}}'
    return PlannedResponse(
        200, _JSON, f'{{"data": null, "errors": [{error}]}}'.encode()
    )


def _with_cookie(planned: PlannedResponse) -> PlannedResponse:
    cookie = ("Set-Cookie", f"{_COOKIE}; Path=/; HttpOnly")
    return planned._replace(headers=(*planned.headers, cookie))


def _echo_script(
    side: str, echo: PlannedResponse
) -> tuple[list[PlannedResponse], CookieScope]:
    """The responses for one side, ending with ``echo``, and the scope it needs.

    The jar sends a cookie outside ``request.headers``, and a server sets one
    on a response, so neither is a header the client's request could name.
    """
    if side == "sent":
        return [_setting_cookie(), echo], "client"
    return [_with_cookie(echo)], "none"


_SIDES = ["sent", "received"]


@pytest.mark.parametrize("side", _SIDES)
def test_a_cookie_value_echoed_in_a_body_reaches_no_rendered_path(
    schema: GraphQLSchema, side: str
) -> None:
    # C17 and C16: every cookie value joins the secret set.
    script, scope = _echo_script(side, _echo("body"))
    replies = iter(script)
    with (
        local_server(lambda _body, _headers: next(replies)) as url,
        build_client(url=url, schema=schema, cookie_scope=scope) as client,
    ):
        for _ in script[:-1]:
            _call(client)
        with pytest.raises(GraphQLTransportError) as caught:
            _call(client)
        rendered = " ".join(
            (
                str(caught.value),
                repr(caught.value),
                caught.value.body_excerpt,
                repr(caught.value.request),
                client.recorder.dump(),
            )
        )

    assert _VALUE not in rendered
    assert "bad session" in rendered


@pytest.mark.parametrize("side", _SIDES)
def test_a_cookie_value_echoed_in_a_graphql_error_reaches_no_rendered_path(
    schema: GraphQLSchema, side: str
) -> None:
    script, scope = _echo_script(side, _echo("graphql-error"))
    replies = iter(script)
    with (
        local_server(lambda _body, _headers: next(replies)) as url,
        build_client(url=url, schema=schema, cookie_scope=scope) as client,
    ):
        for _ in script[:-1]:
            _call(client)
        response = client.execute(
            'query { user(id: "u1") { id } }', raise_on_error=False
        )
        rendered = " ".join(
            (repr(response), repr(response.errors), client.recorder.dump())
        )

    assert _VALUE in response.errors[0].message
    assert _VALUE not in rendered
    assert "[redacted:" in repr(response.errors)
