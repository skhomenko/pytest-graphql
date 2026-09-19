"""Response model (M5b): schema-aware materialization and the envelope.

``build_response`` is the one entry point M5c's client calls. Nothing here
opens a socket or imports pytest.
"""

from __future__ import annotations

from pytest_graphql._core.response.envelope import (
    GraphQLResponse,
    build_response,
)
from pytest_graphql._core.response.node import Node, NodeList
from pytest_graphql._core.response.types import GraphQLErrorInfo, HttpInfo

__all__ = [
    "GraphQLErrorInfo",
    "GraphQLResponse",
    "HttpInfo",
    "Node",
    "NodeList",
    "build_response",
]
