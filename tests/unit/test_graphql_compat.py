"""``graphql_compat.py``: one behavior across the graphql-core 3.2 and 3.3 lines.

Every test here runs on both lines, because the CI matrix installs each. A test
that differs by line says so in its name and branches on ``graphql.version_info``,
so a difference between the lines is stated in a test and never left implicit.
"""

from __future__ import annotations

import importlib
from typing import Any

import graphql
import pytest
from graphql import (
    GraphQLEnumType,
    GraphQLFloat,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLScalarType,
    GraphQLString,
    OperationDefinitionNode,
    Undefined,
    build_client_schema,
    build_schema,
    get_introspection_query,
    graphql_sync,
    parse,
    parse_const_value,
    parse_value,
)

from pytest_graphql._core.graphql_compat import (
    LINE_3_3,
    ast_tuple,
    default_of,
    invalid_input_path,
    parse_scalar_input,
    print_value,
)
from pytest_graphql._core.selection.policy import missing_required_arguments

ON_3_3 = graphql.version_info >= (3, 3)

COLOR = GraphQLEnumType("Color", {"RED": "red", "BLUE": "blue"})
POSITIVE = GraphQLScalarType(
    "Positive", parse_value=lambda v: v if isinstance(v, int) and v > 0 else 1 / 0
)
LEAF = GraphQLInputObjectType(
    "Leaf",
    {
        "n": GraphQLInputField(GraphQLNonNull(GraphQLInt)),
        "color": GraphQLInputField(COLOR),
        "tags": GraphQLInputField(GraphQLList(GraphQLNonNull(GraphQLString))),
        "size": GraphQLInputField(POSITIVE),
    },
)
BRANCH = GraphQLInputObjectType(
    "Branch",
    {"leaves": GraphQLInputField(GraphQLList(GraphQLNonNull(LEAF)))},
)


# -- ast_tuple: the shape of an optional AST collection ---------------------


def test_an_absent_collection_is_an_empty_tuple() -> None:
    assert ast_tuple(None) == ()


def test_a_tuple_or_a_list_becomes_a_tuple() -> None:
    assert ast_tuple((1, 2)) == (1, 2)
    assert ast_tuple([1, 2]) == (1, 2)


def test_a_parsed_field_without_arguments_reads_as_empty() -> None:
    # 3.2 parses this to an empty tuple and 3.3 to None. Both read as ().
    document = parse("{ user { id } }")
    operation = document.definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    field = operation.selection_set.selections[0]
    assert ast_tuple(field.arguments) == ()  # type: ignore[attr-defined]
    assert ast_tuple(operation.variable_definitions) == ()


def test_a_parsed_field_with_arguments_reads_as_those_arguments() -> None:
    document = parse("query($i: ID!) { user(id: $i, first: 2) { id } }")
    operation = document.definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    field = operation.selection_set.selections[0]
    names = [a.name.value for a in ast_tuple(field.arguments)]  # type: ignore[attr-defined]
    assert names == ["id", "first"]
    assert len(ast_tuple(operation.variable_definitions)) == 1


# -- invalid_input_path: where a value first fails --------------------------


@pytest.mark.parametrize(
    ("type_", "value"),
    [
        (GraphQLInt, 3),
        (GraphQLInt, None),
        (GraphQLNonNull(GraphQLInt), 3),
        (GraphQLList(GraphQLInt), [1, 2]),
        (GraphQLList(GraphQLInt), 1),
        (GraphQLList(GraphQLInt), []),
        (COLOR, "RED"),
        (LEAF, {"n": 1}),
        (LEAF, {"n": 1, "tags": ["a"], "color": "BLUE", "size": 2}),
        (BRANCH, {"leaves": [{"n": 1}, {"n": 2}]}),
        (GraphQLFloat, 1),
        (GraphQLFloat, 2.5),
    ],
)
def test_a_value_that_fits_its_type_has_no_failure(
    type_: GraphQLInputType, value: Any
) -> None:
    assert invalid_input_path(value, type_) is None


