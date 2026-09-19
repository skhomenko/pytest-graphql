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
from dataclasses import dataclass
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


class Transport(Protocol):
    """SPEC 5.6. The complete public surface a caller may implement or call."""

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse: ...

    def close(self) -> None: ...


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
