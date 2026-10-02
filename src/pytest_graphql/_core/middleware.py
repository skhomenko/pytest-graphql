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
    """One hook pair around a call (B6, return types per C42)."""

    def before_request(self, request: RequestInfo) -> RequestInfo | None: ...

    def after_response(
        self, response: GraphQLResponse[Any]
    ) -> GraphQLResponse[Any] | None: ...


class BaseMiddleware:
    """No-op defaults, so an implementer overrides one method (B6)."""

    def before_request(
        self,
        request: RequestInfo,  # noqa: ARG002 -- part of the overridable contract
    ) -> RequestInfo | None:
        return None

    def after_response(
        self,
        response: GraphQLResponse[Any],  # noqa: ARG002 -- the overridable contract
    ) -> GraphQLResponse[Any] | None:
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
