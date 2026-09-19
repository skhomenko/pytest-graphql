"""``Transport``, ``DerivableTransportBase`` and ``RawResponse`` (C19, C23).

Derivation is opted into by inheritance alone (C23): no marker attribute, no
structural check. A transport that implements only ``send()``/``close()`` is
shared, whatever else it has, and an unrelated ``derive`` member is never
looked up.
"""

from __future__ import annotations

from typing import Any

import pytest

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.transport.base import DerivableTransportBase, RawResponse
from pytest_graphql._core.transport.httpx_transport import HttpxTransport


class _PlainTransport:
    """Implements only the ``Transport`` protocol: ``send()`` and ``close()``."""

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:
        raise NotImplementedError

    def close(self) -> None:
        return


class _TransportWithUnrelatedDerive:
    """Has a ``derive`` attribute but does not inherit the abstract base.

    C23: "an unrelated ``derive`` member is never looked up." This is not a
    capability; it just happens to share a name.
    """

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:
        raise NotImplementedError

    def close(self) -> None:
        return

    def derive(self) -> str:
        return "not a transport"


def test_httpx_transport_inherits_the_derivable_base() -> None:
    transport = HttpxTransport()
    try:
        assert isinstance(transport, DerivableTransportBase)
    finally:
        transport.close()


def test_a_plain_send_close_transport_is_not_derivable() -> None:
    assert not isinstance(_PlainTransport(), DerivableTransportBase)


def test_an_unrelated_derive_attribute_does_not_grant_derivability() -> None:
    """A same-named method is not enough; only inheritance is (C23)."""
    transport = _TransportWithUnrelatedDerive()
    assert not isinstance(transport, DerivableTransportBase)
    # The attribute is still there; it is just never treated as the capability.
    assert transport.derive() == "not a transport"


def test_derivable_transport_base_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        DerivableTransportBase()  # type: ignore[abstract]


def test_a_subclass_without_derive_cannot_be_instantiated() -> None:
    class _Incomplete(DerivableTransportBase):
        pass

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


def test_raw_response_is_frozen() -> None:
    response = RawResponse(
        status_code=200,
        media_type="application/json",
        data={"greet": "hi"},
        errors=(),
        extensions=None,
        headers={},
    )
    with pytest.raises(AttributeError):
        response.status_code = 500  # type: ignore[misc]


def test_raw_response_fields() -> None:
    error: dict[str, Any] = {"message": "boom"}
    response = RawResponse(
        status_code=200,
        media_type="application/graphql-response+json",
        data=None,
        errors=(error,),
        extensions={"tracing": {}},
        headers={"content-type": "application/graphql-response+json"},
    )
    assert response.status_code == 200
    assert response.data is None
    assert response.errors == (error,)
    assert response.extensions == {"tracing": {}}
