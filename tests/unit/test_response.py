"""Response model (M5b): C5 materialization, B17 ``Node``, B18 ``unwrap``.

Real envelopes come from the hostile schema through ``FakeTransport``. Where a
test needs a shape a correct server would not send, it builds the
``RawResponse`` by hand.
"""

from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any

import pytest
from graphql import build_schema as build_sdl_schema
from graphql import parse

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.errors import (
    GraphQLFieldError,
    GraphQLTestError,
    ResponseShapeError,
)
from pytest_graphql._core.response import (
    GraphQLResponse,
    Node,
    NodeList,
    build_response,
)
from pytest_graphql._core.response.materialize import MAX_DEPTH
from pytest_graphql._core.transport.base import RawResponse
from tests.schema.fake_transport import FakeTransport
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()
SECRET = "s3cr3t-token-value"


def _request(query: str, variables: dict[str, Any] | None = None) -> RequestInfo:
    return RequestInfo(
        operation=None,
        kind="query",
        document=query,
        variables=variables or {},
        headers={"Authorization": f"Bearer {SECRET}"},
        url="http://example.test/graphql",
    )


def build(
    query: str,
    data: Any,
    *,
    errors: tuple[dict[str, Any], ...] = (),
    parsers: Any = None,
    extensions: Any = None,
) -> GraphQLResponse[Any]:
    raw = RawResponse(
        status_code=200,
        media_type="application/json",
        data=data,
        errors=errors,
        extensions=extensions,
        headers={"Content-Type": "application/json"},
    )
    return build_response(
        raw,
        request=_request(query),
        schema=SCHEMA,
        document=parse(query),
        parsers=parsers,
        duration_ms=1.5,
    )


def run(query: str, *, parsers: Any = None) -> GraphQLResponse[Any]:
    envelope = FakeTransport(SCHEMA).execute(query)
    return build(
        query,
        envelope["data"],
        errors=tuple(envelope.get("errors", ())),
        parsers=parsers,
    )


# -- materialization (C5) ----------------------------------------------------


def test_objects_become_nodes_and_lists_of_objects_become_node_lists() -> None:
    response = run("{ users { id name } }")
    users = response.data.users
    assert isinstance(response.data, Node)
    assert isinstance(users, NodeList)
    assert all(isinstance(user, Node) for user in users)
    assert users[0].name == "Ada Lovelace"


def test_aliases_resolve_to_the_alias_name() -> None:
    response = run('{ first: user(id: "u1") { label: name } }')
    assert list(response.data) == ["first"]
    assert response.data.first.label == "Ada Lovelace"
    assert response.unwrap().label == "Ada Lovelace"


def test_named_and_inline_fragments_flatten_onto_the_parent() -> None:
    query = """
    query { search(term: "a") { __typename ...U ... on Team { name } } }
    fragment U on User { name }
    """
    results = run(query).data.search
    assert [type(item) for item in results] == [Node] * 5
    assert [item.__typename__ for item in results][:1] == ["User"]
    assert results[0].name == "Ada Lovelace"
    assert results[3].name == "Platform"
    assert "name" not in results[4]


def test_nested_scalar_list_stays_raw_json() -> None:
    matrix = run("{ matrix }").data.matrix
    assert matrix == [["a", "b"], ["c"]]
    assert type(matrix) is list
    assert type(matrix[0]) is list


@pytest.mark.parametrize(
    "value",
    [{"theme": "dark", "nested": [1, {"a": 2}]}, [1, {"a": 2}, [3]], "text", 7, None],
)
def test_custom_json_scalar_is_never_wrapped(value: Any) -> None:
    response = build(
        '{ user(id: "u1") { preferences } }', {"user": {"preferences": value}}
    )
    got = response.data.user.preferences
    assert got == value
    assert type(got) is type(value)
    assert not isinstance(got, (Node, NodeList))


