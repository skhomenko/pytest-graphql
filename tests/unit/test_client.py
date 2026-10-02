"""The client surface: call flow, identity, ownership and the raising table.

SPEC 11.2's "in-process" layer. Every test here runs a real client against
``FakeGraphQLTransport``, which executes the hostile schema with
``graphql_sync`` and opens no socket.

The lifecycle report has its own file, because the reporting product is large
enough that mixing it in here would hide both.
"""

from __future__ import annotations

import base64
import math
import os
import ssl
from fractions import Fraction
from typing import Any

import httpx
import pytest
from graphql import GraphQLSchema
from graphql import build_schema as build_graphql_schema

from pytest_graphql._core.auth import BearerAuth, HeaderAuth
from pytest_graphql._core.client import (
    ClientConfig,
    GraphQLClient,
    _new_root_pool,
    build_client,
)
from pytest_graphql._core.diagnostics import MAX_OMISSION_RECORDS, RequestInfo
from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLExecutionError,
    GraphQLPartialDataError,
    GraphQLTransportError,
)
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.response import GraphQLResponse
from pytest_graphql._core.transport.base import RawResponse
from tests.schema.fake_transport import FakeGraphQLTransport, FakeTransport
from tests.schema.resolvers import build_schema
from tests.unit.local_http_server import PlannedResponse, local_server


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


@pytest.fixture
def transport(schema: GraphQLSchema) -> FakeGraphQLTransport:
    return FakeGraphQLTransport(schema)


@pytest.fixture
def client(schema: GraphQLSchema, transport: FakeGraphQLTransport) -> GraphQLClient:
    return GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql"),
    )


# -- the call flow (SPEC 5.2) -------------------------------------------------


def test_query_returns_the_unwrapped_top_level_field(client: GraphQLClient) -> None:
    user = client.query("user", id="u1")

    assert user["id"] == "u1"
    assert user.__typename__ == "User"


def test_query_raw_returns_the_whole_response(client: GraphQLClient) -> None:
    response = client.query("user", id="u1", raw=True)

    assert isinstance(response, GraphQLResponse)
    assert response.has_data
    assert response.http.status_code == 200


