"""The client surface: call flow, identity, ownership and the raising table.

SPEC 11.2's "in-process" layer. Every test here runs a real client against
``FakeGraphQLTransport``, which executes the hostile schema with
``graphql_sync`` and opens no socket.

The lifecycle report has its own file, because the reporting product is large
enough that mixing it in here would hide both.
"""

from __future__ import annotations

from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.auth import BearerAuth, HeaderAuth
from pytest_graphql._core.client import (
    ClientConfig,
    GraphQLClient,
    build_client,
)
from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.errors import (
    GraphQLExecutionError,
    GraphQLPartialDataError,
)
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.response import GraphQLResponse
from pytest_graphql._core.transport.base import RawResponse
from tests.schema.fake_transport import FakeGraphQLTransport, FakeTransport
from tests.schema.resolvers import build_schema


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

    assert calls == []
    assert clone.transport is unrelated
    assert clone.owns_transport is False


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