def test_custom_scalar_goes_to_its_parser_and_null_is_not_parsed() -> None:
    calls: list[Any] = []

    def parse_money(value: Any) -> str:
        calls.append(value)
        return f"parsed:{value}"

    query = "{ users { id balance } }"
    data = {"users": [{"id": "u1", "balance": 5}, {"id": "u2", "balance": None}]}
    users = build(query, data, parsers={"Money": parse_money}).data.users
    assert calls == []  # lazy
    assert users[0].balance == "parsed:5"
    assert users[1].balance is None
    assert calls == [5]
    assert users[0].balance == "parsed:5"
    assert calls == [5]  # cached


def test_parser_for_a_built_in_scalar_name_is_ignored() -> None:
    response = run("{ users { name } }", parsers={"String": lambda _v: "X"})
    assert response.data.users[0].name == "Ada Lovelace"


def test_nested_lists_of_a_parsed_scalar_are_parsed_element_wise() -> None:
    schema = build_sdl_schema("scalar UUID type Query { ids: [[UUID!]!]! }")
    raw = RawResponse(200, "application/json", {"ids": [["a", "b"], []]}, (), None, {})
    query = "{ ids }"
    response = build_response(
        raw,
        request=_request(query),
        schema=schema,
        document=parse(query),
        parsers={"UUID": str.upper},
    )
    assert response.data.ids == [["A", "B"], []]
    assert raw.data == {"ids": [["a", "b"], []]}  # the input is not modified


def test_null_at_every_level_stays_none() -> None:
    response = build(
        '{ user(id: "u9") { id } nullableList }',
        {"user": None, "nullableList": None},
    )
    assert response.data.user is None
    assert response.data.nullable_list is None


def test_round_trip_to_dict_equals_the_received_data() -> None:
    for query in [
        "{ users { id name manager { id } } }",
        '{ search(term: "a") { __typename ... on User { name } } }',
        "{ matrix teams { id members { name } } }",
    ]:
        envelope = FakeTransport(SCHEMA).execute(query)
        response = build(query, copy.deepcopy(envelope["data"]))
        assert response.data.to_dict() == envelope["data"]
        assert response.raw["data"] == envelope["data"]


# -- states (C5) --------------------------------------------------------------


def test_present_data() -> None:
    response = run("{ users { id } }")
    assert response.has_data
    assert response.data_state == "present"


def test_null_data_with_errors() -> None:
    error = {"message": "boom", "path": ["users", 0], "extensions": {"code": "X"}}
    response = build("{ users { id } }", None, errors=(error,))
    assert not response.has_data
    assert response.data_state == "null"
    assert response.data is None
    assert response.errors[0].code == "X"
    assert response.errors[0].path == ("users", 0)
    assert response.raw == {"data": None, "errors": [error]}


def test_partial_data_keeps_data_and_errors() -> None:
    response = build(
        '{ user(id: "u1") { id manager { id } } }',
        {"user": {"id": "u1", "manager": None}},
        errors=({"message": "partial"},),
    )
    assert response.has_data
    assert len(response.errors) == 1
    assert response.errors[0].code is None


def test_raw_is_the_untouched_envelope() -> None:
    data = {"user": {"preferences": {"k": [1, 2]}}}
    snapshot = copy.deepcopy(data)
    response = build('{ user(id: "u1") { preferences } }', data)
    _ = response.data.user.preferences
    assert response.raw == {"data": snapshot}
    assert response.raw["data"] is data
    assert response.data.user.preferences is data["user"]["preferences"]


# -- Node (B17) ---------------------------------------------------------------


def test_node_mapping_uses_original_server_keys() -> None:
    user = run('{ user(id: "u1") { id joinedAt } }').data.user
    assert list(user) == ["id", "joinedAt"]
    assert len(user) == 2
    assert dict(user) == {"id": "u1", "joinedAt": "2020-01-01T00:00:00+00:00"}
    assert user["joinedAt"] == user["joined_at"] == user.joined_at
    assert "joined_at" in user
    assert "nope" not in user
    assert user.get("nope", 5) == 5


