"""The ``Transport`` protocol, ``DerivableTransportBase``, and ``RawResponse``.

Per C59, ``Transport.send()`` (SPEC 5.6) takes the canonical ``RequestInfo``
the Diagnostics foundation milestone built; this module imports it and
defines nothing new under that name.

Derivation is an optional capability, opted into by inheriting one abstract
base (C23), never detected by a marker attribute or a structural check.
``isinstance`` against a real class is a type guard, so the caller that does
``isinstance(transport, DerivableTransportBase)`` gets a fully typed, bound
``derive`` method back, and a subclass with the wrong signature is a
``mypy --strict`` error where it is written, not at a distant call site. A
transport that does not inherit the base is shared, whatever methods it has:
an unrelated member named ``derive`` is never looked up, and the plain
``Transport`` protocol below -- ``send()`` and ``close()`` -- is unchanged by
this. The library never registers a virtual subclass, so opting in is by
inheritance only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pytest_graphql._core.diagnostics import RequestInfo


@dataclass(frozen=True)
class RawResponse:
    """What a transport returns: one GraphQL answer, as the server sent it.

    A `Transport` builds one `RawResponse` for each request. The client turns it
    into a `GraphQLResponse` and decides whether the answer is an error. Return
    one for every answer that has a `data` entry, whatever the HTTP status, and
    also when the answer holds errors. For anything else, such as an unparsable
    body, raise an exception and do not return a `RawResponse`.

    Args:
        status_code: The HTTP status code of the answer.
        media_type: The media type of the answer, such as
            `application/graphql-response+json`.
        data: The `data` entry of the answer. It may be `None`.
        errors: The `errors` list of the answer, as received. Use an empty tuple
            when there is none.
        extensions: The `extensions` entry of the answer, or `None`.
        headers: The response headers.
        transport_credentials: Secrets that the transport sent or received
            outside the request headers, as `(name, value)` pairs. Most
            transports leave it empty.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(
            status_code=200,
            media_type="application/json",
            data={"user": {"name": "Ada Lovelace"}},
            errors=(),
            extensions=None,
            headers={},
        )
        assert raw.data["user"]["name"] == "Ada Lovelace"
        ```
    """

    status_code: int
    """The HTTP status code of the answer.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(503, "application/json", {"x": 1}, (), None, {})
        assert raw.status_code == 503
        ```
    """
    media_type: str
    """The media type of the answer.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(200, "application/json", {}, (), None, {})
        assert raw.media_type == "application/json"
        ```
    """
    data: Any
    """The `data` entry of the answer, which may be `None`.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(200, "application/json", None, (), None, {})
        assert raw.data is None
        ```
    """
    errors: tuple[Mapping[str, Any], ...]
    """The `errors` list of the answer, as received. An empty tuple means none.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        error = {"message": "no access"}
        raw = RawResponse(200, "application/json", None, (error,), None, {})
        assert raw.errors[0]["message"] == "no access"
        ```
    """
    extensions: Mapping[str, Any] | None
    """The `extensions` entry of the answer, or `None`.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(200, "application/json", {}, (), {"cost": 3}, {})
        assert raw.extensions == {"cost": 3}
        ```
    """
    headers: Mapping[str, str]
    """The response headers.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(200, "application/json", {}, (), None, {"X-Id": "1"})
        assert raw.headers["X-Id"] == "1"
        ```
    """
    #: C16, C17. Credentials the transport sent or received on this
    #: request's behalf outside its headers, a proxy's and every cookie
    #: value, as ``RequestInfo.transport_credentials`` pairs, so the
    #: client renders the response with the same secret set the transport
    #: scrubbed its own errors with. A server or proxy can echo one into an
    #: error ``path`` as easily as into a body. Never rendered.
    transport_credentials: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    """Secrets that the transport handled outside the request headers.

    A pair is `(name, value)`. The client hides each value in what it shows, so a
    server that repeats one in an error does not leak it. It is never shown.

    Examples:
        ```python {.exec}
        from pytest_graphql import RawResponse

        raw = RawResponse(200, "application/json", {}, (), None, {})
        assert raw.transport_credentials == ()
        ```
    """


class Transport(Protocol):
    """The part of a client that talks to the server.

    A client sends every request through a transport. The default is an HTTP
    transport built on `httpx`. Any object with `send` and `close` methods of
    this shape can take its place, for example one that calls an in-process
    schema in a unit test, or one that wraps another transport to add logging.

    A transport you give to `build_client()` or `GraphQLClient` is yours. The
    client does not close it unless you pass `owns_transport=True` to
    `GraphQLClient`.

    Examples:
        A transport that wraps another one and records each URL:

        ```python {.exec}
        from pytest_graphql import build_client


        class RecordingTransport:
            def __init__(self, inner):
                self.inner = inner
                self.urls = []

            def send(self, request, *, timeout):
                self.urls.append(request.url)
                return self.inner.send(request, timeout=timeout)

            def close(self):
                self.inner.close()


        recording = RecordingTransport(gql.transport)
        client = build_client(
            url="http://localhost:8000/graphql",
            transport=recording,
            schema=gql.schema,
        )
        client.query("users")
        assert recording.urls == ["http://localhost:8000/graphql"]
        ```
    """

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:
        """Send one request and return the server's answer.

        Args:
            request: The request to send. It carries real header and variable
                values, so a transport can send them. Never print it. Use
                `request.redacted()` when you need text.
            timeout: The longest this call may take, in seconds. It may be
                `math.inf`, which means no limit.

        Returns:
            The parsed response. It holds the HTTP status code, the media type,
            the `data` value, the `errors` list, the `extensions` and the
            response headers. A response that has no GraphQL result in it, such
            as an unparsable body, is not returned. Raise an exception instead.

        Raises:
            Exception: When the request cannot be sent or the reply is not a
                GraphQL response. The exception reaches the caller.

        Examples:
            ```python {.exec}
            from pytest_graphql import RequestInfo

            request = RequestInfo(
                operation="user",
                kind="query",
                document='query user { user(id: "u1") { name } }',
                variables={},
                headers={},
                url="http://localhost:8000/graphql",
            )
            raw = gql.transport.send(request, timeout=5.0)
            assert raw.status_code == 200
            assert raw.data == {"user": {"name": "Ada Lovelace"}}
            ```
        """
        ...

    def close(self) -> None:
        """Release what the transport holds, such as open connections.

        A client calls this once, when the client closes and owns the
        transport. A client built by `build_client()` without a `transport`
        owns the one it creates.

        Examples:
            ```python {.exec}
            from pytest_graphql import build_client

            client = build_client(
                url="http://localhost:8000/graphql",
                schema=gql.schema,
            )
            assert client.owns_transport
            client.close()
            ```
        """
        ...


class DerivableTransportBase(ABC):
    """The base class for a transport that keeps state for each client.

    A client that you clone with `as_()`, `with_headers()` or `anonymous()` shares
    the transport of its parent. A transport that holds state for one client, such
    as cookies or a session, must give each clone a copy of its own. Inherit this
    class and write `derive()`, which takes no argument and returns a new
    transport. A clone then closes its own transport when it closes, and never the
    transport of its parent.

    A transport that does not inherit this class is shared, whatever methods it
    has. A method named `derive` on such a transport is never called.

    Examples:
        ```python {.exec}
        from pytest_graphql import DerivableTransportBase, build_client


        class CountingTransport(DerivableTransportBase):
            def __init__(self, inner):
                self.inner = inner
                self.sent = 0

            def send(self, request, *, timeout):
                self.sent += 1
                return self.inner.send(request, timeout=timeout)

            def derive(self):
                return CountingTransport(self.inner)

            def close(self):
                pass


        parent = CountingTransport(gql.transport)
        client = build_client(
            url="http://localhost:8000/graphql", transport=parent, schema=gql.schema
        )
        with client.as_("token-for-ada") as clone:
            clone.query("users", fields=["id"])
            assert clone.transport.sent == 1

        assert parent.sent == 0
        ```
    """

    @abstractmethod
    def derive(self) -> Transport:
        """Return a new transport for one clone of a client.

        The new transport must not share per-client state with this one. The
        clone closes it when the clone closes.

        Returns:
            A new transport.

        Examples:
            ```python {.exec}
            from pytest_graphql import DerivableTransportBase


            class Plain(DerivableTransportBase):
                def send(self, request, *, timeout):
                    raise NotImplementedError

                def derive(self):
                    return Plain()

                def close(self):
                    pass


            first = Plain()
            assert first.derive() is not first
            ```
        """
