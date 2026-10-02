"""``HttpxTransport``: the C3 response matrix, the C13 retry gate, C19
transport-level ownership, and the transport-layer leak suite.

Every case that classifies a response runs against a real local HTTP server
(``tests/unit/local_http_server.py``), not only a fake, because C3
classification depends on real HTTP framing: a status line, headers and a
body read over the wire. Unit tests may reach loopback (``tests/unit/conftest.py``).
"""

from __future__ import annotations

import base64
import json
import math
import os
import ssl
import sys
from collections.abc import Iterator, Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.errors import (
    GraphQLConnectionError,
    GraphQLHTTPStatusError,
    GraphQLRequestError,
    GraphQLTimeoutError,
    GraphQLTransportError,
)
from pytest_graphql._core.schema.info import OperationKind
from pytest_graphql._core.transport.httpx_transport import (
    MAX_TIMEOUT_SECONDS,
    HttpxTransport,
    _resolve_ssl_context,
    checked_seconds,
    checked_timeout,
)
from tests.unit.local_http_server import (
    PlannedResponse,
    Responder,
    closed_port_url,
    fixed,
    local_server,
)

_SECRET = "SECRET1234567890"  # 8+ chars: above the default min_redacted_value_length


def _make_request(
    *,
    url: str,
    kind: OperationKind = "query",
    idempotent: bool = False,
    headers: Mapping[str, str] | None = None,
    variables: Mapping[str, Any] | None = None,
    document: str = "query Greet { greet }",
    operation: str | None = "Greet",
    method: str = "POST",
) -> RequestInfo:
    return RequestInfo(
        operation=operation,
        kind=kind,
        document=document,
        variables=variables or {},
        headers=headers or {},
        url=url,
        idempotent=idempotent,
        method=method,
    )


@pytest.fixture
def transport() -> Iterator[HttpxTransport]:
    instance = HttpxTransport()
    try:
        yield instance
    finally:
        instance.close()