def test_node_equality_rules() -> None:
    a = run('{ user(id: "u1") { id name } }').data.user
    b = run('{ user(id: "u1") { id name } }').data.user
    assert a == b
    assert a == {"id": "u1", "name": "Ada Lovelace"}
    assert a != {"id": "u1"}  # exact, not partial
    assert a.__eq__(object()) is NotImplemented
    with pytest.raises(TypeError):
        hash(a)


def test_missing_field_raises_with_suggestion_and_is_attribute_error() -> None:
    user = run('{ user(id: "u1") { id name } }').data.user
    with pytest.raises(GraphQLFieldError) as excinfo:
        _ = user.nam
    message = str(excinfo.value)
    assert "no field 'nam' on User" in message
    assert "Did you mean 'name'?" in message
    assert not hasattr(user, "nam")
    assert getattr(user, "nam", None) is None
    with pytest.raises(KeyError):
        _ = user["nam"]


def test_absent_and_null_are_different() -> None:
    user = build(
        '{ user(id: "u1") { id manager { id } } }',
        {"user": {"id": "u1", "manager": None}},
    ).data.user
    assert user.manager is None
    with pytest.raises(GraphQLFieldError):
        _ = user.team


def test_snake_collision_is_ambiguous_but_exact_keys_work() -> None:
    user = run('{ user(id: "u1") { userId user_id } }').data.user
    assert user["userId"] == "u1"
    with pytest.warns(UserWarning, match="exact name wins"):
        assert user["user_id"] == "u1-snake"
    query = '{ user(id: "u1") { userId userID } }'
    twin = build(query, {"user": {"userId": "a", "userID": "b"}}).data.user
    assert twin["userId"] == "a"
    assert twin["userID"] == "b"
    with pytest.raises(GraphQLFieldError, match="ambiguous"):
        _ = twin.user_id
    with pytest.raises(GraphQLFieldError, match="ambiguous"):
        twin.get("user_id")


def test_private_and_dunder_attributes_are_not_fields() -> None:
    user = run('{ user(id: "u1") { id } }').data.user
    assert not hasattr(user, "_nothing")
    assert not hasattr(user, "__copy__")


def test_typename_is_none_when_the_response_carries_none_for_an_interface() -> None:
    node = build('{ node(id: "u1") { id } }', {"node": {"id": "u1"}}).data.node
    assert node.__typename__ is None
    assert node.id == "u1"
    typed = build(
        '{ node(id: "u1") { __typename id } }',
        {"node": {"__typename": "User", "id": "u1"}},
    ).data.node
    assert typed.__typename__ == "User"


def test_abstract_selection_without_typename_unions_all_fragments() -> None:
    query = '{ search(term: "a") { ... on User { joinedAt } ... on Team { name } } }'
    data = {"search": [{"joinedAt": "2020"}, {"name": "T"}]}
    parsed = build(query, data, parsers={"DateTime": lambda v: f"dt:{v}"}).data.search
    assert parsed[0].joined_at == "dt:2020"
    assert parsed[1].name == "T"


def test_node_to_dict_is_an_independent_copy() -> None:
    user = run('{ user(id: "u1") { preferences } }').data.user
    copy_ = user.to_dict()
    copy_["preferences"]["theme"] = "changed"
    assert user.preferences["theme"] == "dark"


def test_node_repr_lists_field_names_and_never_values() -> None:
    query = '{ user(id: "u1") { id name } }'
    data = {"user": {"id": SECRET, "name": SECRET, "extra": SECRET}}
    user = build(query, data).data.user
    text = repr(user)
    assert text == "User(id, name, +1 unselected)"
    assert SECRET not in text
    assert str(user) == text


# -- unwrap (B18), at, pluck ---------------------------------------------------


def test_unwrap_returns_the_single_top_level_value() -> None:
    assert isinstance(run("{ users { id } }").unwrap(), NodeList)


def test_unwrap_ignores_unselected_top_level_keys() -> None:
    response = build("{ users { id } }", {"users": [], "unexpected": 1})
    assert response.unwrap() == []
    assert response.data["unexpected"] == 1


