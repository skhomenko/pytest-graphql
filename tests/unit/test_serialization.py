"""Variable serialization: what is put on the wire for a custom scalar.

``ScalarSpec.serialize`` turns a Python value into JSON. It runs on every
variable value before the value is validated and sent, wherever the scalar
sits: at the top, in a list, in a list of lists, in an input object, or in a
list of input objects. A value that is not JSON afterwards is refused here,
not in the transport.
"""

from __future__ import annotations

import traceback
from decimal import Decimal
from typing import Any

import pytest
from graphql import GraphQLInputType, GraphQLSchema, build_schema, parse_type
from graphql.utilities import type_from_ast

from pytest_graphql._core.errors import ArgumentError
from pytest_graphql._core.factory import ScalarRegistry, ScalarSpec
from pytest_graphql._core.validation import coerce_variables
from tests.factory.schemas import SCHEMA
from tests.schema.scalar_hooks import set_scalar_parser


def money() -> ScalarSpec:
    return ScalarSpec(name="Money", serialize=str, fake=lambda _rng: Decimal("1"))


def registry(*extra: ScalarSpec) -> ScalarRegistry:
    return ScalarRegistry([money(), *extra])


def type_of(text: str) -> GraphQLInputType:
    found = type_from_ast(SCHEMA, parse_type(text))
    assert found is not None
    return found  # type: ignore[return-value]


def wire(
    text: str, value: Any, scalars: ScalarRegistry | None = None, name: str = "v"
) -> Any:
    out = coerce_variables(
        [(name, type_of(text), value)],
        kind="query",
        operation_name="total",
        scalars=scalars,
    )
    return out[name]


# -- where a scalar can sit -------------------------------------------------


def test_a_top_level_custom_scalar_is_serialized() -> None:
    assert wire("Money!", Decimal("1.50"), registry()) == "1.50"


def test_a_list_of_custom_scalars_is_serialized_item_by_item() -> None:
    assert wire("[Money!]", [Decimal("1"), Decimal("2.5")], registry()) == ["1", "2.5"]


def test_a_tuple_is_accepted_for_a_list() -> None:
    assert wire("[Money!]", (Decimal("1"),), registry()) == ["1"]


def test_a_nested_list_is_serialized_at_every_level() -> None:
    grid = [[Decimal("1"), Decimal("2")], [Decimal("3")]]
    assert wire("[[Money!]!]", grid, registry()) == [["1", "2"], ["3"]]


def test_a_single_value_for_a_list_type_is_serialized_then_wrapped() -> None:
    # GraphQL input coercion accepts one item where a list is declared.
    assert wire("[Money!]", Decimal("4"), registry()) == ["4"]


def test_a_custom_scalar_in_an_input_object_is_serialized() -> None:
    value = {"lines": [{"price": Decimal("9.99")}], "total": Decimal("9.99")}
    assert wire("OrderInput!", value, registry()) == {
        "lines": [{"price": "9.99"}],
        "total": "9.99",
    }


def test_a_custom_scalar_in_a_list_of_input_objects_is_serialized() -> None:
    value = {"lines": [{"price": Decimal("1")}, {"price": Decimal("2")}]}
    assert [
        line["price"] for line in wire("OrderInput!", value, registry())["lines"]
    ] == [
        "1",
        "2",
    ]


def test_a_nested_list_inside_an_input_object_is_serialized() -> None:
    value = {"lines": [], "grid": [[Decimal("5")]]}
    assert wire("OrderInput!", value, registry())["grid"] == [["5"]]


def test_a_scalar_list_inside_a_list_of_input_objects_is_serialized() -> None:
    stamp = ScalarSpec(name="Stamp", serialize=lambda n: f"t{n}", fake=lambda _r: 1)
    value = {"lines": [{"price": Decimal("1"), "tags": [1, 2]}]}
    assert wire("OrderInput!", value, registry(stamp))["lines"][0]["tags"] == [
        "t1",
        "t2",
    ]


# -- what is left alone -----------------------------------------------------


def test_null_stays_null_and_serialize_is_not_called() -> None:
    calls: list[object] = []
    spec = ScalarSpec(
        name="Money", serialize=lambda v: calls.append(v) or "x", fake=lambda _r: 1
    )
    assert wire("Money", None, ScalarRegistry([spec])) is None
    assert wire("[Money]", [None], ScalarRegistry([spec])) == [None]
    assert calls == []


def test_a_built_in_scalar_is_never_serialized() -> None:
    assert wire("String", "plain", registry()) == "plain"
    assert wire("Int", 3, registry()) == 3


def test_an_unregistered_custom_scalar_keeps_its_json_value() -> None:
    assert wire("Money!", "1.50", ScalarRegistry()) == "1.50"
    assert wire("Money!", "1.50", None) == "1.50"


