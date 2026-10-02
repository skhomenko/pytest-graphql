"""The diagnostics recorder (B3): its bound, its dump, and what a client records.

The recorder is a ``deque`` with a fixed ``maxlen``, so a long run outside
pytest keeps the newest calls and never grows. A client records exactly one
call for every request it hands to the transport, whatever happens after:
the transport raising, a response that contradicts the schema, a raising
``after_response`` middleware, or the raising table. A call refused before
it is sent records nothing, because no request left the client. Recording
never replaces the exception the caller is waiting for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import (
    DEFAULT_MAX_RECORDED_CALLS,
    DiagnosticSnapshot,
    DiagnosticsRecorder,
    OmissionRecord,
    RecordedCall,
    RequestInfo,
)
from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLExecutionError,
    GraphQLPartialDataError,
    ResponseShapeError,
)
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.response import GraphQLResponse
from pytest_graphql._core.transport.base import RawResponse
from tests.schema.resolvers import build_schema

_URL = "https://example.test/graphql"
_SECRET = "rec-secret-0123456789"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


def _snapshot(operation: str | None = "Greet", **overrides: Any) -> DiagnosticSnapshot:
    fields: dict[str, Any] = {"variables": {}, "headers": {}, **overrides}
    return RequestInfo(
        operation=operation,
        kind="query",
        document="query { greet }",
        url=_URL,
        **fields,
    ).redacted()


def _call(operation: str = "Greet", **fields: Any) -> RecordedCall:
    return RecordedCall(request=_snapshot(operation), outcome="ok", **fields)


def _envelope(data: Any, errors: tuple[dict[str, Any], ...] = ()) -> RawResponse:
    return RawResponse(
        status_code=200,
        media_type="application/graphql-response+json",
        data=data,
        errors=errors,
        extensions=None,
        headers={},
    )


class _Scripted:
    """A transport that returns or raises what the test planned, and counts."""

    def __init__(self, outcome: RawResponse | BaseException) -> None:
        self._outcome = outcome
        self.sends = 0

    def send(
        self,
        request: RequestInfo,  # noqa: ARG002 -- part of the Transport contract
        *,
        timeout: float,  # noqa: ARG002 -- part of the Transport contract
    ) -> RawResponse:
        self.sends += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    def close(self) -> None:
        pass


class _RaisingAfter(BaseMiddleware):
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def after_response(
        self,
        response: GraphQLResponse[Any],  # noqa: ARG002 -- the Middleware contract
    ) -> None:
        raise self.failure


class _RaisingBefore(BaseMiddleware):
    def before_request(
        self,
        request: RequestInfo,  # noqa: ARG002 -- the Middleware contract
    ) -> None:
        raise RuntimeError("refused before sending")


def _client(
    schema: GraphQLSchema,
    transport: _Scripted,
    *,
    middleware: tuple[BaseMiddleware, ...] = (),
    **config: Any,
) -> GraphQLClient:
    return GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url=_URL, **config),
        middleware=middleware,
    )


# -- the bound ----------------------------------------------------------------


def test_the_default_bound_is_fifty_calls(schema: GraphQLSchema) -> None:
    client = _client(schema, _Scripted(_envelope({"user": {"id": "u1"}})))

    assert DEFAULT_MAX_RECORDED_CALLS == 50
    assert DiagnosticsRecorder().max_calls == 50
    assert client.recorder.max_calls == 50


@pytest.mark.parametrize("capacity", [1, 3, DEFAULT_MAX_RECORDED_CALLS])
def test_the_oldest_call_is_evicted_at_the_bound(capacity: int) -> None:
    recorder = DiagnosticsRecorder(capacity)
    calls = [_call(f"Op{index}") for index in range(capacity + 7)]
    for call in calls:
        recorder.record(call)

    assert len(recorder) == capacity
    assert recorder.calls == tuple(calls[-capacity:])


@pytest.mark.parametrize("capacity", [0, -1])
def test_a_bound_of_zero_or_below_records_nothing(capacity: int) -> None:
    recorder = DiagnosticsRecorder(capacity)
    recorder.record(_call())

    assert recorder.max_calls == 0
    assert len(recorder) == 0
    assert recorder.dump() == "no GraphQL calls recorded"


def test_the_client_bound_comes_from_its_config(schema: GraphQLSchema) -> None:
    transport = _Scripted(_envelope({"user": {"id": "u1"}}))
    client = _client(schema, transport, max_recorded_calls=3)

    for _ in range(10):
        client.query("user", id="u1", fields=["id"])

    assert client.recorder.max_calls == 3
    assert len(client.recorder) == 3
    assert transport.sends == 10


def test_calls_is_a_copy_that_later_records_do_not_change() -> None:
    recorder = DiagnosticsRecorder(2)
    first = _call("First")
    recorder.record(first)
    seen = recorder.calls

    recorder.record(_call("Second"))
    recorder.record(_call("Third"))

    assert seen == (first,)


def test_clear_empties_the_recorder_and_keeps_its_bound() -> None:
    recorder = DiagnosticsRecorder(2)
    recorder.record(_call())
    recorder.clear()

    assert len(recorder) == 0
    assert recorder.max_calls == 2
    assert recorder.dump() == "no GraphQL calls recorded"
    recorder.record(_call("After"))
    assert [call.request.operation for call in recorder.calls] == ["After"]


# -- the dump -----------------------------------------------------------------


def test_the_dump_numbers_only_the_calls_it_still_holds() -> None:
    recorder = DiagnosticsRecorder(2)
    for operation in ("Gone", "Kept", "Last"):
        recorder.record(_call(operation))

    lines = recorder.dump().splitlines()

    assert lines[0].startswith("1. query Kept -> ok")
    assert lines[2].startswith("2. query Last -> ok")
    assert "Gone" not in recorder.dump()


def test_the_dump_shows_every_field_of_a_call() -> None:
    omissions = tuple(OmissionRecord("User", ("friends",), "depth") for _ in range(2))
    request = _snapshot(
        "Shown",
        variables={"note": "x" * 400},
        max_diagnostic_bytes=64,
        omissions=omissions,
        omissions_total=5,
    )
    recorder = DiagnosticsRecorder()
    recorder.record(RecordedCall(request=request, outcome="failed", failure="boom"))
    recorder.record(
        RecordedCall(
            request=_snapshot(None),
            outcome="errors",
            status_code=200,
            duration_ms=12.34,
            error_count=2,
        )
    )

    assert recorder.dump().splitlines() == [
        "1. query Shown -> failed (status -, 0.0 ms, 0 error(s))",
        f"   POST {_URL}",
        "   failure: boom",
        f"   truncated: {', '.join(request.truncated)}",
        "   auto-selection omitted: User.friends (depth), User.friends (depth)",
        "   ... and 3 further omission(s) not shown, of 5 total",
        "2. query <anonymous> -> errors (status 200, 12.3 ms, 2 error(s))",
        f"   POST {_URL}",
    ]
    assert request.truncated


# -- what a client records ----------------------------------------------------


def _ok() -> RawResponse:
    return _envelope({"user": {"id": "u1"}})


def _boom() -> tuple[dict[str, Any], ...]:
    return ({"message": "boom"},)


#: Each case: the transport outcome, the middleware, the call options, and
#: what the call raises (or ``None``), the recorded outcome and status.
_Case = tuple[
    Callable[[], RawResponse | BaseException],
    Callable[[], tuple[BaseMiddleware, ...]],
    dict[str, Any],
    type[BaseException] | None,
    str,
    int | None,
]

_CASES: dict[str, _Case] = {
    "ok": (_ok, tuple, {}, None, "ok", 200),
    "errors-returned": (
        lambda: _envelope({"user": {"id": "u1"}}, _boom()),
        tuple,
        {"raise_on_error": False},
        None,
        "errors",
        200,
    ),
    "errors-raised": (
        lambda: _envelope(None, _boom()),
        tuple,
        {},
        GraphQLExecutionError,
        "errors",
        200,
    ),
    "partial-raised": (
        lambda: _envelope({"user": {"id": "u1"}}, _boom()),
        tuple,
        {},
        GraphQLPartialDataError,
        "errors",
        200,
    ),
    "transport-raised": (
        lambda: ConnectionError("refused"),
        tuple,
        {},
        ConnectionError,
        "failed",
        None,
    ),
    "transport-interrupted": (
        KeyboardInterrupt,
        tuple,
        {},
        KeyboardInterrupt,
        "failed",
        None,
    ),
    "shape-refused": (
        lambda: _envelope({"user": "not-an-object"}),
        tuple,
        {},
        ResponseShapeError,
        "failed",
        200,
    ),
    "after-response-raised": (
        _ok,
        lambda: (_RaisingAfter(RuntimeError("middleware failed")),),
        {},
        RuntimeError,
        "failed",
        200,
    ),
    "after-response-interrupted": (
        _ok,
        lambda: (_RaisingAfter(KeyboardInterrupt()),),
        {},
        KeyboardInterrupt,
        "failed",
        200,
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_every_sent_call_is_recorded_exactly_once(
    schema: GraphQLSchema, case: str
) -> None:
    outcome, middleware, options, raises, recorded, status = _CASES[case]
    transport = _Scripted(outcome())
    client = _client(schema, transport, middleware=middleware())

    if raises is None:
        client.query("user", id="u1", fields=["id"], **options)
    else:
        with pytest.raises(raises):
            client.query("user", id="u1", fields=["id"], **options)

    assert transport.sends == 1
    (call,) = client.recorder.calls
    assert call.outcome == recorded
    assert call.status_code == status
    assert call.duration_ms >= 0.0
    assert call.error_count == (len(_boom()) if recorded == "errors" else 0)
    assert bool(call.failure) == (recorded == "failed")
    assert isinstance(call.request, DiagnosticSnapshot)
    assert call.request.operation == "user"


def test_a_failure_after_the_transport_returned_names_its_cause(
    schema: GraphQLSchema,
) -> None:
    client = _client(schema, _Scripted(_envelope({"user": "not-an-object"})))

    with pytest.raises(ResponseShapeError):
        client.query("user", id="u1", fields=["id"])

    assert client.recorder.calls[0].failure.startswith("ResponseShapeError: ")


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise ValueError("this message cannot be rendered")


@pytest.mark.parametrize("where", ["transport", "after-response"])
def test_recording_never_replaces_the_callers_exception(
    schema: GraphQLSchema, where: str
) -> None:
    failure = _UnprintableError()
    if where == "transport":
        client = _client(schema, _Scripted(failure))
    else:
        middleware = (_RaisingAfter(failure),)
        client = _client(schema, _Scripted(_ok()), middleware=middleware)

    with pytest.raises(_UnprintableError) as caught:
        client.query("user", id="u1", fields=["id"])

    assert caught.value is failure
    (call,) = client.recorder.calls
    assert call.failure == "_UnprintableError: <message unavailable>"


def test_a_secret_in_a_middleware_failure_does_not_reach_the_dump(
    schema: GraphQLSchema,
) -> None:
    middleware = (_RaisingAfter(RuntimeError(f"rejected token {_SECRET}")),)
    client = _client(
        schema,
        _Scripted(_ok()),
        middleware=middleware,
        headers={"Authorization": f"Bearer {_SECRET}"},
    )

    with pytest.raises(RuntimeError):
        client.query("user", id="u1", fields=["id"])
    (call,) = client.recorder.calls

    assert "rejected token" in call.failure
    assert _SECRET not in call.failure
    assert _SECRET not in client.recorder.dump()


_REFUSALS: dict[str, tuple[dict[str, Any], tuple[BaseMiddleware, ...], type]] = {
    "unknown-argument": ({"nope": 1}, (), ArgumentError),
    "before-request-raised": ({}, (_RaisingBefore(),), RuntimeError),
    "timeout-refused": ({"timeout": None}, (), TypeError),
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_a_call_refused_before_sending_records_nothing(
    schema: GraphQLSchema, case: str
) -> None:
    options, middleware, raises = _REFUSALS[case]
    transport = _Scripted(_ok())
    client = _client(schema, transport, middleware=middleware)

    with pytest.raises(raises):
        client.query("user", id="u1", fields=["id"], **options)

    assert transport.sends == 0
    assert len(client.recorder) == 0


def test_clones_record_into_their_parents_recorder(schema: GraphQLSchema) -> None:
    # A report shows one test's calls in order, whichever identity made them.
    client = _client(schema, _Scripted(_ok()), max_recorded_calls=2)
    clones = [client.as_("token-a-0123"), client.with_headers({"X-Trace": "1"})]

    for caller in (client, *clones):
        caller.query("user", id="u1", fields=["id"])

    for clone in clones:
        assert clone.recorder is client.recorder
    assert len(client.recorder) == 2
