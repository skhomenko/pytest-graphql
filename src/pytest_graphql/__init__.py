"""Schema-aware GraphQL test client with an optional pytest plugin.

`pytest_graphql` sends GraphQL operations to a server and reads back the result.
It loads the server's schema, chooses the fields to ask for, checks arguments
before anything is sent, and returns responses that read like Python objects.

Every name on this page can be imported from `pytest_graphql` itself. Importing
the package does not import pytest, so the client works in any Python program.
Under pytest, the `gql` fixture is a ready client.

Examples:
    ```python {.exec}
    def test_user_has_a_name(gql):
        user = gql.query("user", id="u1")
        assert user.name == "Ada Lovelace"
    ```

    Outside pytest, build the client yourself and close it when you are done:

    ```python {.no-exec}
    from pytest_graphql import build_client

    with build_client(url="http://localhost:8000/graphql") as gql:
        user = gql.query("user", id="u1")
    ```
"""

from __future__ import annotations

from pytest_graphql._core.auth import Auth, BearerAuth, HeaderAuth
from pytest_graphql._core.client import (
    ClientConfig,
    GraphQLClient,
    build_client,
)
from pytest_graphql._core.diagnostics import DiagnosticSnapshot, RequestInfo
from pytest_graphql._core.errors import (
    ArgumentError,
    DiagnosticRenderError,
    ExpectedErrorNotRaised,
    GraphQLClientError,
    GraphQLConnectionError,
    GraphQLExecutionError,
    GraphQLFieldError,
    GraphQLHTTPStatusError,
    GraphQLPartialDataError,
    GraphQLRequestError,
    GraphQLTestError,
    GraphQLTimeoutError,
    GraphQLTransportError,
    OperationNotFoundError,
    ResponseShapeError,
    ScalarNotRegisteredError,
    SchemaError,
    SelectionError,
    SelectionTooLargeError,
    WaitTimeoutError,
)
from pytest_graphql._core.factory import (
    DeterministicRandom,
    ScalarRegistry,
    ScalarSpec,
    unique,
)
from pytest_graphql._core.matching import (
    Matcher,
    absent,
    any_length,
    any_value,
    contains,
    gt,
    gte,
    length,
    lt,
    lte,
    matches,
    one_of,
    unordered,
)
from pytest_graphql._core.middleware import BaseMiddleware, Middleware
from pytest_graphql._core.response import GraphQLResponse, Node, NodeList
from pytest_graphql._core.schema.source import SchemaSource
from pytest_graphql._core.selection.model import AUTO, Field, Selection
from pytest_graphql._core.selection.policy import CyclePolicy, SelectionPolicy
from pytest_graphql._core.transport.base import (
    DerivableTransportBase,
    RawResponse,
    Transport,
)

__all__ = [
    "AUTO",
    "ArgumentError",
    "Auth",
    "BaseMiddleware",
    "BearerAuth",
    "ClientConfig",
    "CyclePolicy",
    "DerivableTransportBase",
    "DeterministicRandom",
    "DiagnosticRenderError",
    "DiagnosticSnapshot",
    "ExpectedErrorNotRaised",
    "Field",
    "GraphQLClient",
    "GraphQLClientError",
    "GraphQLConnectionError",
    "GraphQLExecutionError",
    "GraphQLFieldError",
    "GraphQLHTTPStatusError",
    "GraphQLPartialDataError",
    "GraphQLRequestError",
    "GraphQLResponse",
    "GraphQLTestError",
    "GraphQLTimeoutError",
    "GraphQLTransportError",
    "HeaderAuth",
    "Matcher",
    "Middleware",
    "Node",
    "NodeList",
    "OperationNotFoundError",
    "RawResponse",
    "RequestInfo",
    "ResponseShapeError",
    "ScalarNotRegisteredError",
    "ScalarRegistry",
    "ScalarSpec",
    "SchemaError",
    "SchemaSource",
    "Selection",
    "SelectionError",
    "SelectionPolicy",
    "SelectionTooLargeError",
    "Transport",
    "WaitTimeoutError",
    "__version__",
    "absent",
    "any_length",
    "any_value",
    "build_client",
    "contains",
    "gt",
    "gte",
    "length",
    "lt",
    "lte",
    "matches",
    "one_of",
    "unique",
    "unordered",
]

__version__ = "0.1.0"
"""The installed version of `pytest-graphql`, as a string.

Examples:
    ```python {.exec}
    import pytest_graphql

    assert isinstance(pytest_graphql.__version__, str)
    ```
"""