@pytest.mark.parametrize(
    ("type_", "value", "path"),
    [
        (GraphQLInt, "x", ()),
        (GraphQLNonNull(GraphQLInt), None, ()),
        (GraphQLList(GraphQLInt), [1, "x"], (1,)),
        (GraphQLList(GraphQLList(GraphQLInt)), [[1], [2, "x"]], (1, 1)),
        (COLOR, "GREEN", ()),
        (LEAF, {"n": "x"}, ("n",)),
        (LEAF, {"n": 1, "color": "GREEN"}, ("color",)),
        (LEAF, {"n": 1, "tags": ["a", 2]}, ("tags", 1)),
        (LEAF, {"n": 1, "tags": [None]}, ("tags", 0)),
        (LEAF, {"n": 1, "size": -1}, ("size",)),
        (BRANCH, {"leaves": [{"n": 1}, {"n": "x"}]}, ("leaves", 1, "n")),
        (BRANCH, {"leaves": "not a leaf"}, ("leaves",)),
        # A missing or an unknown field is reported at the enclosing object.
        (LEAF, {}, ()),
        (LEAF, {"n": 1, "unknown": 1}, ()),
        (BRANCH, {"leaves": [{"n": 1, "unknown": 1}]}, ("leaves", 0)),
        (LEAF, "not an object", ()),
    ],
)
def test_the_failure_is_reported_as_a_path(
    type_: GraphQLInputType, value: Any, path: tuple[str | int, ...]
) -> None:
    assert invalid_input_path(value, type_) == path


def test_the_path_holds_no_part_of_the_value() -> None:
    secret = "s3cr3t-value-4242"
    found = invalid_input_path({"n": secret}, LEAF)
    assert found == ("n",)
    assert secret not in repr(found)


def test_the_earliest_failure_wins() -> None:
    value = {"n": "x", "tags": [1]}
    assert invalid_input_path(value, LEAF) == ("n",)


def test_an_integer_a_float_cannot_hold_exactly_differs_by_line() -> None:
    # graphql-core 3.3 refuses an integer that a float cannot represent
    # exactly, and 3.2 passes it on. This is graphql-core's own rule for
    # values, so the library reports what the installed line decides.
    found = invalid_input_path(10**30, GraphQLFloat)
    assert found == () if ON_3_3 else found is None


# -- default_of: a default, whichever way the schema declared it -------------

DEFAULTS_SDL = """
input Inner { flag: Boolean = false tags: [Int!] = [1] bare: Int note: String = null }
type Query { f(x: Int! = 3, inner: Inner = {flag: true}, plain: Int, req: Int!): Int }
"""


def _args_and_fields(schema: graphql.GraphQLSchema) -> tuple[Any, Any]:
    assert schema.query_type is not None
    inner = schema.type_map["Inner"]
    assert isinstance(inner, GraphQLInputObjectType)
    return schema.query_type.fields["f"].args, inner.fields


def _introspected(schema: graphql.GraphQLSchema) -> graphql.GraphQLSchema:
    result = graphql_sync(schema, get_introspection_query())
    assert result.data is not None
    return build_client_schema(result.data)


@pytest.mark.parametrize("through", ["sdl", "introspection"])
def test_a_default_in_a_built_schema_reads_as_its_coerced_value(through: str) -> None:
    schema = build_schema(DEFAULTS_SDL)
    if through == "introspection":
        schema = _introspected(schema)
    args, fields = _args_and_fields(schema)
    assert default_of(args["x"]) == 3
    assert default_of(fields["flag"]) is False
    assert default_of(fields["tags"]) == [1]
    assert default_of(fields["note"]) is None


@pytest.mark.parametrize("through", ["sdl", "introspection"])
def test_a_field_with_no_default_reads_as_undefined(through: str) -> None:
    schema = build_schema(DEFAULTS_SDL)
    if through == "introspection":
        schema = _introspected(schema)
    args, fields = _args_and_fields(schema)
    assert default_of(args["plain"]) is Undefined
    assert default_of(args["req"]) is Undefined
    assert default_of(fields["bare"]) is Undefined


def test_an_object_default_carries_the_defaults_of_its_own_fields() -> None:
    args, _ = _args_and_fields(build_schema(DEFAULTS_SDL))
    assert default_of(args["inner"]) == {"flag": True, "tags": [1], "note": None}


@pytest.mark.parametrize("through", ["sdl", "introspection"])
def test_a_required_argument_with_a_default_is_not_missing(through: str) -> None:
    # The A6 skip rule: a non-null argument is required only without a default.
    schema = build_schema(DEFAULTS_SDL)
    if through == "introspection":
        schema = _introspected(schema)
    assert schema.query_type is not None
    field = schema.query_type.fields["f"]
    assert missing_required_arguments(field) == ("req",)


