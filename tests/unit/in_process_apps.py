"""A minimal ASGI and a minimal WSGI GraphQL application, for the in-process tests.

Both are built from ``graphql-core`` and the standard library only, so the
suite keeps no framework dependency (DESIGN_DECISIONS.md section 13.3). They
serve one small schema:

- ``hello`` answers a constant.
- ``slow(seconds)`` answers after a delay. Over ASGI it awaits, so a timeout
  can cancel it. ``block(seconds)`` sleeps without awaiting, so it holds the
  event loop.
- ``login(name)`` sets a ``session`` cookie and ``whoami`` reads it back.
- ``greeting`` reads the lifespan ``state`` of the request scope.
- ``upload(file)`` answers the filename, the size and the type of a file sent
  as a GraphQL multipart request.

A request to ``/crash`` raises :class:`AppCrashError` from the application itself,
outside GraphQL execution, and a request to ``/exit`` raises ``SystemExit``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from http.cookies import SimpleCookie
from typing import Any, Literal

from graphql import build_schema, graphql, graphql_sync

from tests.schema.multipart import graphql_payload

SDL = """
scalar Upload

type Query {
  hello: String!
  slow(seconds: Float!): String!
  block(seconds: Float!): String!
  whoami: String
  greeting: String
}

type Mutation {
  login(name: String!): String!
  upload(file: Upload!): String!
}
"""

MEDIA_TYPE = "application/graphql-response+json"


class AppCrashError(Exception):
    """Raised by an application itself, not by a resolver."""


def _context(cookie_header: str, state: MutableMapping[str, Any]) -> dict[str, Any]:
    cookies = SimpleCookie()
    cookies.load(cookie_header)
    return {"cookies": cookies, "set_cookie": [], "state": state}


def _whoami(_root: object, info: Any) -> str | None:
    morsel = info.context["cookies"].get("session")
    return None if morsel is None else str(morsel.value)


def _login(_root: object, info: Any, name: str) -> str:
    info.context["set_cookie"].append(f"session={name}; Path=/")
    return name


def _greeting(_root: object, info: Any) -> str | None:
    value = info.context["state"].get("greeting")
    return None if value is None else str(value)


def _upload(_root: object, _info: Any, file: Any) -> str:
    return f"{file.filename}:{len(file.content)}:{file.content_type}"


def _schema(*, asynchronous: bool) -> Any:
    schema = build_schema(SDL)
    query = schema.query_type
    mutation = schema.mutation_type
    assert query is not None and mutation is not None

    def block(_root: object, _info: Any, seconds: float) -> str:
        time.sleep(seconds)
        return "blocked"

    if asynchronous:

        async def slow(_root: object, _info: Any, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "slow"

        query.fields["slow"].resolve = slow
    else:

        def slow_sync(_root: object, _info: Any, seconds: float) -> str:
            time.sleep(seconds)
            return "slow"

        query.fields["slow"].resolve = slow_sync
    query.fields["hello"].resolve = lambda _root, _info: "world"
    query.fields["block"].resolve = block
    query.fields["whoami"].resolve = _whoami
    query.fields["greeting"].resolve = _greeting
    mutation.fields["login"].resolve = _login
    mutation.fields["upload"].resolve = _upload
    return schema


def _body(result: Any) -> bytes:
    envelope: dict[str, Any] = {"data": result.data}
    if result.errors:
        envelope["errors"] = [error.formatted for error in result.errors]
    return json.dumps(envelope).encode()


def _headers(context: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("Content-Type", MEDIA_TYPE),
        *(("Set-Cookie", value) for value in context["set_cookie"]),
    ]


# -- ASGI ---------------------------------------------------------------------

LifespanMode = Literal[
    "complete", "unsupported", "returns", "failed", "hangs", "shutdown_failed", "exits"
]

Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class AsgiApp:
    """The ASGI application, an object whose ``__call__`` is a coroutine.

    ``lifespan`` chooses how it answers the lifespan scope, and ``events``
    records every lifespan event it received.
    """

    def __init__(self, *, lifespan: LifespanMode = "complete") -> None:
        self.lifespan = lifespan
        self.events: list[str] = []
        self.schema = _schema(asynchronous=True)

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        assert scope["type"] == "http"
        if scope["path"] == "/crash":
            raise AppCrashError("the application crashed")
        if scope["path"] == "/exit":
            raise SystemExit("the application exited")
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        cookie = ""
        content_type = ""
        for name, value in scope["headers"]:
            if name == b"cookie":
                cookie = value.decode("latin-1")
            if name == b"content-type":
                content_type = value.decode("latin-1")
        context = _context(cookie, scope.get("state", {}))
        payload = graphql_payload(body, content_type)
        result = await graphql(
            self.schema,
            payload["query"],
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
            context_value=context,
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in _headers(context)
                ],
            }
        )
        await send({"type": "http.response.body", "body": _body(result)})

    async def _lifespan(
        self, scope: MutableMapping[str, Any], receive: Receive, send: Send
    ) -> None:
        if self.lifespan == "unsupported":
            raise AppCrashError("lifespan is not supported")
        if self.lifespan == "returns":
            return
        if self.lifespan == "exits":
            raise SystemExit("the lifespan exited")
        message = await receive()
        self.events.append(message["type"])
        if self.lifespan == "hangs":
            await asyncio.Event().wait()
        if self.lifespan == "failed":
            await send({"type": "lifespan.startup.failed", "message": "no database"})
            return
        scope["state"]["greeting"] = "hello from startup"
        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        self.events.append(message["type"])
        if self.lifespan == "shutdown_failed":
            await send({"type": "lifespan.shutdown.failed", "message": "flush failed"})
            return
        await send({"type": "lifespan.shutdown.complete"})


_DEFAULT_ASGI = AsgiApp()


async def asgi_function(
    scope: MutableMapping[str, Any], receive: Receive, send: Send
) -> None:
    """The same application as a plain coroutine function."""
    await _DEFAULT_ASGI(scope, receive, send)


# -- WSGI ---------------------------------------------------------------------

_WSGI_SCHEMA = _schema(asynchronous=False)


def wsgi_app(
    environ: dict[str, Any], start_response: Callable[..., Any]
) -> Iterable[bytes]:
    """The WSGI application, a plain function."""
    if environ["PATH_INFO"] == "/crash":
        raise AppCrashError("the application crashed")
    if environ["PATH_INFO"] == "/exit":
        raise SystemExit("the application exited")
    length = int(environ.get("CONTENT_LENGTH") or 0)
    payload = graphql_payload(
        environ["wsgi.input"].read(length), environ.get("CONTENT_TYPE", "")
    )
    context = _context(environ.get("HTTP_COOKIE", ""), {})
    result = graphql_sync(
        _WSGI_SCHEMA,
        payload["query"],
        variable_values=payload.get("variables"),
        operation_name=payload.get("operationName"),
        context_value=context,
    )
    start_response("200 OK", _headers(context))
    return [_body(result)]
