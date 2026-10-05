"""The ``Middleware`` protocol, ``BaseMiddleware`` and the C6 ordering (B6).

Middleware is an ordered list. ``before_request`` runs first to last and
``after_response`` runs last to first, so each middleware wraps the ones after
it. ``None`` means no change, a returned object replaces the value for the
rest of the chain, and an exception aborts the call and propagates unchanged.

The return types are ``RequestInfo | None`` and ``GraphQLResponse | None`` per
C42, which corrects B6: C6 already said ``None`` means no change, and the
protocol as B6 first wrote it forbade returning it, so ``BaseMiddleware``
itself did not satisfy the declared protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.response import GraphQLResponse


@runtime_checkable
class Middleware(Protocol):
    """Code that runs around every call: one hook before it, one after it.

    Any object with these two methods is a `Middleware`. Subclass
    `BaseMiddleware` to write only the one you need. Pass a list of middleware
    to `build_client()` or `GraphQLClient` as `middleware=`.

    The list is ordered. `before_request` runs from the first item to the last,
    and `after_response` runs from the last item to the first, so each
    middleware wraps the ones after it. A hook that returns `None` changes
    nothing. A hook that returns an object replaces the request or the response
    for every hook that runs after it. A hook that raises stops the call, and
    the exception reaches the caller unchanged.

    Examples:
        ```python {.exec}
        from pytest_graphql import BaseMiddleware, build_client


        class Tracer:
            def __init__(self):
                self.events = []

            def before_request(self, request):
                self.events.append(f"send {request.operation}")

            def after_response(self, response):
                self.events.append(f"got {response.http.status_code}")


        tracer = Tracer()
        client = build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=gql.schema,
            middleware=[tracer],
        )
        client.query("user", id="u1")
        assert tracer.events == ["send user", "got 200"]
        ```
    """

    def before_request(self, request: RequestInfo) -> RequestInfo | None:
        """Look at, or replace, a request before it is sent.

        `request` carries real header and variable values. It is immutable, so
        to change it, return a new one built with `dataclasses.replace()`.

        Args:
            request: The request about to be sent.

        Returns:
            A request to send instead, or `None` to keep this one.

        Examples:
            ```python {.exec}
            from dataclasses import replace

            from pytest_graphql import BaseMiddleware, build_client


            class AddTraceHeader(BaseMiddleware):
                def before_request(self, request):
                    headers = {**request.headers, "X-Trace-Id": "t-1"}
                    return replace(request, headers=headers)


            client = build_client(
                url="http://localhost:8000/graphql",
                transport=gql.transport,
                schema=gql.schema,
                middleware=[AddTraceHeader()],
            )
            assert client.query("user", id="u1").name == "Ada Lovelace"
            ```
        """
        ...

    def after_response(
        self, response: GraphQLResponse[Any]
    ) -> GraphQLResponse[Any] | None:
        """Look at, or replace, a response after it was received.

        This runs before the client decides whether to raise because of errors
        in the response. A hook that raises here stops the call.

        Args:
            response: The response that was received.

        Returns:
            A response to use instead, or `None` to keep this one.

        Examples:
            ```python {.exec}
            from pytest_graphql import BaseMiddleware, build_client


            class SlowCalls(BaseMiddleware):
                def __init__(self):
                    self.slow = []

                def after_response(self, response):
                    if response.duration_ms > 1000:
                        self.slow.append(response.request.operation)


            slow = SlowCalls()
            client = build_client(
                url="http://localhost:8000/graphql",
                transport=gql.transport,
                schema=gql.schema,
                middleware=[slow],
            )
            client.query("user", id="u1")
            assert slow.slow == []
            ```
        """
        ...


class BaseMiddleware:
    """A `Middleware` whose hooks do nothing, to subclass.

    Override `before_request`, `after_response` or both. The hook you leave
    alone passes the request or the response through unchanged.

    Examples:
        ```python {.exec}
        from pytest_graphql import BaseMiddleware, build_client


        class CountCalls(BaseMiddleware):
            def __init__(self):
                self.count = 0

            def before_request(self, request):
                self.count += 1


        counter = CountCalls()
        client = build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=gql.schema,
            middleware=[counter],
        )
        client.query("users")
        client.query("user", id="u2")
        assert counter.count == 2
        ```
    """

    def before_request(
        self,
        request: RequestInfo,  # noqa: ARG002 -- part of the overridable contract
    ) -> RequestInfo | None:
        """Return `None`, which keeps the request as it is.

        Args:
            request: The request about to be sent.

        Returns:
            Always `None`.

        Examples:
            ```python {.exec}
            from pytest_graphql import BaseMiddleware, RequestInfo

            request = RequestInfo(
                operation=None,
                kind="query",
                document="{ __typename }",
                variables={},
                headers={},
                url="http://localhost:8000/graphql",
            )
            assert BaseMiddleware().before_request(request) is None
            ```
        """
        return None

    def after_response(
        self,
        response: GraphQLResponse[Any],  # noqa: ARG002 -- the overridable contract
    ) -> GraphQLResponse[Any] | None:
        """Return `None`, which keeps the response as it is.

        Args:
            response: The response that was received.

        Returns:
            Always `None`.

        Examples:
            ```python {.exec}
            from pytest_graphql import BaseMiddleware

            response = gql.execute("{ __typename }")
            assert BaseMiddleware().after_response(response) is None
            ```
        """
        return None


def apply_before_request(
    middleware: Sequence[Middleware], request: RequestInfo
) -> RequestInfo:
    """Run ``before_request`` first to last, folding each non-``None`` return."""
    for item in middleware:
        replacement = item.before_request(request)
        if replacement is not None:
            request = replacement
    return request


def apply_after_response(
    middleware: Sequence[Middleware], response: GraphQLResponse[Any]
) -> GraphQLResponse[Any]:
    """Run ``after_response`` last to first, folding each non-``None`` return.

    Reversed with respect to :func:`apply_before_request`, which is what
    makes each middleware wrap the ones after it rather than sit beside them.
    """
    for item in reversed(middleware):
        replacement = item.after_response(response)
        if replacement is not None:
            response = replacement
    return response
