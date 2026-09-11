"""An in-process transport that runs the hostile schema with ``graphql_sync``.

``FakeTransport`` stands in for a real network transport in unit tests (SPEC
11.2, the "in-process" layer). It takes a query document and variables and
returns the same ``{"data": ..., "errors": ...}`` envelope shape a real HTTP
GraphQL endpoint would send back, without opening a socket.
"""

from __future__ import annotations

from typing import Any

from graphql import ExecutionResult, GraphQLSchema, graphql_sync


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