def test_unwrap_counts_a_selected_field_the_server_omitted() -> None:
    response = build("{ users { id } }", {})
    with pytest.raises(GraphQLFieldError, match="users"):
        response.unwrap()


def test_unwrap_names_fields_when_there_are_several() -> None:
    with pytest.raises(GraphQLTestError, match="users, teams"):
        run("{ users { id } teams { id } }").unwrap()


def test_unwrap_without_data_raises() -> None:
    with pytest.raises(GraphQLTestError, match="null"):
        build("{ users { id } }", None, errors=({"message": "x"},)).unwrap()


def test_at_pluck_and_ids() -> None:
    response = run("{ users { id manager { id } } }")
    assert response.at("users.1.manager.id") == "u1"
    assert response.at("users[2].manager.id") == "u1"
    assert response.at("users.0.manager.id", None) is None
    assert response.data.users.ids() == ["u1", "u2", "u3"]
    assert response.data.users.pluck("manager.id", None) == [None, "u1", "u1"]
    with pytest.raises(GraphQLFieldError):
        response.data.users.pluck("manager.id")


def test_at_missing_segment_raises_unless_default() -> None:
    response = run("{ users { id } }")
    with pytest.raises(GraphQLFieldError):
        response.at("users.0.nope")
    with pytest.raises(GraphQLFieldError):
        response.at("users.9.id")
    assert response.at("users.0.nope", "d") == "d"
    assert build("{ users { id } }", None).at("users", 3) == 3


# -- shape errors ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "data", "fragment"),
    [
        (
            "{ users { id } }",
            {"users": {"id": "u1"}},
            "expected [User!]! at data.users",
        ),
        ("{ users { id } }", {"users": ["u1"]}, "expected User! at data.users.0"),
        (
            "{ users { id } }",
            {"users": [{"id": [1]}]},
            "expected ID! at data.users.0.id",
        ),
        ("{ pingScalar }", {"pingScalar": "yes"}, "expected Boolean!"),
        ("{ pingScalar }", {"pingScalar": 1}, "expected Boolean!"),
        ("{ matrix }", {"matrix": ["a"]}, "expected [String!]! at data.matrix.0"),
        ("{ matrix }", {"matrix": [[1]]}, "expected String!"),
        ("{ users { name } }", {"users": [{"name": {"a": 1}}]}, "got a JSON object"),
    ],
)
def test_data_contradicting_its_type_raises_shape_error(
    query: str, data: Any, fragment: str
) -> None:
    with pytest.raises(ResponseShapeError, match=r".*") as excinfo:
        build(query, data)
    assert fragment in str(excinfo.value)


def test_int_and_float_rules() -> None:
    query = "{ users { id } }"
    assert build(query, {"users": [{"id": 5}]}).data.users[0].id == 5
    with pytest.raises(ResponseShapeError):
        build(query, {"users": [{"id": True}]})
    with pytest.raises(ResponseShapeError):
        build(
            '{ search(term: "a") { ... on Attachment { sizeBytes } } }',
            {"search": [{"sizeBytes": 1.5}]},
        )
    with pytest.raises(ResponseShapeError):
        build(
            '{ search(term: "a") { ... on Attachment { sizeBytes } } }',
            {"search": [{"sizeBytes": True}]},
        )


def test_impossible_typename_raises_without_echoing_it() -> None:
    query = '{ node(id: "u1") { __typename id } }'
    with pytest.raises(ResponseShapeError) as excinfo:
        build(query, {"node": {"__typename": SECRET, "id": "u1"}})
    assert SECRET not in str(excinfo.value)
    assert "not a possible type of Node" in str(excinfo.value)
    with pytest.raises(ResponseShapeError):
        build(query, {"node": {"__typename": 5, "id": "u1"}})
    with pytest.raises(ResponseShapeError):
        build(query, {"node": {"__typename": "Attachment", "id": "u1"}})


