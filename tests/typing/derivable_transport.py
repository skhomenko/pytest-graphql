"""C23: an incompatible ``derive()`` is a strict type error where it is written.

Derivation is opted into by inheriting ``DerivableTransportBase``, whose one
abstract method takes no argument besides ``self`` and returns a
``Transport``. A subclass that changes that signature must fail the strict
check at its own definition, not at a distant call site.

This file is checked by ``tests/typing/test_typing_fixtures.py`` and never
run. A ``# expect:`` marker names the error code mypy must report on that
line, and a ``# reveal:`` marker names the type ``reveal_type`` must show,
without module paths.
"""

from __future__ import annotations

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.transport.base import (
    DerivableTransportBase,
    RawResponse,
    Transport,
)


class _Derivable(DerivableTransportBase):
    """The required shape: ``derive(self) -> Transport``, beside the protocol."""

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:
        raise NotImplementedError

    def close(self) -> None:
        return

    def derive(self) -> _Derivable:
        return _Derivable()


class _TakesASchema(DerivableTransportBase):
    def derive(self, schema: object) -> Transport:  # expect: override
        raise NotImplementedError


class _ReturnsNoTransport(DerivableTransportBase):
    def derive(self) -> str:  # expect: override
        return "not a transport"


class _NeverDerives(DerivableTransportBase):
    pass


derivable: Transport = _Derivable()
reveal_type(_Derivable().derive())  # reveal: _Derivable
_NeverDerives()  # expect: abstract