def _envelope(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


# --------------------------------------------------------------------------
# C3 response matrix
# --------------------------------------------------------------------------


def test_4xx_envelope_without_data_raises_request_error(
    transport: HttpxTransport,
) -> None:
    body = _envelope({"errors": [{"message": "bad request"}]})
    with (
        local_server(fixed(400, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLRequestError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert excinfo.value.status_code == 400
    assert excinfo.value.media_type == "application/json"
    assert excinfo.value.errors == ({"message": "bad request"},)


def test_200_with_errors_and_null_data_is_returned(transport: HttpxTransport) -> None:
    body = _envelope({"data": None, "errors": [{"message": "oops"}]})
    with local_server(fixed(200, content_type="application/json", body=body)) as url:
        response = transport.send(_make_request(url=url), timeout=5)
    assert response.status_code == 200
    assert response.data is None
    assert response.errors == ({"message": "oops"},)


def test_200_with_partial_data_is_returned(transport: HttpxTransport) -> None:
    body = _envelope(
        {
            "data": {"greet": None},
            "errors": [{"message": "field failed", "path": ["greet"]}],
        }
    )
    with local_server(fixed(200, content_type="application/json", body=body)) as url:
        response = transport.send(_make_request(url=url), timeout=5)
    assert response.data == {"greet": None}
    assert response.errors[0]["path"] == ["greet"]


def test_intermediary_html_error_page_raises_http_status_error(
    transport: HttpxTransport,
) -> None:
    body = b"<html><body>502 Bad Gateway</body></html>"
    with (
        local_server(fixed(502, content_type="text/html", body=body)) as url,
        pytest.raises(GraphQLHTTPStatusError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert excinfo.value.status_code == 502
    assert "Bad Gateway" in excinfo.value.body_excerpt


def test_invalid_json_at_2xx_raises_transport_error(transport: HttpxTransport) -> None:
    with (
        local_server(
            fixed(200, content_type="application/json", body=b"{not json")
        ) as url,
        pytest.raises(GraphQLTransportError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert not isinstance(excinfo.value, GraphQLHTTPStatusError)


def test_invalid_json_at_non_2xx_raises_http_status_error(
    transport: HttpxTransport,
) -> None:
    with (
        local_server(
            fixed(500, content_type="application/json", body=b"{not json")
        ) as url,
        pytest.raises(GraphQLHTTPStatusError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert excinfo.value.status_code == 500


def test_empty_body_raises_transport_error(transport: HttpxTransport) -> None:
    with (
        local_server(fixed(200, content_type="application/json", body=b"")) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_valid_json_that_is_not_an_envelope_raises(transport: HttpxTransport) -> None:
    body = _envelope({"foo": "bar"})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_errors_as_a_string_is_not_a_valid_envelope(transport: HttpxTransport) -> None:
    """F03: a value at ``errors`` that is not a list of mappings must not be
    coerced into a structured error list (CR-20260918T234353Z-6ed5cad-4b844c6d-F03)."""
    body = _envelope({"errors": "oops"})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_empty_errors_list_is_not_a_valid_envelope(transport: HttpxTransport) -> None:
    """F03: the GraphQL-over-HTTP contract never uses an empty ``errors``
    array; a body claiming one is not a valid envelope."""
    body = _envelope({"errors": []})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_errors_as_a_mapping_with_data_is_not_a_valid_envelope(
    transport: HttpxTransport,
) -> None:
    """F03: ``{"data": null, "errors": {...}}`` must not be classified as a
    success whose ``errors`` tuple is the mapping's keys."""
    body = _envelope({"data": None, "errors": {"message": "bad"}})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_error_object_missing_message_is_not_a_valid_envelope(
    transport: HttpxTransport,
) -> None:
    """F03: an ``errors`` entry with no ``message`` field is JSON that
    happens to be an object, not a GraphQL error object
    (CR-20260919T001700Z-6ed5cad-3631bad9-F03)."""
    body = _envelope({"errors": [{}]})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_error_object_with_non_string_message_is_not_a_valid_envelope(
    transport: HttpxTransport,
) -> None:
    """F03: ``GraphQLErrorInfo.message`` is typed ``str`` (SPEC.md); a
    numeric ``message`` can never satisfy that downstream, so the envelope
    carrying it is invalid at the transport boundary already."""
    body = _envelope({"errors": [{"message": 123}]})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_error_object_with_non_mapping_extensions_is_not_a_valid_envelope(
    transport: HttpxTransport,
) -> None:
    """F03: an error's own ``extensions`` must be a mapping when present,
    the same rule ``_is_envelope`` already applies at the top level."""
    body = _envelope(
        {"data": {"greet": "hi"}, "errors": [{"message": "boom", "extensions": "oops"}]}
    )
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_non_mapping_top_level_extensions_is_not_a_valid_envelope(
    transport: HttpxTransport,
) -> None:
    """F03: a non-mapping top-level ``extensions`` must invalidate the whole
    envelope, not be silently coerced to ``None``."""
    body = _envelope({"data": {"greet": "hi"}, "extensions": "oops"})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param({"message": "boom", "path": "not-a-list"}, id="path-not-a-list"),
        pytest.param({"message": "boom", "path": [True, "ok"]}, id="path-with-bool"),
        pytest.param(
            {"message": "boom", "path": [{"nested": 1}]}, id="path-with-object"
        ),
        pytest.param(
            {"message": "boom", "locations": "not-a-list"}, id="locations-not-a-list"
        ),
        pytest.param(
            {"message": "boom", "locations": [{"line": "1", "column": 2}]},
            id="location-with-string-line",
        ),
        pytest.param(
            {"message": "boom", "locations": [{"line": True, "column": 2}]},
            id="location-with-bool-line",
        ),
        pytest.param(
            {"message": "boom", "locations": [{"column": 2}]},
            id="location-missing-line",
        ),
        pytest.param(
            {"message": "boom", "locations": ["not-an-object"]},
            id="location-not-an-object",
        ),
    ],
)
def test_malformed_error_path_or_locations_is_not_a_valid_envelope(
    transport: HttpxTransport, error: Mapping[str, Any]
) -> None:
    """F03 (CR-20260919T012246Z-6ed5cad-c2157e06): ``path`` and ``locations``
    are sibling fields of the same ``GraphQLErrorInfo`` shape ``message`` and
    ``extensions`` are already validated against (SPEC.md types them
    ``tuple[str | int, ...] | None`` and ``tuple[tuple[int, int], ...] |
    None``). The prior fix enumerated only the reviewer's own examples
    instead of sweeping every typed field on the same error object."""
    body = _envelope({"data": {"greet": "hi"}, "errors": [error]})
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError),
    ):
        transport.send(_make_request(url=url), timeout=5)


def test_valid_error_path_and_locations_are_accepted(
    transport: HttpxTransport,
) -> None:
    """The positive counterpart to the malformed-shape sweep: a well-typed
    ``path``/``locations`` pair, and their documented ``null`` spelling of
    "absent," must both still classify as a valid envelope."""
    body = _envelope(
        {
            "data": None,
            "errors": [
                {
                    "message": "boom",
                    "path": ["greet", 0, "nested"],
                    "locations": [{"line": 1, "column": 7}],
                },
                {"message": "also boom", "path": None, "locations": None},
            ],
        }
    )
    with local_server(fixed(200, content_type="application/json", body=body)) as url:
        response = transport.send(_make_request(url=url), timeout=5)
    assert response.errors[0]["path"] == ["greet", 0, "nested"]
    assert response.errors[1]["path"] is None


def test_graphql_response_json_at_500_with_data_is_returned(
    transport: HttpxTransport,
) -> None:
    body = _envelope({"data": None, "errors": [{"message": "boom"}]})
    with local_server(
        fixed(500, content_type="application/graphql-response+json", body=body)
    ) as url:
        response = transport.send(_make_request(url=url), timeout=5)
    assert response.status_code == 500
    assert response.data is None
    assert response.errors == ({"message": "boom"},)


def test_graphql_response_json_at_500_without_data_raises_request_error(
    transport: HttpxTransport,
) -> None:
    body = _envelope({"errors": [{"message": "boom"}]})
    with (
        local_server(
            fixed(500, content_type="application/graphql-response+json", body=body)
        ) as url,
        pytest.raises(GraphQLRequestError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert excinfo.value.status_code == 500
    assert excinfo.value.media_type == "application/graphql-response+json"


def test_timeout_raises_timeout_error(transport: HttpxTransport) -> None:
    with (
        local_server(fixed(200, content_type="application/json", delay=1.0)) as url,
        pytest.raises(GraphQLTimeoutError),
    ):
        transport.send(_make_request(url=url), timeout=0.05)


def test_redirect_is_not_followed_and_raises_http_status_error(
    transport: HttpxTransport,
) -> None:
    with (
        local_server(
            fixed(
                302,
                content_type="text/plain",
                body=b"moved",
                extra_headers=(("Location", "http://127.0.0.1:1/elsewhere"),),
            )
        ) as url,
        pytest.raises(GraphQLHTTPStatusError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    assert excinfo.value.status_code == 302


def test_connection_refused_raises_connection_error(transport: HttpxTransport) -> None:
    request = _make_request(url=closed_port_url(), kind="mutation", idempotent=False)
    with pytest.raises(GraphQLConnectionError):
        transport.send(request, timeout=5)


def test_oversized_body_raises_transport_error() -> None:
    small_transport = HttpxTransport(max_response_bytes=16)
    try:
        body = _envelope({"data": {"greet": "x" * 200}})
        with (
            local_server(fixed(200, content_type="application/json", body=body)) as url,
            pytest.raises(GraphQLTransportError) as excinfo,
        ):
            small_transport.send(_make_request(url=url), timeout=5)
        assert "max_response_bytes=16" in str(excinfo.value)
    finally:
        small_transport.close()


def test_oversized_body_excerpt_never_exceeds_the_response_cap() -> None:
    """F02: the chunk that crosses ``max_response_bytes`` must itself be
    trimmed to the cap, not retained whole and only trimmed later by the
    diagnostic excerpt's own (much larger) budget
    (CR-20260918T234353Z-6ed5cad-4b844c6d-F02). A single 1,000,000-byte
    chunk against a 16-byte cap must leave an excerpt of exactly 16 bytes,
    never a truncated slice of the full million.
    """
    small_transport = HttpxTransport(max_response_bytes=16)
    try:
        request = _make_request(url="http://example.invalid/graphql")
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=iter([b"x" * 1_000_000]),
        )
        with pytest.raises(GraphQLTransportError) as excinfo:
            small_transport._read_capped_body(request, response)
        assert excinfo.value.body_excerpt == "x" * 16
    finally:
        small_transport.close()


# --------------------------------------------------------------------------
# C13 retry gate
# --------------------------------------------------------------------------


def test_query_retries_on_connect_failure(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "pytest_graphql._core.transport.httpx_transport._sleep", sleeps.append
    )
    request = _make_request(url=closed_port_url(), kind="query")
    with pytest.raises(GraphQLConnectionError):
        transport.send(request, timeout=5)
    assert len(sleeps) == 2  # max_attempts=3, so at most two retries


def test_ordinary_mutation_does_not_retry_on_connect_failure(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "pytest_graphql._core.transport.httpx_transport._sleep", sleeps.append
    )
    request = _make_request(url=closed_port_url(), kind="mutation", idempotent=False)
    with pytest.raises(GraphQLConnectionError):
        transport.send(request, timeout=5)
    assert sleeps == []


def test_idempotent_mutation_retries_on_connect_failure(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "pytest_graphql._core.transport.httpx_transport._sleep", sleeps.append
    )
    request = _make_request(url=closed_port_url(), kind="mutation", idempotent=True)
    with pytest.raises(GraphQLConnectionError):
        transport.send(request, timeout=5)
    assert len(sleeps) == 2


def test_read_timeout_is_never_retried_even_for_a_query(
    transport: HttpxTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "pytest_graphql._core.transport.httpx_transport._sleep", sleeps.append
    )
    with (
        local_server(fixed(200, content_type="application/json", delay=1.0)) as url,
        pytest.raises(GraphQLTimeoutError),
    ):
        transport.send(_make_request(url=url, kind="query"), timeout=0.05)
    assert sleeps == []


# --------------------------------------------------------------------------
# C19 transport-level ownership
# --------------------------------------------------------------------------


def _spy_on_pool_close(root: HttpxTransport) -> list[None]:
    """Count real calls to the root's shared pool's own ``close()``.

    ``httpcore.ConnectionPool.close()`` only drops the connections it holds
    at that moment; it does not mark the pool permanently closed, so a
    request made through it afterward can simply open a new one. That makes
    "does the pool still work" the wrong observation for C19's "closes the
    pool exactly once" -- the pool's own ``close()`` call count is the real
    contract, and this spy counts it directly.
    """
    calls: list[None] = []
    real_close = root._pool.close

    def counting_close() -> None:
        calls.append(None)
        real_close()

    root._pool.close = counting_close  # type: ignore[method-assign]
    return calls


def test_derive_with_default_flag_leaves_the_pool_usable() -> None:
    root = HttpxTransport()
    pool_closes = _spy_on_pool_close(root)
    body = _envelope({"data": {"greet": "hi"}})
    try:
        with local_server(
            fixed(200, content_type="application/graphql-response+json", body=body)
        ) as url:
            derived_a = root.derive()
            response_a = derived_a.send(_make_request(url=url), timeout=5)
            assert response_a.data == {"greet": "hi"}
            derived_a.close()
            assert pool_closes == []

            # The pool outlives the closed derived transport: a second
            # derivation from the same root still works.
            derived_b = root.derive()
            response_b = derived_b.send(_make_request(url=url), timeout=5)
            assert response_b.data == {"greet": "hi"}
            derived_b.close()
            assert pool_closes == []
    finally:
        root.close()


def test_derive_with_own_pool_closes_the_pool_exactly_once() -> None:
    root = HttpxTransport()
    pool_closes = _spy_on_pool_close(root)
    body = _envelope({"data": {"greet": "hi"}})
    try:
        with local_server(
            fixed(200, content_type="application/graphql-response+json", body=body)
        ) as url:
            owner = root.derive(own_pool=True)
            response = owner.send(_make_request(url=url), timeout=5)
            assert response.data == {"greet": "hi"}

            owner.close()
            assert len(pool_closes) == 1

            # A second close of the same derived transport raises nothing,
            # and does not call the pool's own close() again.
            owner.close()
            assert len(pool_closes) == 1
    finally:
        root.close()


def test_a_second_close_of_a_default_derived_transport_raises_nothing() -> None:
    root = HttpxTransport()
    try:
        derived = root.derive()
        derived.close()
        derived.close()
    finally:
        root.close()


# --------------------------------------------------------------------------
# Transport-layer leak suite
# --------------------------------------------------------------------------


def _leaking_request(url: str) -> RequestInfo:
    return _make_request(url=url, headers={"Authorization": f"Bearer {_SECRET}"})


def test_html_page_excerpt_never_leaks_the_request_secret(
    transport: HttpxTransport,
) -> None:
    body = f"<html>proxy denied Bearer {_SECRET}</html>".encode()
    with (
        local_server(fixed(502, content_type="text/html", body=body)) as url,
        pytest.raises(GraphQLHTTPStatusError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=5)
    assert _SECRET not in str(excinfo.value)
    assert _SECRET not in excinfo.value.body_excerpt
    assert _SECRET not in repr(excinfo.value.request)


def test_invalid_json_excerpt_never_leaks_the_request_secret(
    transport: HttpxTransport,
) -> None:
    body = f"{{not json Bearer {_SECRET}".encode()
    with (
        local_server(fixed(200, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLTransportError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=5)
    assert _SECRET not in str(excinfo.value)
    assert _SECRET not in repr(excinfo.value.request)


def test_oversized_body_excerpt_never_leaks_the_request_secret() -> None:
    small_transport = HttpxTransport(max_response_bytes=32)
    try:
        body = f"Bearer {_SECRET}".encode() + b"x" * 200
        with (
            local_server(fixed(200, content_type="application/json", body=body)) as url,
            pytest.raises(GraphQLTransportError) as excinfo,
        ):
            small_transport.send(_leaking_request(url), timeout=5)
        assert _SECRET not in str(excinfo.value)
        assert _SECRET not in excinfo.value.body_excerpt
        assert _SECRET not in repr(excinfo.value.request)
    finally:
        small_transport.close()


def test_request_error_scrubs_an_echoed_secret_in_structured_errors(
    transport: HttpxTransport,
) -> None:
    body = _envelope(
        {
            "errors": [
                {
                    "message": f"token {_SECRET} was rejected",
                    "extensions": {"received": _SECRET},
                }
            ]
        }
    )
    with (
        local_server(fixed(400, content_type="application/json", body=body)) as url,
        pytest.raises(GraphQLRequestError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=5)
    assert _SECRET not in str(excinfo.value)
    assert _SECRET not in repr(excinfo.value.request)
    for error in excinfo.value.errors:
        assert _SECRET not in json.dumps(error)


def test_connection_error_scrubs_the_underlying_exception_message() -> None:
    transport = HttpxTransport()
    try:
        request = _leaking_request("http://127.0.0.1:1/graphql")
        fake_exc = httpx.ConnectError(f"failed talking to token {_SECRET}")
        error = transport._connection_error(request, fake_exc)
        assert _SECRET not in str(error)
        assert _SECRET not in repr(error.request)
    finally:
        transport.close()


def test_timeout_error_scrubs_the_underlying_exception_message() -> None:
    transport = HttpxTransport()
    try:
        request = _leaking_request("http://127.0.0.1:1/graphql")
        fake_exc = httpx.ReadTimeout(f"stalled after sending token {_SECRET}")
        error = transport._timeout_error(request, fake_exc)
        assert _SECRET not in str(error)
        assert _SECRET not in repr(error.request)
    finally:
        transport.close()


def test_response_content_type_never_leaks_the_request_secret(
    transport: HttpxTransport,
) -> None:
    """The response's own ``Content-Type`` header is response-controlled text
    too: it must pass the same scrub as a body excerpt before reaching an
    exception's message (CR-20260918T231631Z-6ed5cad-bead5a04-F01)."""
    with (
        local_server(
            fixed(500, content_type=f"application/json; token={_SECRET}", body=b"oops")
        ) as url,
        pytest.raises(GraphQLHTTPStatusError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=5)
    assert _SECRET not in str(excinfo.value)


def test_unknown_charset_never_leaks_the_request_secret(
    transport: HttpxTransport,
) -> None:
    # _declared_charset lowercases whatever it parses out, so the secret
    # planted here must already be lowercase: a mixed-case secret would
    # survive unscrubbed by accident of case, not because scrubbing worked.
    secret = _SECRET.lower()
    with (
        local_server(
            fixed(200, content_type=f"application/json; charset={secret}", body=b"{}")
        ) as url,
        pytest.raises(GraphQLTransportError) as excinfo,
    ):
        request = _make_request(url=url, headers={"Authorization": f"Bearer {secret}"})
        transport.send(request, timeout=5)
    assert secret not in str(excinfo.value)


def _assert_no_chained_exception(exc: BaseException) -> None:
    """F01: a raw lower-level exception must never remain reachable through
    ``__cause__``/``__context__`` -- attaching it there is just as much a
    leak as embedding its text in the message, since both are ordinary
    Python attributes a traceback, logger or debugger can read
    (CR-20260918T234353Z-6ed5cad-4b844c6d-F01).
    """
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_connect_failure_does_not_chain_the_raw_httpx_exception() -> None:
    instance = HttpxTransport()
    try:
        request = _leaking_request(closed_port_url())
        with pytest.raises(GraphQLConnectionError) as excinfo:
            instance.send(request, timeout=5)
        _assert_no_chained_exception(excinfo.value)
    finally:
        instance.close()


def test_header_phase_timeout_does_not_chain_the_raw_httpx_exception(
    transport: HttpxTransport,
) -> None:
    with (
        local_server(fixed(200, content_type="application/json", delay=1.0)) as url,
        pytest.raises(GraphQLTimeoutError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=0.05)
    _assert_no_chained_exception(excinfo.value)


def test_mid_body_read_timeout_is_converted_to_graphql_timeout_error(
    transport: HttpxTransport,
) -> None:
    """F01: a timeout while the body is being streamed, after headers already
    arrived, must be covered by the same exception boundary as a
    connect-phase timeout. Before this fix it escaped as a raw
    ``httpx.ReadTimeout``, never reaching ``_classify`` at all.
    """
    body = _envelope({"data": {"greet": "hi"}})
    with (
        local_server(
            fixed(200, content_type="application/json", body=body, body_delay=1.0)
        ) as url,
        pytest.raises(GraphQLTimeoutError) as excinfo,
    ):
        transport.send(_leaking_request(url), timeout=0.05)
    _assert_no_chained_exception(excinfo.value)


def test_oversized_body_does_not_chain_a_lower_level_exception() -> None:
    small_transport = HttpxTransport(max_response_bytes=32)
    try:
        body = f"Bearer {_SECRET}".encode() + b"x" * 200
        with (
            local_server(fixed(200, content_type="application/json", body=body)) as url,
            pytest.raises(GraphQLTransportError) as excinfo,
        ):
            small_transport.send(_leaking_request(url), timeout=5)
        _assert_no_chained_exception(excinfo.value)
    finally:
        small_transport.close()


def test_unknown_charset_does_not_chain_the_lookup_error(
    transport: HttpxTransport,
) -> None:
    with (
        local_server(
            fixed(200, content_type="application/json; charset=bogus", body=b"{}")
        ) as url,
        pytest.raises(GraphQLTransportError) as excinfo,
    ):
        transport.send(_make_request(url=url), timeout=5)
    _assert_no_chained_exception(excinfo.value)


def test_malformed_url_does_not_leak_the_request_secret(
    transport: HttpxTransport,
) -> None:
    """F01: request construction (URL/header/body encoding) runs before any
    I/O, and before this fix it was outside every exception boundary in
    ``send()``. A malformed URL raises ``httpx.InvalidURL`` with a raw
    ``ValueError`` as its ``__context__``; both must be replaced, not just
    the top-level message (CR-20260919T001700Z-6ed5cad-3631bad9-F01).
    """
    request = _leaking_request(f"http://127.0.0.1:{_SECRET}/graphql")
    with pytest.raises(GraphQLTransportError) as excinfo:
        transport.send(request, timeout=5)
    _assert_no_chained_exception(excinfo.value)
    assert _SECRET not in str(excinfo.value)
    assert _SECRET not in repr(excinfo.value.request)


def test_request_construction_failure_still_covers_the_close_boundary(
    transport: HttpxTransport,
) -> None:
    """A request-construction failure must raise a redacted
    ``GraphQLTransportError``, not the raw ``httpx`` exception, even though
    ``httpx.InvalidURL`` is not an ``httpx.HTTPError`` and so is not caught
    by ``send()``'s other exception handlers."""
    request = _make_request(url="http://127.0.0.1:not-a-port/graphql")
    with pytest.raises(GraphQLTransportError) as excinfo:
        transport.send(request, timeout=5)
    assert isinstance(excinfo.value, GraphQLTransportError)
    assert not isinstance(excinfo.value, httpx.HTTPError)


def test_nfkc_rejected_netloc_does_not_leak_the_request_secret(
    transport: HttpxTransport,
) -> None:
    """The reviewer's own reproduction (CR-20260919T012246Z-6ed5cad-c2157e06-F01):
    a netloc ``urlsplit`` itself rejects under NFKC normalization, with a
    credential embedded before the offending character. ``httpx`` raises
    ``InvalidURL`` with an IDNA exception as its own ``__context__``; the
    redaction pipeline's ``urlsplit`` call, run from inside that same
    ``except`` clause, previously raised a second, unredacted ``ValueError``
    carrying the credential in its own message before a safe
    ``GraphQLTransportError`` could ever be constructed.
    """
    nfkc_rejected_netloc = chr(0x2100) + chr(0xFF0F)
    request = _leaking_request(f"http://user:{_SECRET}@{nfkc_rejected_netloc}/graphql")
    with pytest.raises(GraphQLTransportError) as excinfo:
        transport.send(request, timeout=5)
    _assert_no_chained_exception(excinfo.value)
    assert _SECRET not in str(excinfo.value)
    assert _SECRET not in repr(excinfo.value.request)


# --------------------------------------------------------------------------
# Request encoding (C3)
# --------------------------------------------------------------------------


def test_request_encoding_matches_c3() -> None:
    captured: dict[str, Any] = {}

    def responder(body: bytes, headers: Mapping[str, str]) -> PlannedResponse:
        captured["body"] = json.loads(body)
        captured["headers"] = headers
        return PlannedResponse(
            status=200,
            headers=(("Content-Type", "application/graphql-response+json"),),
            body=_envelope({"data": {"greet": "hi"}}),
        )

    instance = HttpxTransport()
    try:
        with local_server(responder) as url:
            request = _make_request(
                url=url, document="query Greet { greet }", operation="Greet"
            )
            instance.send(request, timeout=5)
    finally:
        instance.close()

    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["accept"] == (
        "application/graphql-response+json, application/json;q=0.9"
    )
    assert captured["body"] == {
        "query": "query Greet { greet }",
        "operationName": "Greet",
        "variables": {},
    }


def test_build_request_enforces_post_and_the_fixed_protocol_headers() -> None:
    """F06: method, ``Content-Type`` and ``Accept`` are wire-protocol
    invariants (DESIGN_DECISIONS.md "Transport and protocol"), not
    caller-overridable defaults (CR-20260918T234353Z-6ed5cad-4b844c6d-F06).
    A caller-supplied ``method``, ``Content-Type`` or ``Accept`` must never
    reach the wire; every other caller header still does.
    """
    instance = HttpxTransport()
    try:
        request = _make_request(
            url="http://example.invalid/graphql",
            headers={
                "Content-Type": "text/plain",
                "Accept": "text/html",
                "X-Custom": "value",
            },
            method="GET",
        )
        httpx_request = instance._build_request(request, timeout=5)
        assert httpx_request.method == "POST"
        assert httpx_request.headers["content-type"] == "application/json"
        assert httpx_request.headers["accept"] == (
            "application/graphql-response+json, application/json;q=0.9"
        )
        assert httpx_request.headers["x-custom"] == "value"
    finally:
        instance.close()


def test_build_request_applies_the_per_call_timeout_as_a_ceiling() -> None:
    """F05: the transport's own phase-specific ``Timeout`` used to be silently
    overwritten by the mandatory per-call scalar on every send
    (CR-20260918T234353Z-6ed5cad-4b844c6d-F05). Per DESIGN_DECISIONS.md,
    "Operational limits", the per-call value is a ceiling, not a
    replacement: a phase already at or under it is untouched.
    """
    instance = HttpxTransport(timeout=httpx.Timeout(connect=1, read=2, write=3, pool=4))
    try:
        request = _make_request(url="http://example.invalid/graphql")
        httpx_request = instance._build_request(request, timeout=9)
        assert httpx_request.extensions["timeout"] == {
            "connect": 1,
            "read": 2,
            "write": 3,
            "pool": 4,
        }
    finally:
        instance.close()


def test_build_request_clamps_a_looser_configured_phase_to_the_call_ceiling() -> None:
    """The other half of the same rule: a phase configured looser than the
    per-call budget is clamped down to it, never left to exceed it."""
    instance = HttpxTransport(
        timeout=httpx.Timeout(connect=1, read=20, write=3, pool=4)
    )
    try:
        request = _make_request(url="http://example.invalid/graphql")
        httpx_request = instance._build_request(request, timeout=9)
        assert httpx_request.extensions["timeout"] == {
            "connect": 1,
            "read": 9,
            "write": 3,
            "pool": 4,
        }
    finally:
        instance.close()


def test_an_unbounded_call_ceiling_leaves_every_configured_phase_as_written() -> None:
    """``ClientConfig.call_timeout()`` answers with no bound when a phase was
    deliberately configured as ``None``, because that phase means "no limit"
    and any finite ceiling would impose one. This is what that scalar does
    once it reaches the transport: the finite phases stay exactly as
    configured and the unbounded one stays ``None``, the only spelling of
    "no limit" a socket accepts.
    """
    instance = HttpxTransport(
        timeout=httpx.Timeout(connect=1, read=None, write=3, pool=4)
    )
    try:
        request = _make_request(url="http://example.invalid/graphql")
        httpx_request = instance._build_request(request, timeout=math.inf)
        assert httpx_request.extensions["timeout"] == {
            "connect": 1,
            "read": None,
            "write": 3,
            "pool": 4,
        }
    finally:
        instance.close()


def test_no_infinite_phase_ever_reaches_httpx() -> None:
    # Every way an infinity can arise: an unbounded phase under an
    # unbounded ceiling, an infinite configured phase, and an infinite
    # scalar on both sides.
    configured_forms: tuple[float | httpx.Timeout, ...] = (
        httpx.Timeout(connect=1, read=None, write=3, pool=4),
        httpx.Timeout(connect=1, read=math.inf, write=3, pool=4),
        math.inf,
    )
    for configured in configured_forms:
        instance = HttpxTransport(timeout=configured)
        try:
            request = _make_request(url="http://example.invalid/graphql")
            for ceiling in (math.inf, 5.0):
                phases = instance._build_request(request, timeout=ceiling).extensions[
                    "timeout"
                ]
                assert all(
                    phase is None or math.isfinite(phase) for phase in phases.values()
                ), (configured, ceiling, phases)
        finally:
            instance.close()


def test_an_unbounded_phase_completes_a_live_request() -> None:
    # Request metadata alone cannot show this: the infinity used to pass the
    # metadata check and then fail in ``socket.settimeout()``.
    body = _envelope({"data": {"greet": "hi"}})
    for configured in (
        httpx.Timeout(connect=1, read=None, write=2, pool=3),
        math.inf,
    ):
        instance = HttpxTransport(timeout=configured)
        try:
            with local_server(
                fixed(200, content_type="application/json", body=body)
            ) as url:
                response = instance.send(_make_request(url=url), timeout=math.inf)
            assert response.data == {"greet": "hi"}
        finally:
            instance.close()


# ``Fraction(1, 10**400)`` is positive but rounds to a zero ``float``.
_OUT_OF_DOMAIN_TIMEOUTS = (
    math.nan,
    -math.inf,
    -1.0,
    0,
    0.0,
    -(10**1000),
    Fraction(1, 10**400),
)


@pytest.mark.parametrize("bad", _OUT_OF_DOMAIN_TIMEOUTS)
def test_a_constructor_timeout_outside_the_domain_is_refused(bad: float) -> None:
    for configured in (bad, httpx.Timeout(connect=1, read=bad, write=2, pool=3)):
        with pytest.raises(ValueError, match="greater than zero"):
            HttpxTransport(timeout=configured)


@pytest.mark.parametrize("bad", _OUT_OF_DOMAIN_TIMEOUTS)
def test_a_send_timeout_outside_the_domain_is_refused_before_io(bad: float) -> None:
    # Negative infinity used to read as "no limit" and NaN reached the
    # socket. Neither may reach the server at all.
    recorder = _Recorder(200, _envelope({"data": {"greet": "hi"}}), "application/json")
    root = HttpxTransport(timeout=30)
    derived = root.derive()
    try:
        with local_server(recorder) as url:
            for transport in (root, derived):
                with pytest.raises(ValueError, match="greater than zero"):
                    transport.send(_make_request(url=url), timeout=bad)
        assert recorder.headers == []
    finally:
        derived.close()
        root.close()


#: 2_147_484 is the first whole second CPython on Windows refuses as a
#: socket timeout (``INT_MAX`` milliseconds), and 1e9 was the old maximum.
_TOO_LARGE_TIMEOUTS = (
    MAX_TIMEOUT_SECONDS * 2,
    2_147_484,
    1e9,
    1e12,
    1e308,
    10**1000,
)


@pytest.mark.parametrize("bad", _TOO_LARGE_TIMEOUTS)
def test_a_timeout_above_the_maximum_is_refused_before_io(bad: float) -> None:
    # 1e12 already overflows the socket deadline on 64-bit macOS, and an
    # int beyond float range used to overflow inside ``float()`` itself.
    for configured in (bad, httpx.Timeout(connect=1, read=bad, write=2, pool=3)):
        with pytest.raises(ValueError, match="at most") as caught:
            HttpxTransport(timeout=configured)
        assert len(str(caught.value)) < 200
    recorder = _Recorder(200, _envelope({"data": {"greet": "hi"}}), "application/json")
    instance = HttpxTransport()
    try:
        with (
            local_server(recorder) as url,
            pytest.raises(ValueError, match="at most"),
        ):
            instance.send(_make_request(url=url), timeout=bad)
        assert recorder.headers == []
    finally:
        instance.close()


def test_the_maximum_timeout_completes_a_live_request() -> None:
    body = _envelope({"data": {"greet": "hi"}})
    for limit in (MAX_TIMEOUT_SECONDS, int(MAX_TIMEOUT_SECONDS)):
        instance = HttpxTransport(timeout=limit)
        try:
            with local_server(
                fixed(200, content_type="application/json", body=body)
            ) as url:
                response = instance.send(_make_request(url=url), timeout=limit)
            assert response.data == {"greet": "hi"}
        finally:
            instance.close()


def test_a_fraction_timeout_phase_completes_a_live_request() -> None:
    # ``httpx`` stores a phase as given, and the socket refuses a
    # ``Fraction``, so an accepted phase must leave the check as a float.
    body = _envelope({"data": {"greet": "hi"}})
    configured = httpx.Timeout(
        connect=Fraction(5), read=Fraction(7, 2), write=5, pool=5
    )
    instance = HttpxTransport(timeout=configured)
    try:
        with local_server(
            fixed(200, content_type="application/json", body=body)
        ) as url:
            response = instance.send(_make_request(url=url), timeout=Fraction(10))
        assert response.data == {"greet": "hi"}
    finally:
        instance.close()


class _ShrinkingSeconds(float):
    """A ``float`` whose first conversion is its value and every later one 0."""

    conversions = 0

    def __float__(self) -> float:
        self.conversions += 1
        return float.__float__(self) if self.conversions == 1 else 0.0


def test_a_timeout_is_converted_once_and_the_checked_float_is_returned() -> None:
    # A second conversion could differ from the one that was checked.
    scalar = _ShrinkingSeconds(1.5)
    assert checked_seconds(scalar, source="timeout") == 1.5
    assert scalar.conversions == 1
    phase = _ShrinkingSeconds(2.5)
    checked = checked_timeout(
        httpx.Timeout(connect=1, read=phase, write=2, pool=3), source="timeout"
    )
    assert isinstance(checked, httpx.Timeout)
    assert checked.read == 2.5
    assert type(checked.read) is float
    assert phase.conversions == 1


@pytest.mark.parametrize("bad", ["30", True, None])
def test_a_timeout_that_is_not_a_number_is_refused(bad: object) -> None:
    with pytest.raises(TypeError, match="number of seconds"):
        HttpxTransport(timeout=bad)  # type: ignore[arg-type]
    instance = HttpxTransport()
    try:
        with pytest.raises(TypeError, match="number of seconds"):
            instance.send(
                _make_request(url="http://example.invalid/graphql"),
                timeout=bad,  # type: ignore[arg-type]
            )
    finally:
        instance.close()


def test_build_request_ceiling_applies_to_a_scalar_configured_timeout_too() -> None:
    """The common case (a scalar constructor ``timeout``, the default):
    every phase starts equal to it, so a stricter per-call value clamps all
    four -- unchanged from the pre-F05 observable behavior for this case."""
    instance = HttpxTransport()
    try:
        request = _make_request(url="http://example.invalid/graphql")
        httpx_request = instance._build_request(request, timeout=5)
        assert httpx_request.extensions["timeout"] == {
            "connect": 5,
            "read": 5,
            "write": 5,
            "pool": 5,
        }
    finally:
        instance.close()


# --------------------------------------------------------------------------
# C13 operational limits
# --------------------------------------------------------------------------


def test_trust_env_false_by_default_ignores_ambient_proxy_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus proxy that would refuse anything if it were actually used."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    instance = HttpxTransport()
    try:
        body = _envelope({"data": {"greet": "hi"}})
        with local_server(
            fixed(200, content_type="application/json", body=body)
        ) as url:
            response = instance.send(_make_request(url=url), timeout=5)
        assert response.data == {"greet": "hi"}
    finally:
        instance.close()


class _Recorder:
    """A loopback responder that counts its requests and keeps their headers."""

    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.headers: list[dict[str, str]] = []
        self._planned = PlannedResponse(
            status=status, headers=(("Content-Type", content_type),), body=body
        )

    def __call__(self, _body: bytes, headers: dict[str, str]) -> PlannedResponse:
        self.headers.append(headers)
        return self._planned


def _reflecting_proxy() -> Responder:
    """A proxy error page that quotes the proxy credential it was sent."""

    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        echoed = (
            headers.get("proxy-authorization", "")
            + " "
            + headers.get("x-proxy-token", "")
        )
        return PlannedResponse(
            status=502,
            headers=(("Content-Type", "text/plain"),),
            body=f"proxy refused credential {echoed}".encode(),
        )

    return respond


_PROXY_USER = "proxy-user-0123456789"
_PROXY_PASSWORD = "proxy-pass-0123456789"
_PROXY_PAIR = f"{_PROXY_USER}:{_PROXY_PASSWORD}"
_PROXY_TOKEN = base64.b64encode(_PROXY_PAIR.encode()).decode()
_PROXY_FORMS = (_PROXY_USER, _PROXY_PASSWORD, _PROXY_PAIR, _PROXY_TOKEN)


def _with_userinfo(url: str) -> str:
    return url.replace("http://", f"http://{_PROXY_USER}:{_PROXY_PASSWORD}@", 1)


def _rendered(exc: BaseException) -> str:
    """Every form of ``exc`` a report, a log or a traceback can show."""
    request = getattr(exc, "request", None)
    return " ".join(
        (str(exc), repr(exc), getattr(exc, "body_excerpt", ""), repr(request))
    )


def _clear_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)


def test_a_reflected_proxy_credential_never_reaches_the_error() -> None:
    # httpcore builds Proxy-Authorization below the request's redaction
    # boundary, so the request alone cannot know it. Root and derived
    # transports both quote the proxy's page, so both are checked.
    with local_server(_reflecting_proxy()) as proxy_url:
        root = HttpxTransport(proxy=_with_userinfo(proxy_url))
        derived = root.derive()
        try:
            for transport in (root, derived):
                with pytest.raises(GraphQLHTTPStatusError) as caught:
                    transport.send(
                        _make_request(url="http://target.invalid/graphql"), timeout=5
                    )
                rendered = _rendered(caught.value)
                assert "proxy refused credential" in rendered
                assert not [form for form in _PROXY_FORMS if form in rendered]
        finally:
            derived.close()
            root.close()


def test_a_proxy_header_value_never_reaches_the_error() -> None:
    secret = "proxy-header-token-0123456789"
    with local_server(_reflecting_proxy()) as proxy_url:
        proxy = httpx.Proxy(proxy_url, headers={"X-Proxy-Token": secret})
        instance = HttpxTransport(proxy=proxy)
        try:
            with pytest.raises(GraphQLHTTPStatusError) as caught:
                instance.send(
                    _make_request(url="http://target.invalid/graphql"), timeout=5
                )
            assert secret not in _rendered(caught.value)
        finally:
            instance.close()


_REPEATED_PROXY_VALUES = (
    "repeated-proxy-token-first-0123456789",
    "repeated-proxy-token-second-0123456789",
)


@pytest.mark.parametrize(
    "values", [_REPEATED_PROXY_VALUES, _REPEATED_PROXY_VALUES[::-1]]
)
def test_each_repeated_proxy_header_value_never_reaches_the_error(
    values: tuple[str, str],
) -> None:
    # A repeated header goes on the wire as one line per value, and the
    # loopback proxy quotes one of them. Both orders make each value the
    # quoted one in turn, through the root and the derived transport.
    with local_server(_reflecting_proxy()) as proxy_url:
        proxy = httpx.Proxy(
            proxy_url, headers=[("X-Proxy-Token", value) for value in values]
        )
        root = HttpxTransport(proxy=proxy)
        derived = root.derive()
        try:
            for transport in (root, derived):
                with pytest.raises(GraphQLHTTPStatusError) as caught:
                    transport.send(
                        _make_request(url="http://target.invalid/graphql"), timeout=5
                    )
                rendered = _rendered(caught.value)
                assert "proxy refused credential" in rendered
                assert not [value for value in values if value in rendered]
        finally:
            derived.close()
            root.close()


def test_an_ambient_proxy_credential_never_reaches_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    with local_server(_reflecting_proxy()) as proxy_url:
        monkeypatch.setenv("HTTP_PROXY", _with_userinfo(proxy_url))
        root = HttpxTransport(trust_env=True)
        derived = root.derive()
        try:
            with pytest.raises(GraphQLHTTPStatusError) as caught:
                derived.send(
                    _make_request(url="http://target.invalid/graphql"), timeout=5
                )
            rendered = _rendered(caught.value)
            assert "proxy refused credential" in rendered
            assert not [form for form in _PROXY_FORMS if form in rendered]
        finally:
            derived.close()
            root.close()


def test_an_invalid_proxy_url_is_refused_without_echoing_it() -> None:
    # httpx's own refusal quotes the whole URL, userinfo included.
    for bad in (
        f"ftp://{_PROXY_PAIR}@proxy.example.test",
        f"http://{_PROXY_PAIR}@proxy.example.test:notaport",
    ):
        with pytest.raises(ValueError, match="proxy URL") as caught:
            HttpxTransport(proxy=bad)
        rendered = _rendered(caught.value)
        assert not [form for form in _PROXY_FORMS if form in rendered]
        _assert_no_chained_exception(caught.value)


def test_trust_env_routes_every_transport_through_the_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    body = _envelope({"data": {"greet": "hi"}})
    proxy = _Recorder(200, body, "application/json")
    target = _Recorder(200, body, "application/json")
    with local_server(proxy) as proxy_url, local_server(target) as target_url:
        monkeypatch.setenv("HTTP_PROXY", _with_userinfo(proxy_url))
        root = HttpxTransport(trust_env=True)
        derived = root.derive()
        try:
            for transport in (root, derived):
                transport.send(_make_request(url=target_url), timeout=5)
        finally:
            derived.close()
            root.close()
    assert len(proxy.headers) == 2
    assert target.headers == []
    assert {headers["proxy-authorization"] for headers in proxy.headers} == {
        f"Basic {_PROXY_TOKEN}"
    }


def test_trust_env_honors_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_environment(monkeypatch)
    body = _envelope({"data": {"greet": "hi"}})
    proxy = _Recorder(200, body, "application/json")
    target = _Recorder(200, body, "application/json")
    with local_server(proxy) as proxy_url, local_server(target) as target_url:
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        instance = HttpxTransport(trust_env=True).derive()
        try:
            instance.send(_make_request(url=target_url), timeout=5)
        finally:
            instance.close()
    assert proxy.headers == []
    assert len(target.headers) == 1


def test_without_trust_env_no_transport_uses_the_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    body = _envelope({"data": {"greet": "hi"}})
    proxy = _Recorder(200, body, "application/json")
    target = _Recorder(200, body, "application/json")
    with local_server(proxy) as proxy_url, local_server(target) as target_url:
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        root = HttpxTransport()
        derived = root.derive()
        try:
            for transport in (root, derived):
                transport.send(_make_request(url=target_url), timeout=5)
        finally:
            derived.close()
            root.close()
    assert proxy.headers == []
    assert len(target.headers) == 2


def test_an_explicit_proxy_outranks_the_ambient_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_environment(monkeypatch)
    body = _envelope({"data": {"greet": "hi"}})
    ambient = _Recorder(200, body, "application/json")
    explicit = _Recorder(200, body, "application/json")
    with local_server(ambient) as ambient_url, local_server(explicit) as explicit_url:
        monkeypatch.setenv("HTTP_PROXY", ambient_url)
        instance = HttpxTransport(trust_env=True, proxy=explicit_url)
        try:
            instance.send(_make_request(url="http://target.invalid/graphql"), timeout=5)
        finally:
            instance.close()
    assert ambient.headers == []
    assert len(explicit.headers) == 1


def test_verify_false_emits_a_warning() -> None:
    with pytest.warns(UserWarning, match="TLS certificate verification"):
        instance = HttpxTransport(verify=False)
    instance.close()


def test_resolve_ssl_context_ignores_ambient_sslkeylogfile_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F04: ``trust_env=False`` promises ambient ``SSLKEYLOGFILE`` is ignored
    (CR-20260918T234353Z-6ed5cad-4b844c6d-F04), but Python's
    ``ssl.create_default_context`` honors that variable on its own, no matter
    what HTTPX's ``trust_env`` says. The context this transport builds for
    itself must neutralize it explicitly.

    ``_resolve_ssl_context`` no longer calls ``ssl.create_default_context``
    at all for this path (CR-20260919T021158Z-6ed5cad-605986bf-F01): the
    named file is never opened, so it never appears on disk, not merely
    "never carries a secret."
    """
    keylog_path = tmp_path / "keylog"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_path))
    context = _resolve_ssl_context(True, trust_env=False)
    assert isinstance(context, ssl.SSLContext)
    assert context.keylog_filename is None
    assert not keylog_path.exists()


def test_resolve_ssl_context_survives_an_unusable_ambient_sslkeylogfile_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-20260919T021158Z-6ed5cad-605986bf-F01: the prior fix called
    ``ssl.create_default_context`` unmodified and only cleared
    ``keylog_filename`` afterwards, so ``SSLKEYLOGFILE`` pointing below a
    directory that does not exist made the eager open inside that call raise
    ``FileNotFoundError`` before the clear could ever run -- an unrelated
    ambient value turning transport construction into a denial of service.
    Not calling ``ssl.create_default_context`` at all closes this
    unconditionally, regardless of what the path is set to.
    """
    unusable_path = tmp_path / "does-not-exist" / "keylog"
    monkeypatch.setenv("SSLKEYLOGFILE", str(unusable_path))
    context = _resolve_ssl_context(True, trust_env=False)
    assert isinstance(context, ssl.SSLContext)
    assert not unusable_path.parent.exists()

    instance = HttpxTransport(trust_env=False)
    instance.close()


def test_resolve_ssl_context_reads_no_environment_variable() -> None:
    """The property behind both tests above, stated directly: building the
    ``trust_env=False`` context for ``verify=True`` never consults
    ``os.environ`` at all, so nothing about the ambient environment --
    present, absent, unusable, or racing with a concurrent writer -- can
    affect what this returns.

    This installs the probe directly with ``del`` in a ``finally``, rather
    than through ``monkeypatch.setattr``: ``os.environ.get`` is inherited
    from a mixin, not set on the instance, so ``hasattr`` sees it and
    ``monkeypatch``'s own restore re-``setattr``s the original bound method
    onto the instance instead of removing the override -- which would leave
    exactly the instance-level ``get`` this suite's other tests assert is
    never present.
    """

    def exploding_get(key: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"unexpected os.environ.get({key!r}) call")

    os.environ.get = exploding_get  # type: ignore[method-assign]
    try:
        context = _resolve_ssl_context(True, trust_env=False)
    finally:
        del os.environ.get  # type: ignore[attr-defined]
    assert isinstance(context, ssl.SSLContext)


def test_resolve_ssl_context_leaves_an_explicit_context_untouched() -> None:
    """An ``ssl.SSLContext`` the caller already built is an explicit choice,
    not ambient configuration, so it is returned unchanged."""
    explicit = ssl.create_default_context()
    assert _resolve_ssl_context(explicit, trust_env=False) is explicit


def test_resolve_ssl_context_accepts_a_ca_bundle_file_path(tmp_path: Path) -> None:
    """DESIGN_DECISIONS.md, "Operational limits": ``verify`` accepts a CA
    bundle path, not only ``True``/``False``/an ``ssl.SSLContext``. This
    exercises the ``cafile`` branch of the new local reconstruction."""
    bundle = tmp_path / "bundle.pem"
    bundle.write_text(Path(certifi.where()).read_text())
    context = _resolve_ssl_context(str(bundle), trust_env=False)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_resolve_ssl_context_accepts_a_ca_bundle_directory(tmp_path: Path) -> None:
    """The ``capath`` branch: an OpenSSL hashed-directory CA store, per the
    same design-decision sentence as the file-path case above.
    ``load_verify_locations(capath=...)`` accepts an empty directory without
    raising (it simply finds no certificates in it), so this only needs to
    prove the directory branch is taken and produces a usable context, not
    that any particular certificate loads from it.
    """
    context = _resolve_ssl_context(str(tmp_path), trust_env=False)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_resolve_ssl_context_matches_the_running_interpreters_default_policy() -> None:
    """CR-20260919T023658Z-6ed5cad-93c4fe37-F01: the locally reconstructed
    context must OR in ``VERIFY_X509_PARTIAL_CHAIN``/``VERIFY_X509_STRICT``
    only on Python 3.13+, matching ``ssl.create_default_context()``'s own
    version-dependent policy rather than the mere availability of the flag
    constants (both have existed since well before 3.13). This asserts
    against the interpreter actually running the suite, so it holds across
    the whole supported matrix rather than only the interpreter used to
    write the fix.
    """
    context = _resolve_ssl_context(True, trust_env=False)
    assert isinstance(context, ssl.SSLContext)
    expect_stricter_flags = sys.version_info >= (3, 13)
    has_partial_chain = bool(context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
    has_strict = bool(context.verify_flags & ssl.VERIFY_X509_STRICT)
    assert has_partial_chain == expect_stricter_flags
    assert has_strict == expect_stricter_flags


def test_trust_env_false_by_default_ignores_ambient_sslkeylogfile_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end companion to the ``_resolve_ssl_context`` unit tests above:
    the pool ``HttpxTransport.__init__`` actually builds must carry no
    ambient key-log target either."""
    keylog_path = tmp_path / "keylog"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_path))
    instance = HttpxTransport()
    try:
        pool = instance._pool
        assert isinstance(pool, httpx.HTTPTransport)
        ssl_context = pool._pool._ssl_context
        assert isinstance(ssl_context, ssl.SSLContext)
        assert ssl_context.keylog_filename is None
    finally:
        instance.close()
    assert not keylog_path.exists()


def test_transport_module_reload_does_not_break_os_environ_get() -> None:
    """CR-20260919T014345Z-6ed5cad-f040aef1-F01: the removed ``os.environ.get``
    replacement captured the currently installed ``get`` as its own delegation
    target at *module* import time. Reloading the module re-executed that
    capture while the old replacement was still installed, made the new one a
    no-op through the reinstall guard, and left the module's own global
    pointing the old replacement at itself -- every later
    ``os.environ.get`` call then recursed until ``RecursionError``. The
    current fix installs nothing at import time, so a reload has nothing to
    corrupt.
    """
    import importlib

    from pytest_graphql._core.transport import httpx_transport

    real_path = os.environ.get("PATH")
    importlib.reload(httpx_transport)
    assert "get" not in vars(os.environ)
    assert os.environ.get("PATH") == real_path
    context = httpx_transport._resolve_ssl_context(True, trust_env=False)
    assert isinstance(context, ssl.SSLContext)


# -- URL userinfo is a fallback below an explicit Authorization (C4) ----------

_USERINFO = ("wire-user", "wire-password")
_BASIC = "Basic " + base64.b64encode(":".join(_USERINFO).encode()).decode()


def _recording_authorization(seen: list[str | None]) -> Any:
    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        seen.append(headers.get("authorization"))
        return PlannedResponse(
            200,
            (("Content-Type", "application/json"),),
            _envelope({"data": {"greet": "hi"}}),
        )

    return respond


@pytest.mark.parametrize(
    ("headers", "on_the_wire"),
    [
        ({}, _BASIC),
        ({"Authorization": "Bearer explicit-token"}, "Bearer explicit-token"),
        ({"authorization": "Bearer explicit-token"}, "Bearer explicit-token"),
    ],
)
def test_url_userinfo_supplies_basic_auth_only_without_an_explicit_header(
    transport: HttpxTransport, headers: dict[str, str], on_the_wire: str
) -> None:
    # CR-20261001T205548Z-c9bc07f-fc2f7f97-F03. Left to itself httpx turns
    # userinfo into Basic during ``send`` and replaces the explicit header.
    seen: list[str | None] = []
    with local_server(_recording_authorization(seen)) as url:
        target = url.replace("http://", "http://{}:{}@".format(*_USERINFO), 1)
        transport.send(_make_request(url=target, headers=headers), timeout=5.0)

    assert seen == [on_the_wire]


def test_a_url_without_userinfo_sends_no_authorization(
    transport: HttpxTransport,
) -> None:
    seen: list[str | None] = []
    with local_server(_recording_authorization(seen)) as url:
        transport.send(_make_request(url=url), timeout=5.0)

    assert seen == [None]