def test_shape_error_never_echoes_a_value() -> None:
    with pytest.raises(ResponseShapeError) as excinfo:
        build("{ pingScalar }", {"pingScalar": SECRET})
    assert SECRET not in str(excinfo.value)
    assert excinfo.value.path == ("pingScalar",)


def test_hostile_depth_is_refused_not_a_recursion_error() -> None:
    levels = 140
    query = (
        '{ user(id: "u1") { ' + "manager { " * levels + "id" + " }" * levels + " } }"
    )
    inner: Any = {"id": "u1"}
    for _ in range(levels):
        inner = {"manager": inner}
    with pytest.raises(ResponseShapeError, match="deeper than"):
        build(query, {"user": inner})


def _nested(levels: int) -> Any:
    """A JSON value holding ``levels`` nested containers."""
    inner: Any = []
    for _ in range(levels - 1):
        inner = {"k": inner}
    return inner


@pytest.mark.parametrize(
    ("levels", "refused"), [(MAX_DEPTH - 2, False), (MAX_DEPTH - 1, True)]
)
def test_depth_bound_applies_to_a_selected_custom_scalar(
    levels: int, refused: bool
) -> None:
    # ``data`` is level 1 and ``user`` is level 2, so 126 more levels fit.
    query = '{ user(id: "u1") { preferences } }'
    data = {"user": {"preferences": _nested(levels)}}
    if refused:
        with pytest.raises(ResponseShapeError, match="deeper than"):
            build(query, data)
    else:
        build(query, data)


@pytest.mark.parametrize(
    ("levels", "refused"), [(MAX_DEPTH - 2, False), (MAX_DEPTH - 1, True)]
)
def test_depth_bound_applies_to_an_unselected_extra_key(
    levels: int, refused: bool
) -> None:
    query = '{ user(id: "u1") { id } }'
    data = {"user": {"id": "u1", "extra": _nested(levels)}}
    if refused:
        with pytest.raises(ResponseShapeError, match="deeper than"):
            build(query, data)
    else:
        build(query, data)


def test_depth_bound_applies_to_errors_and_extensions() -> None:
    deep = _nested(MAX_DEPTH + 1)
    with pytest.raises(ResponseShapeError, match=r"errors\.0"):
        build("{ pingScalar }", None, errors=({"message": "x", "extensions": deep},))
    with pytest.raises(ResponseShapeError, match="extensions"):
        build("{ pingScalar }", {"pingScalar": True}, extensions={"deep": deep})


def _proxied(containers: int) -> Any:
    """A tree of ``containers`` alternating non-dict mappings and tuples."""
    inner: Any = 1
    for index in range(containers):
        inner = MappingProxyType({"k": inner}) if index % 2 == 0 else (inner,)
    return inner


@pytest.mark.parametrize(
    ("containers", "refused"), [(MAX_DEPTH - 1, False), (MAX_DEPTH, True)]
)
def test_depth_bound_follows_non_dict_mappings_and_tuples(
    containers: int, refused: bool
) -> None:
    # The outer mapping is level 1, so 127 containers fit below it.
    query = "{ pingScalar }"
    data = {"pingScalar": True}
    deep = _proxied(containers)
    error = MappingProxyType({"message": "x", "extensions": deep})
    cases = [
        lambda: build(query, data, extensions=MappingProxyType({"d": deep})),
        lambda: build(query, data, errors=(error,)),
    ]
    for case in cases:
        if refused:
            with pytest.raises(ResponseShapeError, match="deeper than"):
                case()
        else:
            case()


def test_depth_error_never_echoes_a_value() -> None:
    deep: Any = [SECRET]
    for _ in range(MAX_DEPTH):
        deep = [deep]
    with pytest.raises(ResponseShapeError) as excinfo:
        build('{ user(id: "u1") { preferences } }', {"user": {"preferences": deep}})
    assert SECRET not in str(excinfo.value)


