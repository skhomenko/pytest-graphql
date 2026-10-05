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
    """One parsed GraphQL-over-HTTP envelope (C3), classification already run.

    A transport only ever constructs this for a response C3 classifies as a
    GraphQL result: a valid envelope carrying a ``data`` entry, whatever its
    status code. Anything else -- no ``data`` entry, an unparsable body, a
    non-2xx response with no envelope -- is a raised exception instead
    (``GraphQLRequestError``, ``GraphQLTransportError``,
    ``GraphQLHTTPStatusError``), never a ``RawResponse``. ``errors`` is the
    envelope's own ``errors`` array, exactly as received: whether it makes
    this an execution error, a partial-data result or a plain success is a
    later milestone's materialization, not this transport's classification.
    """

    status_code: int
    media_type: str
    data: Any
    errors: tuple[Mapping[str, Any], ...]
    extensions: Mapping[str, Any] | None
    headers: Mapping[str, str]
    #: C16, C17. Credentials the transport sent or received on this
    #: request's behalf outside its headers, a proxy's and every cookie
    #: value, as ``RequestInfo.transport_credentials`` pairs, so the
    #: client renders the response with the same secret set the transport
    #: scrubbed its own errors with. A server or proxy can echo one into an
    #: error ``path`` as easily as into a body. Never rendered.
    transport_credentials: tuple[tuple[str, str], ...] = field(default=(), repr=False)


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
    """Opt-in marker for the derivation capability (C19, C23).

    Inheriting this and implementing ``derive()`` is the only way a
    transport supports derivation. ``HttpxTransport`` is the one transport
    in this package that does; ``FakeTransport`` and any other custom
    ``Transport`` implementation are shared, not derived, by not inheriting
    this class.
    """

    @abstractmethod
    def derive(self) -> Transport: ...
