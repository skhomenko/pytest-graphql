"""What a client records about a call beyond its snapshot (DESIGN section 7).

A failure report needs the data a call returned, the error lines, and a
reproducing ``curl`` command. ``as_curl()`` needs a live request, and a response
keeps only a digest-only snapshot, so all three are built where the request is
still in hand and recorded as finished text. These tests pin what is recorded,
its bounds, and that none of it holds a secret.
"""

from __future__ import annotations

from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import client as client_module
from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import WITHHELD_TEXT, RecordedCall, RequestInfo
from pytest_graphql._core.errors import DiagnosticRenderError, GraphQLExecutionError
from tests.schema.resolvers import build_schema
from tests.unit.scripted_transport import (
    UPDATE_MUTATION,
    USER_QUERY,
    ScriptedTransport,
    envelope,
    user_data,
)

URL = "https://example.test/graphql"
SECRET = "rec-secret-0123456789"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


def client_over(
    schema: GraphQLSchema, transport: ScriptedTransport, **config: Any
) -> GraphQLClient:
    settings: dict[str, Any] = {
        "url": URL,
        "headers": {"Authorization": f"Bearer {SECRET}"},
        **config,
    }
    return GraphQLClient(
        transport=transport, schema=schema, config=ClientConfig(**settings)
    )


def last_call(client: GraphQLClient) -> RecordedCall:
    return client.recorder.calls[-1]


def test_a_new_call_has_no_text_fields_by_default() -> None:
    snapshot = RequestInfo(
        operation=None,
        kind="query",
        document="{ x }",
        variables={},
        headers={},
        url=URL,
    ).redacted()
    call = RecordedCall(request=snapshot, outcome="ok")
    assert call.errors == ()
    assert call.data is None
    assert (call.data_fields, call.data_cut, call.curl) == (0, False, "")


def test_a_successful_call_records_its_data_and_a_curl_line(
    schema: GraphQLSchema,
) -> None:
    client = client_over(schema, ScriptedTransport(envelope(user_data())))
    client.execute(USER_QUERY, {"id": "123"})
    call = last_call(client)
    assert call.data == '{"user": {"id": "123", "name": "John"}}'
    assert call.data_fields == 3
    assert call.data_cut is False
    assert call.errors == ()
    assert call.curl.startswith("curl ")
    assert "PYTEST_GQL_HEADER_AUTHORIZATION" in call.curl


def test_data_is_cut_to_the_diagnostic_limit_and_counts_every_field(
    schema: GraphQLSchema,
) -> None:
    client = client_over(
        schema,
        ScriptedTransport(envelope(user_data(name="n" * 600))),
        max_diagnostic_bytes=120,
    )
    client.execute(USER_QUERY, {"id": "123"})
    call = last_call(client)
    assert call.data is not None
    assert len(call.data.encode("utf-8")) <= 120
    assert call.data_cut is True
    assert call.data_fields == 3


def test_null_data_is_recorded_as_null(schema: GraphQLSchema) -> None:
    client = client_over(
        schema,
        ScriptedTransport(
            envelope(None, ({"message": "no", "extensions": {"code": "NOPE"}},))
        ),
    )
    with pytest.raises(GraphQLExecutionError):
        client.execute(USER_QUERY, {"id": "1"})
    call = last_call(client)
    assert call.outcome == "errors"
    assert call.data == "null"
    assert call.data_fields == 0


def test_error_lines_are_the_scrubbed_summaries_in_response_order(
    schema: GraphQLSchema,
) -> None:
    client = client_over(
        schema,
        ScriptedTransport(
            envelope(
                None,
                (
                    {
                        "message": "name already taken",
                        "path": ["updateUser"],
                        "extensions": {"code": "CONFLICT"},
                    },
                    {"message": "second"},
                ),
            )
        ),
    )
    with pytest.raises(GraphQLExecutionError):
        client.execute(UPDATE_MUTATION, {"id": "123", "name": "New"})
    call = last_call(client)
    assert call.errors == (
        'CONFLICT at ["updateUser"]: name already taken',
        "(no code): second",
    )
    assert call.error_count == 2


def test_error_lines_follow_max_recorded_errors_and_the_count_stays_exact(
    schema: GraphQLSchema,
) -> None:
    errors = tuple({"message": f"e{i}"} for i in range(7))
    client = client_over(
        schema,
        ScriptedTransport(envelope(None, errors)),
        max_recorded_errors=3,
    )
    with pytest.raises(GraphQLExecutionError):
        client.execute(USER_QUERY, {"id": "1"})
    call = last_call(client)
    assert call.errors == ("(no code): e0", "(no code): e1", "(no code): e2")
    assert call.error_count == 7


def test_a_failed_call_records_curl_and_no_data(schema: GraphQLSchema) -> None:
    client = client_over(schema, ScriptedTransport(ConnectionError("refused")))
    with pytest.raises(ConnectionError):
        client.execute(USER_QUERY, {"id": "1"})
    call = last_call(client)
    assert call.outcome == "failed"
    assert call.data is None
    assert call.errors == ()
    assert call.curl.startswith("curl ")


def test_no_recorded_text_shows_a_secret(schema: GraphQLSchema) -> None:
    errors = ({"message": f"bad token {SECRET}", "extensions": {"code": "AUTH"}},)
    client = client_over(
        schema,
        ScriptedTransport(
            envelope(user_data(name=f"echo Bearer {SECRET}"), errors),
        ),
    )
    with pytest.raises(GraphQLExecutionError):
        client.execute(USER_QUERY, {"id": "1"}, raise_on_partial=True)
    call = last_call(client)
    for text in (call.data, *call.errors, call.curl, repr(call)):
        assert text is not None
        assert SECRET not in text


def test_a_redacted_response_path_shows_no_value(schema: GraphQLSchema) -> None:
    client = client_over(
        schema,
        ScriptedTransport(envelope(user_data(name="p4ss-w0rd-value"))),
        redact_variables=("name",),
    )
    client.execute(USER_QUERY, {"id": "123"})
    call = last_call(client)
    assert call.data is not None
    assert "p4ss-w0rd-value" not in call.data
    assert "[redacted" in call.data


def test_a_curl_that_cannot_render_is_withheld_and_the_call_still_returns(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Any) -> str:  # noqa: ARG001
        raise DiagnosticRenderError("as_curl()", "refused")

    monkeypatch.setattr(client_module.RequestInfo, "as_curl", refuse)
    client = client_over(schema, ScriptedTransport(envelope(user_data())))
    client.execute(USER_QUERY, {"id": "123"})
    assert last_call(client).curl == WITHHELD_TEXT


def test_data_that_cannot_render_is_withheld_and_the_call_still_returns(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
        raise DiagnosticRenderError("as_curl()", "refused")

    monkeypatch.setattr(client_module, "render_data_excerpt", refuse)
    client = client_over(schema, ScriptedTransport(envelope(user_data())))
    client.execute(USER_QUERY, {"id": "123"})
    assert last_call(client).data == WITHHELD_TEXT


def test_the_dump_is_unchanged_by_the_new_fields(schema: GraphQLSchema) -> None:
    client = client_over(schema, ScriptedTransport(envelope(user_data())))
    client.execute(USER_QUERY, {"id": "123"})
    dump = client.recorder.dump()
    assert dump.splitlines()[0].startswith("1. query user -> ok (status 200,")
    assert "curl" not in dump
