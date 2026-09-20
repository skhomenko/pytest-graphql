"""The ``Auth`` protocol and the two implementations that ship (B5).

``Auth.apply`` is called once per request, so a token that has to be refreshed
is refreshed inside ``apply`` and needs no extra machinery. It sits fourth in
the C4 header precedence chain: above ``ClientConfig.headers`` and the
``gql_headers`` fixture, below ``with_headers()`` and a per-call ``headers=``.

``apply`` receives the live ``RequestInfo`` (C2), because an implementation
genuinely needs the real values to sign or refresh a request. The leak
boundary is rendering, not the object, so nothing here may render one: this
module produces a new ``RequestInfo`` and never text.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.headers import merge_headers


@runtime_checkable
class Auth(Protocol):
    """One request identity (B5)."""

    def apply(self, request: RequestInfo) -> RequestInfo: ...


def with_headers(request: RequestInfo, headers: Mapping[str, str]) -> RequestInfo:
    """``request`` with ``headers`` merged over its own, under the C4 rule.

    The one place an ``Auth`` implementation in this package rewrites a
    request, so the case-insensitive replacement rule is applied once rather
    than at each implementation.
    """
    return dataclasses.replace(request, headers=merge_headers(request.headers, headers))


class BearerAuth:
    """``Authorization: Bearer <token>`` (B5).

    The token is held as given. It reaches no representation: ``RequestInfo``
    renders only its redacted snapshot, and ``authorization`` is in the
    default ``redact_headers`` set, so the rendered form is a placeholder and
    the value enters the C16 secret set for the free-form-text scrub.
    """

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token

    def apply(self, request: RequestInfo) -> RequestInfo:
        return with_headers(request, {"Authorization": f"Bearer {self.token}"})

    def __repr__(self) -> str:
        return "BearerAuth(token=<redacted>)"


class HeaderAuth:
    """A fixed set of identity headers (B5).

    B5 writes the constructor as ``HeaderAuth(**headers)``. A keyword cannot
    spell a name containing a hyphen, and almost every real identity header
    does, so an optional positional mapping is accepted as well, exactly as
    B21 does for ``with_headers()`` and for the same reason:
    ``HeaderAuth({"X-Api-Key": "v"}, Authorization="Bearer x")``.
    """

    __slots__ = ("headers",)

    def __init__(
        self, headers: Mapping[str, str] | None = None, /, **named: str
    ) -> None:
        self.headers = merge_headers(headers, named)

    def apply(self, request: RequestInfo) -> RequestInfo:
        return with_headers(request, self.headers)

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.headers))
        return f"HeaderAuth(names=[{names}], values=<redacted>)"


def as_(auth: Auth | str) -> Auth:
    """Resolve the documented shorthand: a bare string is a bearer token."""
    return BearerAuth(auth) if isinstance(auth, str) else auth