@pytest.mark.skipif(not ON_3_3, reason="a literal default exists on 3.3 only")
def test_a_literal_default_that_does_not_fit_its_type_reads_as_no_default() -> None:
    field = GraphQLInputField(
        GraphQLInt,
        default=graphql.GraphQLDefaultInput(literal=parse_const_value('"x"')),
    )
    assert default_of(field) is Undefined


def test_a_default_given_in_code_reads_as_that_value() -> None:
    field = GraphQLInputField(GraphQLInt, default_value=7)
    assert default_of(field) == 7
    assert default_of(GraphQLInputField(GraphQLInt)) is Undefined


# -- print_value: one spelling of a literal ---------------------------------


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("1", "1"),
        ("-2.5", "-2.5"),
        ("true", "true"),
        ("null", "null"),
        ("RED", "RED"),
        ('"a\\"b"', '"a\\"b"'),
        ("$v", "$v"),
        ("[]", "[]"),
        ("{}", "{}"),
        ("[1, 2]", "[1, 2]"),
        ("{a: 1}", "{a: 1}"),
        ("{ a: 1 }", "{a: 1}"),
        ("{a: 1, b: {c: [1, {d: 2}], e: {}}}", "{a: 1, b: {c: [1, {d: 2}], e: {}}}"),
        ("[{a: 1}, {b: []}]", "[{a: 1}, {b: []}]"),
    ],
)
def test_a_literal_prints_the_same_on_both_lines(text: str, printed: str) -> None:
    assert print_value(parse_value(text)) == printed


# -- parse_scalar_input: a scalar's input hook, under either name -----------


def test_a_built_in_scalar_parses_through_its_input_hook() -> None:
    assert parse_scalar_input(graphql.GraphQLID, 5) == "5"
    assert parse_scalar_input(GraphQLFloat, 1) == 1.0


def test_a_built_in_scalar_refuses_what_it_cannot_parse() -> None:
    with pytest.raises(graphql.GraphQLError):
        parse_scalar_input(GraphQLInt, "x")


def test_a_custom_scalar_parses_with_the_hook_it_was_built_with() -> None:
    scalar = GraphQLScalarType("Doubled", parse_value=lambda v: v * 2)
    assert parse_scalar_input(scalar, 4) == 8


@pytest.mark.skipif(not ON_3_3, reason="the current hook name exists on 3.3 only")
def test_a_scalar_is_parsed_by_the_current_hook_not_the_deprecated_alias() -> None:
    scalar = GraphQLScalarType("Swapped", parse_value=lambda v: ("old", v))
    scalar.coerce_input_value = lambda v: ("new", v)  # type: ignore[assignment]
    assert parse_scalar_input(scalar, 1) == ("new", 1)


# -- the walk ends at the first failure -------------------------------------


def test_the_walk_ends_at_the_first_failure() -> None:
    calls: list[object] = []

    def parse(value: object) -> object:
        calls.append(value)
        raise ValueError("refused")

    scalar = GraphQLScalarType("Counted", parse_value=parse)
    assert invalid_input_path(["x"] * 1000, GraphQLList(scalar)) == (0,)
    assert len(calls) == 1


def test_a_value_with_many_failures_does_not_grow_what_the_helper_holds() -> None:
    import tracemalloc

    tracemalloc.start()
    try:
        invalid_input_path(["x"] * 200_000, GraphQLList(GraphQLInt))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000


# -- the line is chosen by a public signal ----------------------------------


def test_the_line_flag_follows_graphql_cores_own_version() -> None:
    assert LINE_3_3 == ON_3_3


@pytest.mark.skipif(not ON_3_3, reason="the private helper exists on 3.3 only")
def test_a_default_is_read_without_the_private_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = importlib.import_module("graphql.utilities.coerce_input_value")
    monkeypatch.delattr(helper, "coerce_default_value")
    args, fields = _args_and_fields(build_schema(DEFAULTS_SDL))
    assert default_of(args["x"]) == 3
    assert default_of(fields["tags"]) == [1]


@pytest.mark.skipif(not ON_3_3, reason="a value default exists on 3.3 only")
def test_a_default_held_as_a_value_reads_as_that_value() -> None:
    field = GraphQLInputField(
        GraphQLInt,
        default=graphql.GraphQLDefaultInput(7),
    )
    assert default_of(field) == 7
    bad = GraphQLInputField(GraphQLInt, default=graphql.GraphQLDefaultInput("x"))
    assert default_of(bad) is Undefined