def test_extra_unselected_keys_are_tolerated_and_kept() -> None:
    user = build(
        '{ user(id: "u1") { id } }', {"user": {"id": "u1", "x": {"y": 1}}}
    ).data.user
    assert user["x"] == {"y": 1}
    assert user.to_dict() == {"id": "u1", "x": {"y": 1}}


def test_absent_selected_key_is_tolerated_but_raises_on_access() -> None:
    user = build('{ user(id: "u1") { id name } }', {"user": {"id": "u1"}}).data.user
    with pytest.raises(GraphQLFieldError):
        _ = user.name


@pytest.mark.parametrize(
    ("extensions", "kept"),
    [(None, False), ({}, False), ({"trace": 1}, True)],
)
def test_raw_carries_extensions_only_when_non_empty(
    extensions: Any, kept: bool
) -> None:
    response = build("{ pingScalar }", {"pingScalar": True}, extensions=extensions)
    assert ("extensions" in response.raw) is kept
    if kept:
        assert response.raw["extensions"] == {"trace": 1}
    assert response.extensions == (extensions or {})


# -- envelope and the redaction boundary -----------------------------------------


def test_envelope_carries_http_request_and_timing() -> None:
    response = run("{ users { id } }")
    assert response.http.status_code == 200
    assert response.http.media_type == "application/json"
    assert response.http.url == "http://example.test/graphql"
    assert response.http.headers["Content-Type"] == "application/json"
    assert response.duration_ms == 1.5
    assert response.extensions == {}


def test_request_is_the_redacted_snapshot_never_the_live_request() -> None:
    response = run("{ users { id } }")
    assert not isinstance(response.request, RequestInfo)
    assert SECRET not in repr(response.request)


def test_response_url_has_userinfo_and_query_credentials_stripped() -> None:
    request = RequestInfo(
        operation=None,
        kind="query",
        document="{ users { id } }",
        variables={},
        headers={},
        url=f"http://user:{SECRET}@example.test/graphql?token={SECRET}",
    )
    raw = RawResponse(200, "application/json", {"users": []}, (), None, {})
    response = build_response(
        raw, request=request, schema=SCHEMA, document=parse("{ users { id } }")
    )
    assert SECRET not in response.http.url


def test_nothing_the_server_sent_appears_in_any_repr_or_str() -> None:
    query = '{ user(id: "u1") { id name } }'
    error = {"message": f"echo {SECRET}", "extensions": {"code": SECRET}}
    response = build(
        query,
        {"user": {"id": SECRET, "name": SECRET}},
        errors=(error,),
    )
    for obj in (
        response,
        response.data,
        response.data.user,
        response.errors[0],
        response.http,
    ):
        assert SECRET not in repr(obj)
        assert SECRET not in str(obj)
    assert "errors=1" in repr(response)


def test_request_secret_in_the_request_never_reaches_the_response_repr() -> None:
    response = run("{ users { id } }")
    assert SECRET not in repr(response)
    assert SECRET not in str(response)


def test_graphql_error_info_degrades_instead_of_raising() -> None:
    from pytest_graphql._core.response import GraphQLErrorInfo

    info = GraphQLErrorInfo.from_mapping(
        {"message": "m", "locations": [{"line": 1, "column": 2}], "path": ["a", 1]}
    )
    assert info.locations == ((1, 2),)
    assert info.path == ("a", 1)
    assert info.code is None
    assert GraphQLErrorInfo.from_mapping({"extensions": {"code": 5}}).code is None


# -- the top-level package surface (C9) ---------------------------------------


def test_response_api_is_exported_from_the_top_level_package() -> None:
    import pytest_graphql

    expected = {
        "GraphQLResponse": GraphQLResponse,
        "Node": Node,
        "NodeList": NodeList,
        "ResponseShapeError": ResponseShapeError,
        "GraphQLFieldError": GraphQLFieldError,
    }
    for name, obj in expected.items():
        assert getattr(pytest_graphql, name) is obj
        assert name in pytest_graphql.__all__
    for internal in ("build_response", "HttpInfo", "GraphQLErrorInfo"):
        assert internal not in pytest_graphql.__all__