def test_the_request_carries_the_operation_and_its_variables(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    client.query("user", id="u1")

    sent = transport.sent[0]
    assert sent.kind == "query"
    assert sent.operation == "user"
    assert sent.variables["id"] == "u1"
    assert "query" in sent.document


def test_explicit_fields_select_only_what_was_asked(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    user = client.query("user", id="u1", fields=["id", "name"])

    assert set(dict(user)) == {"id", "name"}
    assert "settings" not in transport.sent[0].document


def test_a_mutation_is_sent_as_a_mutation(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    client.mutation("updateUser", id="u1", name="new", fields=["id", "name"])

    assert transport.sent[0].kind == "mutation"


def test_idempotent_is_a_per_call_option_the_request_carries(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    client.mutation("updateUser", id="u1", fields=["id"], idempotent=True)

    assert transport.sent[0].idempotent is True


def test_an_option_name_is_never_sent_as_a_variable(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    client.query("user", id="u1", fields=["id"], timeout=5.0, raw=True)

    assert set(transport.sent[0].variables) == {"id"}


def test_execute_returns_the_response_itself(client: GraphQLClient) -> None:
    response = client.execute(
        "query Named($id: ID!) { user(id: $id) { id name } }",
        {"id": "u1"},
        operation_name="Named",
    )

    assert isinstance(response, GraphQLResponse)
    assert response.unwrap()["id"] == "u1"


# -- header precedence (C4) ---------------------------------------------------


def _headers_of(transport: FakeGraphQLTransport) -> dict[str, str]:
    return {name.lower(): value for name, value in transport.sent[-1].headers.items()}


def test_config_headers_are_the_lowest_layer(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(headers={"X-Trace": "base"}),
    )
    client.query("user", id="u1", fields=["id"])

    assert _headers_of(transport)["x-trace"] == "base"


def test_auth_replaces_a_config_header_and_a_call_header_replaces_auth(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(headers={"authorization": "from-config"}),
        auth=BearerAuth("from-auth"),
    )

    client.query("user", id="u1", fields=["id"])
    assert _headers_of(transport)["authorization"] == "Bearer from-auth"

    client.query("user", id="u1", fields=["id"], headers={"Authorization": "from-call"})
    assert _headers_of(transport)["authorization"] == "from-call"


def test_clone_headers_sit_above_auth_and_below_a_call_header(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport, schema=schema, auth=BearerAuth("tok")
    ).with_headers({"Authorization": "from-clone"})

    client.query("user", id="u1", fields=["id"])
    assert _headers_of(transport)["authorization"] == "from-clone"

    client.query("user", id="u1", fields=["id"], headers={"authorization": "from-call"})
    assert _headers_of(transport)["authorization"] == "from-call"


def test_a_later_source_replaces_a_name_rather_than_adding_a_second_line(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(headers={"X-Api-Key": "one"}),
    ).with_headers({"x-api-key": "two"})

    client.query("user", id="u1", fields=["id"])

    names = [name.lower() for name in transport.sent[-1].headers]
    assert names.count("x-api-key") == 1
    assert _headers_of(transport)["x-api-key"] == "two"


def test_with_headers_accepts_a_positional_mapping_and_keywords(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(transport=transport, schema=schema).with_headers(
        {"X-Api-Key": "v"}, Authorization="Bearer x"
    )

    client.query("user", id="u1", fields=["id"])

    headers = _headers_of(transport)
    assert headers["x-api-key"] == "v"
    assert headers["authorization"] == "Bearer x"


def test_header_auth_takes_a_positional_mapping_too(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport, schema=schema, auth=HeaderAuth({"X-Api-Key": "v"})
    )

    client.query("user", id="u1", fields=["id"])

    assert _headers_of(transport)["x-api-key"] == "v"


# -- identity and cloning (B2, C17, C19) --------------------------------------


def test_a_clone_over_a_plain_transport_shares_it_and_owns_nothing(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    clone = client.as_("token")

    assert clone.transport is transport
    assert clone.owns_transport is False

    clone.close()
    assert transport.close_calls == 0


def test_as_wraps_a_bare_string_in_a_bearer_token(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    GraphQLClient(transport=transport, schema=schema).as_("tok").query(
        "user", id="u1", fields=["id"]
    )

    assert _headers_of(transport)["authorization"] == "Bearer tok"


def test_anonymous_drops_the_auth_identity(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(
        transport=transport, schema=schema, auth=BearerAuth("tok")
    ).anonymous()

    client.query("user", id="u1", fields=["id"])

    assert "authorization" not in _headers_of(transport)


def test_a_clone_keeps_the_schema_config_and_middleware(
    client: GraphQLClient,
) -> None:
    clone = client.with_headers({"X-A": "1"})

    assert clone.schema is client.schema
    assert clone.config is client.config


# -- ownership and close counts (9.1, C19, C28) -------------------------------


def test_a_client_on_an_injected_transport_closes_nothing_the_caller_owns(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    assert client.owns_transport is False

    client.close()

    assert transport.close_calls == 0


def test_the_flag_builds_the_list_when_no_list_is_supplied(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(transport=transport, schema=schema, owns_transport=True)

    client.close()

    assert transport.close_calls == 1


def test_closing_twice_adds_no_further_calls(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(transport=transport, schema=schema, owns_transport=True)

    client.close()
    client.close()

    assert transport.close_calls == 1


def test_leaving_the_context_manager_twice_is_idempotent(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = GraphQLClient(transport=transport, schema=schema, owns_transport=True)

    with client:
        pass
    with client:
        pass

    assert transport.close_calls == 1


def test_a_supplied_cleanup_list_closes_the_transport_exactly_once(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    # The internal form: the list is already complete, and the constructor
    # never appends to it. A constructor that appended would close twice.
    cleanup: list[Any] = [transport]
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        owns_transport=True,
        cleanup=cleanup,
    )

    client.close()

    assert transport.close_calls == 1
    assert len(cleanup) == 1


def test_owns_transport_reads_true_on_exactly_the_paths_that_close_it(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    owning = GraphQLClient(transport=transport, schema=schema, owns_transport=True)
    borrowing = GraphQLClient(transport=transport, schema=schema)

    assert owning.owns_transport is True
    assert borrowing.owns_transport is False

    borrowing.close()
    assert transport.close_calls == 0
    owning.close()
    assert transport.close_calls == 1


def test_a_two_method_transport_constructs_clones_and_closes(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    # C19/C23: a plain `send()`/`close()` transport needs no extra method,
    # and an unrelated `derive` member is never looked up.
    client = GraphQLClient(transport=transport, schema=schema, owns_transport=True)
    clone = client.with_headers({"X-A": "1"})

    clone.query("user", id="u1", fields=["id"])
    clone.close()
    client.close()

    assert transport.close_calls == 1


def test_an_unrelated_derive_member_is_never_called(
    schema: GraphQLSchema,
) -> None:
    calls: list[str] = []

    class WithUnrelatedDerive(FakeGraphQLTransport):
        def derive(
            self,
            schema: GraphQLSchema,  # noqa: ARG002 -- an unrelated member's own shape
        ) -> None:
            calls.append("derive")

    unrelated = WithUnrelatedDerive(schema)
    client = GraphQLClient(transport=unrelated, schema=schema)

    clone = client.with_headers({"X-A": "1"})
    clone.close()
    client.close()

    assert calls == []
    assert clone.transport is unrelated
    assert clone.owns_transport is False
    assert unrelated.close_calls == 0


# -- the factory (part 5 of 2.14) ---------------------------------------------


def test_build_client_on_an_injected_transport_closes_nothing(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = build_client(
        url="https://example.test/graphql", transport=transport, schema=schema
    )

    assert client.owns_transport is False
    client.close()
    assert transport.close_calls == 0


def test_build_client_loads_the_schema_over_the_supplied_transport(
    schema: GraphQLSchema,
) -> None:
    client = build_client(
        url="https://example.test/graphql",
        transport=FakeGraphQLTransport(schema),
    )

    assert client.schema.query_type is not None
    assert "user" in client.schema.query_type.fields


def test_build_client_records_the_url_on_the_configuration(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = build_client(
        url="https://example.test/graphql", transport=transport, schema=schema
    )

    assert client.config.url == "https://example.test/graphql"


# -- the raising table (B16, as C5 refined it) --------------------------------


class _EnvelopeTransport:
    """Returns one fixed envelope, so the raising table can be driven directly."""

    def __init__(self, raw: RawResponse) -> None:
        self._raw = raw
        self.close_calls = 0

    def send(
        self,
        request: RequestInfo,  # noqa: ARG002 -- part of the Transport contract
        *,
        timeout: float,  # noqa: ARG002 -- part of the Transport contract
    ) -> RawResponse:
        return self._raw

    def close(self) -> None:
        self.close_calls += 1


def _envelope(data: Any, errors: tuple[dict[str, Any], ...] = ()) -> RawResponse:
    return RawResponse(
        status_code=200,
        media_type="application/graphql-response+json",
        data=data,
        errors=errors,
        extensions=None,
        headers={},
    )


def _client_for(
    schema: GraphQLSchema, raw: RawResponse, **config: Any
) -> GraphQLClient:
    return GraphQLClient(
        transport=_EnvelopeTransport(raw),
        schema=schema,
        config=ClientConfig(**config),
    )


def test_errors_with_no_data_raise_an_execution_error(schema: GraphQLSchema) -> None:
    client = _client_for(schema, _envelope(None, ({"message": "boom"},)))

    with pytest.raises(GraphQLExecutionError) as caught:
        client.query("user", id="u1", fields=["id"])

    assert not isinstance(caught.value, GraphQLPartialDataError)


def test_an_exception_repr_does_not_escape_its_message_again(
    schema: GraphQLSchema,
) -> None:
    # The message quotes the response's ``repr``, which already escaped the
    # variable's one backslash into two. The default exception ``repr``
    # would escape the message again, into the four the header holds.
    secret = "q\\\\\\\\w-0123456789"
    client = _client_for(
        schema,
        _envelope(None, ({"message": "boom"},)),
        headers={"X-API-Key": secret},
    )

    with pytest.raises(GraphQLExecutionError) as caught:
        client.query("user", id="q\\w-0123456789", fields=["id"])

    assert secret.count("\\") == 4
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_errors_with_data_raise_a_partial_data_error(schema: GraphQLSchema) -> None:
    client = _client_for(
        schema, _envelope({"user": {"id": "u1"}}, ({"message": "boom"},))
    )

    with pytest.raises(GraphQLPartialDataError):
        client.query("user", id="u1", fields=["id"])


def test_raise_on_partial_false_returns_the_data(schema: GraphQLSchema) -> None:
    client = _client_for(
        schema,
        _envelope({"user": {"id": "u1"}}, ({"message": "boom"},)),
        raise_on_partial=False,
    )

    assert client.query("user", id="u1", fields=["id"])["id"] == "u1"


def test_raise_on_error_false_disables_partial_raising_too(
    schema: GraphQLSchema,
) -> None:
    client = _client_for(
        schema,
        _envelope({"user": {"id": "u1"}}, ({"message": "boom"},)),
        raise_on_error=False,
        raise_on_partial=True,
    )

    assert client.query("user", id="u1", fields=["id"])["id"] == "u1"


def test_null_data_with_no_errors_is_a_protocol_violation(
    schema: GraphQLSchema,
) -> None:
    client = _client_for(schema, _envelope(None))

    with pytest.raises(GraphQLExecutionError, match="protocol violation"):
        client.query("user", id="u1", fields=["id"])


def test_a_per_call_option_overrides_the_configured_raising(
    schema: GraphQLSchema,
) -> None:
    client = _client_for(
        schema, _envelope({"user": {"id": "u1"}}, ({"message": "boom"},))
    )

    result = client.query("user", id="u1", fields=["id"], raise_on_error=False)

    assert result["id"] == "u1"


# -- middleware (B6, C6) ------------------------------------------------------


def test_before_request_runs_first_to_last_and_after_response_last_to_first(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    order: list[str] = []

    class Recording(BaseMiddleware):
        def __init__(self, label: str) -> None:
            self.label = label

        def before_request(
            self,
            request: RequestInfo,  # noqa: ARG002 -- the overridable contract
        ) -> RequestInfo | None:
            order.append(f"before:{self.label}")
            return None

        def after_response(
            self,
            response: GraphQLResponse[Any],  # noqa: ARG002 -- the same contract
        ) -> GraphQLResponse[Any] | None:
            order.append(f"after:{self.label}")
            return None

    client = GraphQLClient(
        transport=transport,
        schema=schema,
        middleware=(Recording("a"), Recording("b")),
    )
    client.query("user", id="u1", fields=["id"])

    assert order == ["before:a", "before:b", "after:b", "after:a"]


def test_a_returned_request_replaces_the_value_for_the_rest_of_the_chain(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    import dataclasses

    class AddsHeader(BaseMiddleware):
        def before_request(self, request: RequestInfo) -> RequestInfo | None:
            return dataclasses.replace(
                request, headers={**request.headers, "X-Added": "yes"}
            )

    class Observes(BaseMiddleware):
        seen: str | None = None

        def before_request(self, request: RequestInfo) -> RequestInfo | None:
            Observes.seen = request.headers.get("X-Added")
            return None

    client = GraphQLClient(
        transport=transport, schema=schema, middleware=(AddsHeader(), Observes())
    )
    client.query("user", id="u1", fields=["id"])

    assert Observes.seen == "yes"
    assert _headers_of(transport)["x-added"] == "yes"


def test_a_middleware_exception_aborts_the_call_and_propagates_unchanged(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    class Boom(BaseMiddleware):
        def before_request(
            self,
            request: RequestInfo,  # noqa: ARG002 -- the overridable contract
        ) -> RequestInfo | None:
            raise RuntimeError("stop here")

    client = GraphQLClient(transport=transport, schema=schema, middleware=(Boom(),))

    with pytest.raises(RuntimeError, match="stop here"):
        client.query("user", id="u1", fields=["id"])

    assert transport.sent == []


# -- schema identity (B20, C4) ------------------------------------------------


def test_schema_loading_uses_schema_headers_and_not_the_call_identity(
    schema: GraphQLSchema,
) -> None:
    probe = FakeGraphQLTransport(schema)
    client = build_client(
        url="https://example.test/graphql",
        transport=probe,
        config=ClientConfig(
            headers={"X-Role": "caller"}, schema_headers={"X-Role": "schema"}
        ),
        auth=BearerAuth("tok"),
    )

    introspection = probe.sent[0]
    assert introspection.headers["X-Role"] == "schema"
    assert "Authorization" not in introspection.headers

    client.query("user", id="u1", fields=["id"])
    assert probe.sent[-1].headers["X-Role"] == "caller"


def test_schema_headers_default_to_headers(schema: GraphQLSchema) -> None:
    probe = FakeGraphQLTransport(schema)
    build_client(
        url="https://example.test/graphql",
        transport=probe,
        config=ClientConfig(headers={"X-Role": "caller"}),
    )

    assert probe.sent[0].headers["X-Role"] == "caller"


def test_the_query_executor_shape_still_loads_a_schema(
    schema: GraphQLSchema,
) -> None:
    # `FakeTransport` is the `QueryExecutor` shape, which `IntrospectionSource`
    # takes directly. The client's own adapter is the other way in.
    from pytest_graphql._core.schema.source import IntrospectionSource

    loaded = IntrospectionSource(FakeTransport(schema)).load()

    assert loaded.query_type is not None


# -- automatic omissions reaching a report (C58) ------------------------------


@pytest.fixture(scope="module")
def wide_schema() -> GraphQLSchema:
    """A root field whose type omits more fields than the record bound holds.

    Every ``fNN`` takes a required argument, which auto-selection skips, so
    one call produces `MAX_OMISSION_RECORDS` + 10 omissions and the retained
    list is necessarily shorter than the total. ``report`` is nullable and
    has no resolver, so execution succeeds with a null field and these tests
    stay about the omission count alone.
    """
    skipped = "\n".join(
        f"  f{index}(need: String!): String"
        for index in range(MAX_OMISSION_RECORDS + 10)
    )
    return build_graphql_schema(f"""
        type Wide {{
          id: ID
        {skipped}
        }}

        type Query {{ report: Wide }}
    """)


def _wide_client(
    transport: FakeGraphQLTransport, schema: GraphQLSchema
) -> GraphQLClient:
    return GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql"),
    )


def test_the_uncapped_omission_total_reaches_the_request(
    wide_schema: GraphQLSchema,
) -> None:
    transport = FakeGraphQLTransport(wide_schema)

    _wide_client(transport, wide_schema).query("report")

    request = transport.sent[-1]
    assert len(request.omissions) == MAX_OMISSION_RECORDS
    assert request.omissions_total == MAX_OMISSION_RECORDS + 10


def test_a_truncated_omission_list_reports_what_it_dropped(
    wide_schema: GraphQLSchema,
) -> None:
    # The count a report states must survive the bound. Recomputing it from
    # the retained list can only ever say that nothing was dropped.
    transport = FakeGraphQLTransport(wide_schema)

    _wide_client(transport, wide_schema).query("report")
    snapshot = transport.sent[-1].redacted()

    assert len(snapshot.omissions) == MAX_OMISSION_RECORDS
    assert snapshot.omissions_total == MAX_OMISSION_RECORDS + 10
    assert snapshot.omissions_dropped == 10


def test_the_dropped_count_is_visible_in_the_rendered_report(
    wide_schema: GraphQLSchema,
) -> None:
    # The recorder dump is where a reader actually sees the count, so the
    # regression has to reach that text and not only the snapshot field.
    transport = FakeGraphQLTransport(wide_schema)
    client = _wide_client(transport, wide_schema)

    client.query("report")
    dump = client.recorder.dump()

    assert "further omission(s) not shown" in dump
    assert f"of {MAX_OMISSION_RECORDS + 10} total" in dump


def test_a_raw_document_carries_no_omissions(
    client: GraphQLClient, transport: FakeGraphQLTransport
) -> None:
    # `execute()` sends what the caller wrote, so there is no generated
    # selection to omit anything and nothing to under-report.
    client.execute('query { user(id: "u1") { id } }')

    assert transport.sent[-1].omissions == ()
    assert transport.sent[-1].omissions_total == 0


# -- transport configuration (Operational limits) -----------------------------


def test_a_scalar_timeout_is_the_call_ceiling(schema: GraphQLSchema) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql", timeout=5.0),
    )

    client.query("user", id="u1", fields=["id"])

    assert transport.timeouts == [5.0]


def test_a_phase_specific_timeout_clamps_no_phase(schema: GraphQLSchema) -> None:
    # The per-call scalar is a ceiling on each configured phase, so the
    # scalar a configured `Timeout` produces must be the largest phase: any
    # smaller value would silently shorten a phase the project set.
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(
            url="https://example.test/graphql",
            timeout=httpx.Timeout(connect=1.0, read=9.0, write=2.0, pool=3.0),
        ),
    )

    client.query("user", id="u1", fields=["id"])

    assert transport.timeouts == [9.0]


def test_an_unbounded_phase_leaves_the_call_ceiling_unbounded(
    schema: GraphQLSchema,
) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(
            url="https://example.test/graphql",
            timeout=httpx.Timeout(connect=1.0, read=None, write=2.0, pool=3.0),
        ),
    )

    client.query("user", id="u1", fields=["id"])

    assert transport.timeouts == [math.inf]


def test_a_per_call_timeout_is_used_as_given(schema: GraphQLSchema) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(
            url="https://example.test/graphql", timeout=httpx.Timeout(30.0)
        ),
    )

    client.query("user", id="u1", fields=["id"], timeout=0.5)

    assert transport.timeouts == [0.5]


def test_schema_loading_uses_the_same_timeout_rule(schema: GraphQLSchema) -> None:
    # Introspection goes through `send()` too, so it needs the same scalar.
    # Passing the configured value straight through cannot type-check once
    # that value may be phase-specific.
    probe = FakeGraphQLTransport(schema)

    build_client(
        url="https://example.test/graphql",
        transport=probe,
        timeout=httpx.Timeout(connect=1.0, read=7.0, write=2.0, pool=3.0),
    )

    assert probe.timeouts == [7.0]


def test_the_root_pool_carries_every_documented_transport_setting() -> None:
    # No socket is opened: this constructs the pool and reads back what it
    # was configured with.
    config = ClientConfig(
        url="https://example.test/graphql",
        timeout=httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0),
        retries=4,
        max_response_bytes=1024,
        trust_env=True,
        proxy="http://proxy.example.test:8080",
        http2=False,
    )

    pool = _new_root_pool(config)
    try:
        assert pool._client.timeout == config.timeout
        assert pool._client.trust_env is True
        assert pool._max_response_bytes == 1024
        assert pool._max_attempts == 5
    finally:
        pool.close()


def test_verify_accepts_an_ssl_context() -> None:
    context = ssl.create_default_context()
    config = ClientConfig(url="https://example.test/graphql", verify=context)

    pool = _new_root_pool(config)
    pool.close()

    assert config.verify is context


# -- proxy credentials and ambient proxies, through the public factory --------

_PROXY_USER = "factory-proxy-user-0123"
_PROXY_PASSWORD = "factory-proxy-pass-0123"
_PROXY_PAIR = f"{_PROXY_USER}:{_PROXY_PASSWORD}"
_PROXY_FORMS = (
    _PROXY_USER,
    _PROXY_PASSWORD,
    _PROXY_PAIR,
    base64.b64encode(_PROXY_PAIR.encode()).decode(),
)


def _with_userinfo(url: str) -> str:
    return url.replace("http://", f"http://{_PROXY_USER}:{_PROXY_PASSWORD}@", 1)


def _reflect_proxy_authorization(
    _body: bytes, headers: dict[str, str]
) -> PlannedResponse:
    return PlannedResponse(
        status=502,
        headers=(("Content-Type", "text/plain"),),
        body=f"bad gateway for {headers.get('proxy-authorization')}".encode(),
    )


_WIRE_USER, _WIRE_PASSWORD = "wire-user", "wire-password"
_WIRE_BASIC = (
    "Basic " + base64.b64encode(f"{_WIRE_USER}:{_WIRE_PASSWORD}".encode()).decode()
)


def _recording_authorization(seen: list[str | None]) -> Any:
    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        seen.append(headers.get("authorization"))
        body = b'{"data": {"user": {"id": "u1"}}}'
        return PlannedResponse(
            200, (("Content-Type", "application/graphql-response+json"),), body
        )

    return respond


@pytest.mark.parametrize(
    "layer", ["none", "config", "auth", "with_headers", "per_call"]
)
def test_an_explicit_authorization_outranks_url_userinfo_on_the_wire(
    schema: GraphQLSchema, layer: str
) -> None:
    # CR-20261001T205548Z-c9bc07f-fc2f7f97-F03. URL userinfo sits below every
    # C4 header layer: it supplies Basic only when no layer set the header.
    bearer = "Bearer layer-token-0123456789"
    seen: list[str | None] = []
    with local_server(_recording_authorization(seen)) as url:
        target = url.replace("http://", f"http://{_WIRE_USER}:{_WIRE_PASSWORD}@", 1)
        client = build_client(
            url=target,
            schema=schema,
            headers={"Authorization": bearer} if layer == "config" else {},
            auth=BearerAuth("layer-token-0123456789") if layer == "auth" else None,
        )
        each = (
            client.with_headers({"Authorization": bearer})
            if layer == "with_headers"
            else client
        )
        try:
            each.query(
                "user",
                id="u1",
                fields=["id"],
                **(
                    {"headers": {"Authorization": bearer}}
                    if layer == "per_call"
                    else {}
                ),
            )
        finally:
            if each is not client:
                each.close()
            client.close()

    assert seen == [_WIRE_BASIC if layer == "none" else bearer]


def test_a_factory_proxy_credential_reaches_no_rendered_path(
    schema: GraphQLSchema,
) -> None:
    with local_server(_reflect_proxy_authorization) as proxy_url:
        client = build_client(
            url="http://target.invalid/graphql",
            schema=schema,
            proxy=_with_userinfo(proxy_url),
        )
        try:
            with pytest.raises(GraphQLTransportError) as caught:
                client.query("user", id="u1", fields=["id"])
            rendered = " ".join(
                (
                    str(caught.value),
                    repr(caught.value),
                    caught.value.body_excerpt,
                    repr(caught.value.request),
                    client.recorder.dump(),
                    repr(client.config),
                )
            )
        finally:
            client.close()

    assert "bad gateway for" in rendered
    assert not [form for form in _PROXY_FORMS if form in rendered]


def _reflect_proxy_token(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
    return PlannedResponse(
        status=502,
        headers=(("Content-Type", "text/plain"),),
        body=f"bad gateway for {headers.get('x-proxy-token')}".encode(),
    )


_REPEATED_PROXY_VALUES = (
    "factory-proxy-token-first-0123456789",
    "factory-proxy-token-second-0123456789",
)


@pytest.mark.parametrize(
    "values", [_REPEATED_PROXY_VALUES, _REPEATED_PROXY_VALUES[::-1]]
)
def test_each_repeated_factory_proxy_header_reaches_no_rendered_path(
    schema: GraphQLSchema, values: tuple[str, str]
) -> None:
    # The loopback proxy quotes one line of the repeated header; both
    # orders make each value the quoted one, through the root client and
    # a derived one.
    rendered: list[str] = []
    with local_server(_reflect_proxy_token) as proxy_url:
        client = build_client(
            url="http://target.invalid/graphql",
            schema=schema,
            proxy=httpx.Proxy(
                proxy_url, headers=[("X-Proxy-Token", value) for value in values]
            ),
        )
        derived = client.anonymous()
        try:
            for each in (client, derived):
                with pytest.raises(GraphQLTransportError) as caught:
                    each.query("user", id="u1", fields=["id"])
                rendered.append(
                    " ".join(
                        (
                            str(caught.value),
                            repr(caught.value),
                            caught.value.body_excerpt,
                            repr(caught.value.request),
                            each.recorder.dump(),
                        )
                    )
                )
        finally:
            derived.close()
            client.close()

    for text in rendered:
        assert "bad gateway for" in text
        assert not [value for value in values if value in text]


# ``Fraction(1, 10**400)`` is positive but rounds to a zero ``float``.
_OUT_OF_DOMAIN_TIMEOUTS = (
    math.nan,
    -math.inf,
    -1.0,
    0,
    1e12,
    10**1000,
    Fraction(1, 10**400),
)


@pytest.mark.parametrize("bad", _OUT_OF_DOMAIN_TIMEOUTS)
def test_a_configured_timeout_outside_the_domain_is_refused_before_io(
    schema: GraphQLSchema, bad: float
) -> None:
    for configured in (bad, httpx.Timeout(connect=1.0, read=bad, write=2.0, pool=3.0)):
        transport = FakeGraphQLTransport(schema)
        client = GraphQLClient(
            transport=transport,
            schema=schema,
            config=ClientConfig(url="https://example.test/graphql", timeout=configured),
        )
        with pytest.raises(ValueError, match=r"ClientConfig\.timeout"):
            client.query("user", id="u1", fields=["id"])
        assert transport.timeouts == []
        with pytest.raises(ValueError, match="greater than zero"):
            build_client(
                url="https://example.test/graphql", schema=schema, timeout=configured
            )


@pytest.mark.parametrize("bad", _OUT_OF_DOMAIN_TIMEOUTS)
def test_a_per_call_timeout_outside_the_domain_is_refused_before_io(
    schema: GraphQLSchema, bad: float
) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql"),
    )

    with pytest.raises(ValueError, match="the timeout option"):
        client.query("user", id="u1", fields=["id"], timeout=bad)

    assert transport.timeouts == []


def test_an_accepted_fraction_timeout_reaches_the_transport_as_a_float(
    schema: GraphQLSchema,
) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(
            url="https://example.test/graphql",
            timeout=httpx.Timeout(Fraction(5), read=Fraction(7, 2)),
        ),
    )

    client.query("user", id="u1", fields=["id"])
    client.query("user", id="u1", fields=["id"], timeout=Fraction(3, 2))

    assert transport.timeouts == [5.0, 1.5]
    assert [type(timeout) for timeout in transport.timeouts] == [float, float]


class _ShrinkingSeconds(float):
    """A ``float`` whose first conversion is its value and every later one 0."""

    conversions = 0

    def __float__(self) -> float:
        self.conversions += 1
        return float.__float__(self) if self.conversions == 1 else 0.0


def test_a_timeout_reaches_the_transport_as_the_float_that_was_checked(
    schema: GraphQLSchema,
) -> None:
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(
            url="https://example.test/graphql",
            timeout=httpx.Timeout(5, read=_ShrinkingSeconds(7.0)),
        ),
    )

    client.query("user", id="u1", fields=["id"])
    client.query("user", id="u1", fields=["id"], timeout=_ShrinkingSeconds(1.5))

    assert transport.timeouts == [7.0, 1.5]


def test_an_explicit_none_timeout_option_is_refused_before_io(
    schema: GraphQLSchema,
) -> None:
    # Only an omitted option means "use the configured timeout"; an explicit
    # None is a value, and it is outside the domain.
    transport = FakeGraphQLTransport(schema)
    client = GraphQLClient(
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql", timeout=12.0),
    )

    with pytest.raises(TypeError, match="the timeout option"):
        client.query("user", id="u1", fields=["id"], timeout=None)
    assert transport.timeouts == []

    client.query("user", id="u1", fields=["id"])
    assert transport.timeouts == [12.0]


def test_a_configuration_repr_shows_no_credential_field() -> None:
    config = ClientConfig(
        url="https://example.test/graphql",
        headers={"Authorization": "Bearer header-secret-0123"},
        schema_headers={"Authorization": "Bearer schema-secret-0123"},
        cookies={"session": "cookie-secret-0123"},
        proxy=f"http://{_PROXY_PAIR}@proxy.example.test:8080",
    )

    rendered = repr(config)

    for secret in ("header-secret", "schema-secret", "cookie-secret", *_PROXY_FORMS):
        assert secret not in rendered
    assert "example.test/graphql" in rendered


def _recording(log: list[str]) -> Any:
    def respond(_body: bytes, headers: dict[str, str]) -> PlannedResponse:
        log.append(headers.get("host", ""))
        return PlannedResponse(
            status=200,
            headers=(("Content-Type", "application/json"),),
            body=b'{"data": {"user": {"id": "u1"}}}',
        )

    return respond


@pytest.mark.parametrize("trust_env", [True, False])
def test_the_factory_follows_the_ambient_proxy_only_when_trusted(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch, trust_env: bool
) -> None:
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    proxied: list[str] = []
    direct: list[str] = []
    with (
        local_server(_recording(proxied)) as proxy_url,
        local_server(_recording(direct)) as target_url,
    ):
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        client = build_client(url=target_url, schema=schema, trust_env=trust_env)
        try:
            client.query("user", id="u1", fields=["id"])
            clone = client.anonymous()
            try:
                clone.query("user", id="u1", fields=["id"])
            finally:
                clone.close()
        finally:
            client.close()

    assert (len(proxied), len(direct)) == ((2, 0) if trust_env else (0, 2))


# -- the documented standalone call shape (SPEC 3.11) -------------------------


def test_the_standalone_example_shape_builds_a_client(schema: GraphQLSchema) -> None:
    # The exact call SPEC 3.11 publishes. A factory that cannot take it is a
    # broken published example.
    probe = FakeGraphQLTransport(schema)

    gql = build_client(
        url="https://api.example.test/graphql",
        transport=probe,
        headers={"Authorization": "Bearer token"},
        max_depth=3,
    )

    assert gql.config.max_depth == 3
    assert gql.config.headers["Authorization"] == "Bearer token"


def test_a_factory_header_reaches_schema_introspection(
    schema: GraphQLSchema,
) -> None:
    # C4 defaults schema identity to `ClientConfig.headers`, so a token given
    # to the factory must authenticate introspection as well as every call.
    probe = FakeGraphQLTransport(schema)

    client = build_client(
        url="https://api.example.test/graphql",
        transport=probe,
        headers={"Authorization": "Bearer token"},
    )

    assert probe.sent[0].operation == "IntrospectionQuery"
    assert probe.sent[0].headers["Authorization"] == "Bearer token"

    client.query("user", id="u1", fields=["id"])
    assert probe.sent[-1].headers["Authorization"] == "Bearer token"


def test_a_factory_option_overrides_the_supplied_configuration(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = build_client(
        url="https://example.test/graphql",
        transport=transport,
        schema=schema,
        config=ClientConfig(max_depth=5, seed=7),
        max_depth=2,
    )

    assert client.config.max_depth == 2
    assert client.config.seed == 7


def test_an_unknown_factory_option_names_the_options_that_exist(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    # Ignoring it would leave the client running on the default the caller
    # believed they had replaced.
    with pytest.raises(ArgumentError) as raised:
        build_client(
            url="https://example.test/graphql",
            transport=transport,
            schema=schema,
            max_dpeth=2,
        )

    assert "max_dpeth" in str(raised.value)
    assert "Did you mean 'max_depth'?" in str(raised.value)


def test_the_factory_still_records_the_url_over_a_supplied_option(
    schema: GraphQLSchema, transport: FakeGraphQLTransport
) -> None:
    client = build_client(
        url="https://example.test/graphql",
        transport=transport,
        schema=schema,
        config=ClientConfig(url="https://stale.example.test/graphql"),
    )

    assert client.config.url == "https://example.test/graphql"