def test_an_unregistered_json_scalar_passes_a_json_structure_through() -> None:
    value = {"lines": [], "meta": {"a": [1, 2.5, None, True, "x"]}}
    assert wire("OrderInput!", value, None)["meta"] == {"a": [1, 2.5, None, True, "x"]}


def test_an_enum_value_is_left_for_coercion() -> None:
    assert wire("OrderInput!", {"lines": [], "role": "ADMIN"}, registry())["role"] == (
        "ADMIN"
    )


def test_the_callers_value_is_not_changed() -> None:
    value = {"lines": [{"price": Decimal("1")}], "grid": [[Decimal("2")]]}
    wire("OrderInput!", value, registry())
    assert value == {"lines": [{"price": Decimal("1")}], "grid": [[Decimal("2")]]}


def test_a_spec_registered_later_is_used_by_the_next_call() -> None:
    scalars = ScalarRegistry()
    assert wire("Money!", "1", scalars) == "1"
    scalars.register(
        ScalarSpec(name="Money", serialize=lambda v: f"${v}", fake=lambda _r: 1)
    )
    assert wire("Money!", "1", scalars) == "$1"


# -- validation still runs --------------------------------------------------


def test_validation_runs_after_serialization() -> None:
    with pytest.raises(ArgumentError, match="lines"):
        wire("OrderInput!", {"total": Decimal("1")}, registry())


def test_an_unknown_input_field_is_still_refused() -> None:
    with pytest.raises(ArgumentError, match="nope"):
        wire("OrderInput!", {"lines": [], "nope": Decimal("1")}, registry())


def test_a_wrong_built_in_value_is_still_refused() -> None:
    with pytest.raises(ArgumentError):
        wire("String", Decimal("1"), registry())


# -- refusals name the place and never echo the value -----------------------

SECRET = Decimal("424242.4242")


def test_a_non_json_value_with_no_spec_is_refused_with_the_fix() -> None:
    with pytest.raises(ArgumentError) as caught:
        wire("OrderInput!", {"lines": [{"price": SECRET}]}, ScalarRegistry())
    message = str(caught.value)
    assert "$v.lines[0].price" in message
    assert "Decimal" in message
    assert "Money" in message
    assert "ScalarSpec" in message and "serialize" in message
    assert "424242" not in message


def test_a_serialize_that_raises_is_refused_without_its_message() -> None:
    def explode(value: object) -> str:
        raise RuntimeError(f"bad {value}")

    spec = ScalarSpec(name="Money", serialize=explode, fake=lambda _r: 1)
    with pytest.raises(ArgumentError) as caught:
        wire("[Money!]", [SECRET], ScalarRegistry([spec]))
    message = str(caught.value)
    assert "$v[0]" in message
    assert "serialize" in message and "RuntimeError" in message
    assert "424242" not in message


def test_a_serialize_that_returns_a_non_json_value_is_refused() -> None:
    spec = ScalarSpec(name="Money", serialize=lambda v: v, fake=lambda _r: 1)
    with pytest.raises(ArgumentError) as caught:
        wire("Money!", SECRET, ScalarRegistry([spec]))
    message = str(caught.value)
    assert "returned a Decimal" in message and "not JSON" in message
    assert "424242" not in message


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), b"bytes", {1: "int key"}, {"a": {1, 2}}, object()],
)
def test_a_value_that_is_not_json_is_refused(bad: object) -> None:
    with pytest.raises(ArgumentError, match="not JSON"):
        wire("JSON", bad, ScalarRegistry())


def test_a_json_value_nested_too_deeply_is_refused() -> None:
    deep: Any = "x"
    for _ in range(300):
        deep = [deep]
    with pytest.raises(ArgumentError, match="too deeply"):
        wire("JSON", deep, ScalarRegistry())


def test_a_tuple_in_a_json_value_becomes_a_list() -> None:
    assert wire("JSON", {"a": (1, 2)}, ScalarRegistry()) == {"a": [1, 2]}


def test_the_refusal_names_the_operation_and_the_variable() -> None:
    with pytest.raises(ArgumentError) as caught:
        wire("Money!", SECRET, ScalarRegistry(), name="amount")
    assert "'total'" in str(caught.value)
    assert "amount" in str(caught.value)


# -- the wire carries the serialized value, never a parser's output ----------

TRANSFORMING_SDL = """
scalar Money
enum Color { RED GREEN }
input Line {
  price: Money!
  tags: [Money!]
  color: Color = RED
  cost: Money = "9.99"
}
input Order { lines: [Line!]! total: Money }
type Query {
  total(
    amount: Money
    order: Order
    color: Color
    ids: [ID!]
    f: Float
    n: Int
    b: Boolean
  ): Int
}
"""


