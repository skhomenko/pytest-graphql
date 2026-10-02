"""Schema-aware GraphQL test client with an optional pytest plugin.

The public API is re-exported from this module. Per C9, only names a user
constructs, catches, annotates or calls belong in ``__all__``; a name lands
here in the milestone that implements it, and the rest of the documented
surface arrives with the milestones that still owe it. ``__version__`` is the
single source of the distribution version, and the release workflow checks
it against the tag.

Importing this module imports no pytest (C41). The pytest layer lives under
``pytest_graphql.plugin`` and nothing here reaches into it.
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
    DiagnosticRenderError,
    GraphQLFieldError,
    ResponseShapeError,
)
from pytest_graphql._core.middleware import BaseMiddleware, Middleware
from pytest_graphql._core.response import GraphQLResponse, Node, NodeList
from pytest_graphql._core.schema.source import SchemaSource
from pytest_graphql._core.selection.model import AUTO, Field, Selection
from pytest_graphql._core.selection.policy import CyclePolicy, SelectionPolicy
from pytest_graphql._core.transport.base import Transport

__all__ = [
    "AUTO",
    "Auth",
    "BaseMiddleware",
    "BearerAuth",
    "ClientConfig",
    "CyclePolicy",
    "DiagnosticRenderError",
    "DiagnosticSnapshot",
    "Field",
    "GraphQLClient",
    "GraphQLFieldError",
    "GraphQLResponse",
    "HeaderAuth",
    "Middleware",
    "Node",
    "NodeList",
    "RequestInfo",
    "ResponseShapeError",
    "SchemaSource",
    "Selection",
    "SelectionPolicy",
    "Transport",
    "__version__",
    "build_client",
]

__version__ = "0.1.0a1.dev0"
