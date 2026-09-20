"""An in-process transport that runs the hostile schema with ``graphql_sync``.

``FakeTransport`` is the ``QueryExecutor`` shape schema loading needs: it
takes a query document and variables and returns the same
``{"data": ..., "errors": ...}`` envelope a real HTTP GraphQL endpoint would
send back, without opening a socket. ``FakeGraphQLTransport`` is the
``Transport`` shape a client needs, over the same execution (SPEC 11.2, the
"in-process" layer).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphql import ExecutionResult, GraphQLSchema, graphql_sync

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.transport.base import RawResponse


class FakeTransport:
    """Executes documents against an in-memory schema, synchronously."""

    def __init__(self, schema: GraphQLSchema) -> None:
        self._schema = schema

    def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        result: ExecutionResult = graphql_sync(
            self._schema,
            query,
            variable_values=variables,
            operation_name=operation_name,
        )
        envelope: dict[str, Any] = {"data": result.data}
        if result.errors:
            envelope["errors"] = [error.formatted for error in result.errors]
        return envelope


class FakeGraphQLTransport:
    """A ``Transport`` over the same in-process execution (SPEC 11.2, C19).

    Implements ``send()`` and ``close()`` and nothing else, and deliberately
    does not inherit ``DerivableTransportBase``: it is the documented case of
    a plain two-method transport that must construct, clone and close with no
    extra method. It holds no cookie jar, so sharing one between clones is
    safe.

    ``sent`` records the live requests, so a test can assert what the client
    built. ``close_calls`` counts closes, so an ownership test can assert that
    a client closed nothing the caller owns.
    """

    def __init__(
        self,
        schema: GraphQLSchema,
        *,
        status_code: int = 200,
        media_type: str = "application/graphql-response+json",
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._schema = schema
        self._status_code = status_code
        self._media_type = media_type
        self._response_headers = dict(response_headers or {})
        self.sent: list[RequestInfo] = []
        self.close_calls = 0

    def send(
        self,
        request: RequestInfo,
        *,
        timeout: float,  # noqa: ARG002 -- part of the Transport contract
    ) -> RawResponse:
        self.sent.append(request)
        result: ExecutionResult = graphql_sync(
            self._schema,
            request.document,
            variable_values=dict(request.variables),
            operation_name=request.operation,
        )
        return RawResponse(
            status_code=self._status_code,
            media_type=self._media_type,
            data=result.data,
            errors=tuple(error.formatted for error in result.errors or ()),
            extensions=None,
            headers=dict(self._response_headers),
        )

    def close(self) -> None:
        self.close_calls += 1