def transforming_schema() -> GraphQLSchema:
    """A schema whose scalar parser and enum values are Python-side values."""
    schema = build_schema(TRANSFORMING_SDL)
    set_scalar_parser(schema.type_map["Money"], Decimal)
    schema.type_map["Money"].parse_literal = (  # type: ignore[attr-defined]
        lambda node, _variables=None: Decimal(node.value)
    )
    for member in schema.type_map["Color"].values.values():  # type: ignore[attr-defined]
        member.value = ("py", member.value)
    return schema


def wire_in(
    schema: GraphQLSchema, text: str, value: Any, scalars: ScalarRegistry | None
) -> Any:
    found = type_from_ast(schema, parse_type(text))
    assert found is not None
    out = coerce_variables(
        [("v", found, value)],  # type: ignore[list-item]
        kind="query",
        operation_name="total",
        scalars=scalars,
    )
    return out["v"]


def test_a_transforming_parser_does_not_replace_the_serialized_value() -> None:
    out = wire_in(transforming_schema(), "Money!", Decimal("1.50"), registry())
    assert out == "1.50"
    assert type(out) is str


def test_a_transforming_parser_is_not_applied_to_a_nested_input_value() -> None:
    order = {
        "lines": [{"price": Decimal("1.5"), "tags": [Decimal("2"), Decimal("3")]}],
        "total": Decimal("4"),
    }
    out = wire_in(transforming_schema(), "Order!", order, registry())
    assert out == {
        "lines": [{"price": "1.5", "tags": ["2", "3"]}],
        "total": "4",
    }


def test_a_scalar_with_no_spec_keeps_its_json_value_over_the_parser_output() -> None:
    out = wire_in(transforming_schema(), "Money", "1.5", ScalarRegistry())
    assert out == "1.5"
    assert type(out) is str


def test_the_parsed_value_still_has_to_pass_the_schema_parser() -> None:
    schema = transforming_schema()
    set_scalar_parser(schema.type_map["Money"], lambda _v: int("x"))
    with pytest.raises(ArgumentError, match="invalid value for argument 'v'"):
        wire_in(schema, "Money", Decimal("1"), registry())


def test_an_enum_goes_out_as_its_name_not_its_python_value() -> None:
    assert wire_in(transforming_schema(), "Color", "GREEN", registry()) == "GREEN"


def test_a_default_is_left_to_the_server_so_no_python_default_reaches_the_wire() -> (
    None
):
    out = wire_in(transforming_schema(), "Line", {"price": Decimal("1")}, registry())
    assert out == {"price": "1"}


def test_a_single_item_for_a_list_is_wrapped_in_a_list() -> None:
    schema = transforming_schema()
    assert wire_in(schema, "[Money!]", Decimal("1"), registry()) == ["1"]
    assert wire_in(schema, "[[Money!]]", Decimal("1"), registry()) == [["1"]]


def test_a_serialize_that_returns_a_list_is_kept_whole_for_a_single_item() -> None:
    spec = ScalarSpec(name="Money", serialize=lambda _v: ["a", "b"], fake=lambda _r: 1)
    out = wire("[Money]", Decimal("1"), ScalarRegistry([spec]))
    assert out == [["a", "b"]]


@pytest.mark.parametrize(
    "items", [{1}, range(3), (n for n in (1, 2)), frozenset({7})], ids=str
)
def test_any_iterable_for_a_list_becomes_a_json_list(items: Any) -> None:
    out = wire_in(transforming_schema(), "[ID!]", items, registry())
    assert type(out) is list
    assert all(type(item) is str for item in out)


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [("ID", 5, "5"), ("Float", 1, 1.0), ("Int", 3, 3), ("Boolean", True, True)],
)
def test_a_built_in_scalar_goes_out_in_its_json_form(
    text: str, value: Any, expected: Any
) -> None:
    out = wire_in(transforming_schema(), text, value, registry())
    assert out == expected and type(out) is type(expected)


def test_a_built_in_scalar_the_parser_refuses_is_reported_by_validation() -> None:
    with pytest.raises(ArgumentError, match="invalid value for argument 'v'"):
        wire_in(transforming_schema(), "Int", "not a number", registry())


# -- the public error carries no serializer text anywhere ---------------------


def test_a_serializer_message_is_in_no_rendered_traceback() -> None:
    def explode(_value: object) -> str:
        raise ValueError("sensitive-marker")

    spec = ScalarSpec(name="Money", serialize=explode, fake=lambda _r: 1)
    with pytest.raises(ArgumentError) as caught:
        wire("Money!", Decimal("1"), ScalarRegistry([spec]))
    rendered = "".join(traceback.format_exception(caught.value))
    assert "sensitive-marker" not in rendered
    assert "ValueError" in rendered


def test_the_refusal_keeps_no_exception_object_to_walk_to() -> None:
    def explode(_value: object) -> str:
        raise ValueError("sensitive-marker")

    spec = ScalarSpec(name="Money", serialize=explode, fake=lambda _r: 1)
    with pytest.raises(ArgumentError) as caught:
        wire("Money!", Decimal("1"), ScalarRegistry([spec]))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
