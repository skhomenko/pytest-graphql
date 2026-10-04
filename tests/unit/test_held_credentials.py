"""A credential an earlier call sent is scrubbed from the text of a later call.

Text a call records is scrubbed and then cut, while the live request is in hand.
A server can repeat a value it was sent before, and a cap can end inside it. The
request of the later call does not carry the earlier call's header, variable,
auth value or cookie, so the cut text would show the start of it. The client
therefore keeps the credentials of past calls, bounded, shared by its clones, and
lists them in the secret set of every later call.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.auth import with_headers
from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import (
    DEFAULT_MIN_REDACTED_VALUE_LENGTH,
    HISTORY_GAP,
    HISTORY_WITHHELD,
    MAX_HELD_CREDENTIALS,
    CredentialLedger,
    DiagnosticsRecorder,
    RequestInfo,
    has_history_gap,
    request_credentials,
    safe_excerpt,
    sanitize_text,
    withhold_if_gapped,
)
from pytest_graphql._core.errors import GraphQLTransportError
from pytest_graphql._core.excerpt import render_data_excerpt
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.transport.httpx_transport import HttpxTransport
from tests.unit.local_http_server import PlannedResponse, local_server
from tests.unit.scripted_steps import build_test_schema, envelope, failure, make_client

SECRET = "past-call-secret-value-0123456789"
ECHO = f"the server said: {SECRET}"
LIMITS = range(0, 60, 3)


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


class FirstCallOnly(BaseMiddleware):
    """Adds the credential to the first request only."""

    def __init__(self) -> None:
        self.seen = 0

    def before_request(self, request: RequestInfo) -> RequestInfo | None:
        self.seen += 1
        if self.seen > 1:
            return None
        return with_headers(request, {"Authorization": f"Bearer {SECRET}"})


def first_response(how: str):
    plain = envelope({"user": {"id": "u1"}})
    if how == "response cookie":
        return dataclasses.replace(plain, transport_credentials=(("cookie", SECRET),))
    return plain


def send_first(client, how: str) -> None:
    fields = {"id": "u1", "fields": ["id"]}
    if how == "per-call header":
        client.query("user", **fields, headers={"Authorization": f"Bearer {SECRET}"})
    elif how == "clone header":
        client.with_headers(Authorization=f"Bearer {SECRET}").query("user", **fields)
    elif how == "auth":
        client.as_(SECRET).query("user", **fields)
    elif how == "variable":
        client.query("user", id=SECRET, fields=["id"])
    else:
        client.query("user", **fields)


HOWS = [
    "per-call header",
    "clone header",
    "auth",
    "variable",
    "middleware",
    "response cookie",
]


def started(text: str) -> bool:
    return SECRET[:DEFAULT_MIN_REDACTED_VALUE_LENGTH] in text


def build(schema: GraphQLSchema, how: str, second, **config):
    middleware = [FirstCallOnly()] if how == "middleware" else []
    if how == "variable":
        config["redact_variables"] = ("id",)
    return make_client(
        schema, first_response(how), second, middleware=middleware, **config
    )


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("limit", LIMITS)
def test_a_cut_through_an_earlier_calls_credential_shows_no_start_of_it(
    schema: GraphQLSchema, how: str, limit: int
) -> None:
    client, _ = build(
        schema,
        how,
        envelope({"user": {"id": ECHO}}, (failure(ECHO, code="LEAK"),)),
        max_diagnostic_bytes=limit,
    )
    send_first(client, how)

    response = client.query(
        "user", id="u2", fields=["id"], raw=True, raise_on_error=False
    )

    _, call = client.recorder.calls
    texts = [
        call.data,
        *call.errors,
        *(info._summary or "" for info in response.errors),
    ]
    for text in texts:
        assert not started(text or ""), (how, limit, text)


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("limit", LIMITS)
def test_a_cut_through_an_earlier_calls_credential_shows_no_start_of_a_failure(
    schema: GraphQLSchema, how: str, limit: int
) -> None:
    client, _ = build(schema, how, RuntimeError(ECHO), max_diagnostic_bytes=limit)
    send_first(client, how)

    with contextlib.suppress(Exception):
        client.query("user", id="u2", fields=["id"])

    _, call = client.recorder.calls
    assert call.outcome == "failed"
    assert not started(call.failure), (how, limit, call.failure)


def test_the_credential_stays_scrubbed_after_the_recorder_is_cleared(
    schema: GraphQLSchema,
) -> None:
    client, _ = build(
        schema,
        "per-call header",
        envelope(None, (failure(ECHO, code="LEAK"),)),
        max_diagnostic_bytes=21,
    )
    send_first(client, "per-call header")
    client.recorder.clear()

    client.query("user", id="u2", fields=["id"], raw=True, raise_on_error=False)

    (call,) = client.recorder.calls
    assert not started(" ".join(call.errors))


def test_the_scrub_marks_the_whole_credential_when_nothing_cuts(
    schema: GraphQLSchema,
) -> None:
    client, _ = build(schema, "per-call header", envelope(None, (failure(ECHO),)))
    send_first(client, "per-call header")

    client.query("user", id="u2", fields=["id"], raw=True, raise_on_error=False)

    _, call = client.recorder.calls
    assert SECRET not in " ".join(call.errors)
    assert "redacted" in " ".join(call.errors)


@pytest.mark.parametrize("limit", LIMITS)
def test_a_cut_in_the_http_transports_error_shows_no_start_of_an_earlier_credential(
    schema: GraphQLSchema, limit: int
) -> None:
    # The transport cuts the body it quotes, so it needs the same secret set.
    seen = []

    def respond(body: bytes, headers: dict[str, str]) -> PlannedResponse:  # noqa: ARG001
        seen.append(body)
        if len(seen) == 1:
            return PlannedResponse(
                200,
                (("Content-Type", "application/json"),),
                b'{"data": {"user": {"id": "u1"}}}',
            )
        return PlannedResponse(
            502, (("Content-Type", "text/html"),), ECHO.encode("utf-8")
        )

    with local_server(respond) as url:
        client = GraphQLClient(
            transport=HttpxTransport(),
            schema=schema,
            config=ClientConfig(url=url, max_diagnostic_bytes=limit),
            owns_transport=True,
        )
        with client:
            client.query(
                "user",
                id="u1",
                fields=["id"],
                headers={"Authorization": f"Bearer {SECRET}"},
            )
            with pytest.raises(GraphQLTransportError) as caught:
                client.query("user", id="u2", fields=["id"])
            (_, call) = client.recorder.calls

    assert not started(str(caught.value)), (limit, str(caught.value))
    assert not started(call.failure), (limit, call.failure)


def test_a_call_with_nothing_held_adds_nothing(schema: GraphQLSchema) -> None:
    client, _ = build(schema, "none", envelope({"user": None}))

    client.query("user", id="u1", fields=["id"])

    assert client.recorder._held() == ()


def test_the_recorder_never_shows_a_held_credential(schema: GraphQLSchema) -> None:
    client, _ = build(schema, "per-call header", envelope({"user": None}))
    send_first(client, "per-call header")

    shown = " ".join(
        [
            repr(client.recorder),
            client.recorder.dump(),
            repr(client.recorder._ledger),
            *(repr(call) for call in client.recorder.calls),
        ]
    )

    assert SECRET not in shown


def test_a_clone_shares_what_the_client_holds(schema: GraphQLSchema) -> None:
    client, _ = build(
        schema,
        "per-call header",
        envelope(None, (failure(ECHO, code="LEAK"),)),
        max_diagnostic_bytes=21,
    )
    send_first(client, "per-call header")

    client.anonymous().query(
        "user", id="u2", fields=["id"], raw=True, raise_on_error=False
    )

    _, call = client.recorder.calls
    assert not started(" ".join(call.errors))


# -- the request's own list --------------------------------------------------------


def request_with(**fields: object) -> RequestInfo:
    base: dict[str, object] = {
        "operation": None,
        "kind": "query",
        "document": "{ x }",
        "variables": {},
        "headers": {},
        "url": "http://example.test/graphql",
    }
    base.update(fields)
    return RequestInfo(**base)  # type: ignore[arg-type]


def values(request: RequestInfo) -> set[str]:
    return {value for _, value in request_credentials(request)}


def test_a_redacted_header_is_a_credential_of_the_request() -> None:
    request = request_with(headers={"Authorization": f"Bearer {SECRET}"})

    assert SECRET in values(request)


def test_a_header_outside_the_rule_is_not_a_credential() -> None:
    request = request_with(headers={"Accept": "application/json-secret-value"})

    assert values(request) == set()


def test_a_redacted_variable_is_a_credential_of_the_request() -> None:
    request = request_with(
        variables={"password": SECRET}, redact_variables=("password",)
    )

    assert SECRET in values(request)


def test_the_userinfo_and_query_of_the_url_are_credentials() -> None:
    request = request_with(url=f"http://agent:{SECRET}@example.test/g?key={SECRET}x")

    assert {SECRET, f"{SECRET}x"} <= values(request)


def test_a_transport_credential_is_a_credential_of_the_request() -> None:
    request = request_with(transport_credentials=(("cookie", SECRET),))

    assert SECRET in values(request)


def test_a_short_value_is_not_a_credential() -> None:
    request = request_with(headers={"Authorization": "short"})

    assert values(request) == set()


def test_what_the_client_holds_does_not_grow_with_calls_that_repeat_a_value(
    schema: GraphQLSchema,
) -> None:
    client, _ = build(schema, "none", envelope({"user": None}))
    sizes = []

    for _ in range(6):
        client.query(
            "user",
            id="u1",
            fields=["id"],
            headers={"Authorization": "Bearer a%20b-secret-value"},
        )
        sizes.append(len(client.recorder._ledger))

    assert sizes[-1] == sizes[1]


# -- the bound ------------------------------------------------------------------------


def test_a_ledger_holds_a_value_once() -> None:
    ledger = CredentialLedger()

    ledger.add([("a", SECRET), ("b", SECRET)])
    ledger.add([("c", SECRET)])

    assert len(ledger) == 1


def test_a_ledger_drops_the_oldest_value_past_its_count() -> None:
    ledger = CredentialLedger(max_count=3)

    ledger.add([(f"l{i}", f"value-number-{i}-0123456789") for i in range(5)])

    assert [v for _, v in ledger.pairs()] == [
        f"value-number-{i}-0123456789" for i in (2, 3, 4)
    ]


def test_a_value_seen_again_is_kept_over_an_older_one() -> None:
    ledger = CredentialLedger(max_count=2)

    ledger.add([("a", "first-value-0123456789"), ("b", "second-value-0123456789")])
    ledger.add([("a", "first-value-0123456789")])
    ledger.add([("c", "third-value-0123456789")])

    assert [v for _, v in ledger.pairs()] == [
        "first-value-0123456789",
        "third-value-0123456789",
    ]


def test_a_ledger_drops_the_oldest_values_past_its_size() -> None:
    ledger = CredentialLedger(max_chars=40)

    ledger.add([("a", "a" * 20), ("b", "b" * 20), ("c", "c" * 20)])

    assert [v for _, v in ledger.pairs()] == ["b" * 20, "c" * 20]


def test_one_value_over_the_size_is_still_held_alone() -> None:
    ledger = CredentialLedger(max_chars=10)

    ledger.add([("a", "a" * 5), ("b", "b" * 30)])

    assert [v for _, v in ledger.pairs()] == ["b" * 30]


def test_the_recorder_makes_a_ledger_of_its_own() -> None:
    assert DiagnosticsRecorder()._ledger is not DiagnosticsRecorder()._ledger


def test_a_ledger_has_no_text_form_of_what_it_holds() -> None:
    ledger = CredentialLedger()
    ledger.add([("a", SECRET)])

    assert SECRET not in repr(ledger)
    assert SECRET not in str(ledger)


def test_calls_on_several_threads_can_add_and_read_together() -> None:
    ledger = CredentialLedger(max_count=50)
    failures: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            for i in range(300):
                ledger.add([("l", f"value-{worker}-{i}-0123456789")])
                ledger.pairs()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(ledger) == 50


# -- a ledger that dropped a value cannot vouch for the text ---------------------
#
# Past its bound the ledger forgets a credential that a server can still repeat.
# Text cut after that could show the start of it, and uncut text could show all of
# it. Nothing can scrub a value that is not known, so the text of a server, or of
# an exception, is withheld from then on rather than shown.


def filler(count: int, start: int = 0) -> list[tuple[str, str]]:
    return [
        ("filler", f"filler-token-{start + i:05d}-0123456789") for i in range(count)
    ]


def test_a_ledger_that_dropped_nothing_is_complete() -> None:
    ledger = CredentialLedger(max_count=3)

    ledger.add(filler(3))
    ledger.add(filler(1, start=2))

    assert not ledger.incomplete


def test_a_ledger_is_incomplete_once_it_drops_a_value_past_its_count() -> None:
    ledger = CredentialLedger(max_count=3)

    ledger.add(filler(4))

    assert ledger.incomplete


def test_a_ledger_is_incomplete_once_it_drops_a_value_past_its_size() -> None:
    ledger = CredentialLedger(max_chars=40)

    ledger.add([("a", "a" * 20), ("b", "b" * 20), ("c", "c" * 20)])

    assert ledger.incomplete


def test_one_value_over_the_size_alone_drops_nothing() -> None:
    ledger = CredentialLedger(max_chars=10)

    ledger.add([("a", "a" * 30)])

    assert not ledger.incomplete


def test_a_ledger_stays_incomplete() -> None:
    ledger = CredentialLedger(max_count=2)
    ledger.add(filler(3))

    ledger.add([])
    ledger.add(filler(1, start=2))

    assert ledger.incomplete


def test_the_gap_is_never_held_as_a_credential() -> None:
    ledger = CredentialLedger()

    ledger.add([HISTORY_GAP])

    assert len(ledger) == 0
    assert not ledger.incomplete


def test_the_recorder_lists_the_gap_only_once_its_ledger_dropped_a_value() -> None:
    recorder = DiagnosticsRecorder()
    recorder._hold(filler(2))
    assert HISTORY_GAP not in recorder._held()

    recorder._ledger = CredentialLedger(max_count=1)
    recorder._hold(filler(2))

    assert HISTORY_GAP in recorder._held()


def test_the_gap_is_not_a_credential_of_a_request() -> None:
    request = RequestInfo(
        operation="q",
        kind="query",
        document="query q { a }",
        variables={},
        headers={},
        url="http://example.test/graphql",
        transport_credentials=(HISTORY_GAP, ("cookie", SECRET)),
    )

    assert request_credentials(request) == (("cookie", SECRET),)
    assert has_history_gap(request)
    assert SECRET not in repr(request.redacted())


def gapped(**fields) -> RequestInfo:
    return RequestInfo(
        operation="q",
        kind="query",
        document="query q { a }",
        variables={},
        headers={},
        url="http://example.test/graphql",
        transport_credentials=(HISTORY_GAP,),
        **fields,
    )


def test_the_text_of_a_request_that_has_a_gap_is_withheld() -> None:
    request = gapped()

    assert sanitize_text(request, ECHO) == HISTORY_WITHHELD
    assert safe_excerpt(request, ECHO) == HISTORY_WITHHELD
    assert request.scrub(ECHO) == HISTORY_WITHHELD
    assert SECRET not in safe_excerpt(request, ECHO)


def test_a_callback_text_is_withheld_only_when_the_request_has_a_gap() -> None:
    assert withhold_if_gapped(gapped(), ECHO) == HISTORY_WITHHELD
    assert withhold_if_gapped(gapped(), "") == ""
    whole = dataclasses.replace(gapped(), transport_credentials=())
    assert withhold_if_gapped(whole, ECHO) == ECHO


def test_a_withheld_text_still_obeys_the_cap() -> None:
    request = gapped(max_diagnostic_bytes=5)

    assert safe_excerpt(request, ECHO).startswith(HISTORY_WITHHELD[:5])
    assert len(safe_excerpt(request, ECHO)) < len(ECHO)


def test_an_empty_text_stays_empty_when_there_is_a_gap() -> None:
    assert sanitize_text(gapped(), "") == ""


def test_a_request_that_opted_out_of_redaction_shows_its_text() -> None:
    request = gapped(redact_values=False)

    assert sanitize_text(request, ECHO) == ECHO


def test_a_request_that_opted_out_of_redaction_withholds_nothing_at_any_site() -> None:
    request = gapped(redact_values=False)

    assert not has_history_gap(request)
    assert request.scrub(ECHO) == ECHO
    assert safe_excerpt(request, ECHO) == ECHO
    assert withhold_if_gapped(request, ECHO) == ECHO
    assert render_data_excerpt(request, {"a": ECHO}).text != HISTORY_WITHHELD


def test_the_data_excerpt_of_a_request_that_has_a_gap_is_withheld() -> None:
    excerpt = render_data_excerpt(gapped(), {"a": SECRET, "b": {"c": SECRET}, "d": 1})

    assert excerpt.text == HISTORY_WITHHELD
    assert excerpt.cut
    assert excerpt.fields == 4


@pytest.mark.parametrize("limit", [0, 3, 21, 57, 4096])
@pytest.mark.parametrize("how", HOWS)
def test_text_after_an_eviction_shows_no_part_of_an_evicted_credential(
    schema: GraphQLSchema, how: str, limit: int
) -> None:
    others = [envelope({"user": {"id": "u1"}})] * 3
    client, _ = make_client(
        schema,
        first_response(how),
        *others,
        envelope({"user": {"id": ECHO}}, (failure(ECHO, code="LEAK"),)),
        RuntimeError(ECHO),
        max_diagnostic_bytes=limit,
        **({"redact_variables": ("id",)} if how == "variable" else {}),
    )
    client.recorder._ledger = CredentialLedger(max_count=2)
    send_first(client, how)
    for index in range(3):
        client.query(
            "user",
            id="u1",
            fields=["id"],
            headers={"Authorization": f"Bearer other-token-{index}-0123456789"},
        )

    response = client.query(
        "user", id="u2", fields=["id"], raw=True, raise_on_error=False
    )
    with contextlib.suppress(Exception):
        client.query("user", id="u3", fields=["id"])

    echoed, failed = client.recorder.calls[-2:]
    texts = [
        echoed.data or "",
        *echoed.errors,
        *(info._summary or "" for info in response.errors),
        failed.failure,
        repr(response),
    ]
    for text in texts:
        assert SECRET not in text, (how, limit, text)
        assert not started(text), (how, limit, text)
    assert HISTORY_WITHHELD[: min(limit, 10)] in " ".join(texts) or limit == 0


def test_text_is_shown_while_the_ledger_has_dropped_nothing(
    schema: GraphQLSchema,
) -> None:
    client, _ = build(
        schema, "per-call header", envelope(None, (failure("plain words"),))
    )
    send_first(client, "per-call header")

    client.query("user", id="u2", fields=["id"], raw=True, raise_on_error=False)

    _, call = client.recorder.calls
    assert "plain words" in " ".join(call.errors)
    assert HISTORY_WITHHELD not in " ".join(call.errors)


@pytest.mark.parametrize("limit", [0, 21, 57, 4096])
def test_the_http_transport_withholds_its_text_after_an_eviction(
    schema: GraphQLSchema, limit: int
) -> None:
    seen = []

    def respond(body: bytes, headers: dict[str, str]) -> PlannedResponse:  # noqa: ARG001
        seen.append(body)
        if len(seen) == 1:
            return PlannedResponse(
                200,
                (("Content-Type", "application/json"),),
                b'{"data": {"user": {"id": "u1"}}}',
            )
        return PlannedResponse(
            502, (("Content-Type", "text/html"),), ECHO.encode("utf-8")
        )

    recorder = DiagnosticsRecorder()
    recorder._ledger = CredentialLedger(max_count=2)
    with local_server(respond) as url:
        client = GraphQLClient(
            transport=HttpxTransport(),
            schema=schema,
            config=ClientConfig(url=url, max_diagnostic_bytes=limit),
            owns_transport=True,
            recorder=recorder,
        )
        with client:
            client.query(
                "user",
                id="u1",
                fields=["id"],
                headers={"Authorization": f"Bearer {SECRET}"},
            )
            recorder._hold(filler(3))
            with pytest.raises(GraphQLTransportError) as caught:
                client.query("user", id="u2", fields=["id"])
            (_, call) = client.recorder.calls

    for text in (str(caught.value), call.failure):
        assert SECRET not in text, (limit, text)
        assert not started(text), (limit, text)


def test_a_client_with_the_default_ledger_withholds_text_past_its_count(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(
        schema,
        envelope({"user": {"id": "u1"}}),
        envelope(None, (failure(ECHO),)),
        max_diagnostic_bytes=30,
        max_recorded_calls=1,
    )
    client.query(
        "user", id="u1", fields=["id"], headers={"Authorization": f"Bearer {SECRET}"}
    )
    client.recorder._hold(filler(MAX_HELD_CREDENTIALS + 1))

    client.query("user", id="u2", fields=["id"], raw=True, raise_on_error=False)

    (call,) = client.recorder.calls
    assert not started(" ".join(call.errors)), call.errors
    assert HISTORY_WITHHELD[:10] in " ".join(call.errors)
