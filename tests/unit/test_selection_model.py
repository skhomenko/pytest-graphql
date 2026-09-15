"""``Field``, ``Selection``, ``InlineFragment`` and ``AUTO``.

Nothing here touches a schema, so these tests are about shape and about the
two operators. What ``+`` and ``-`` mean against a real schema is asserted in
``test_selection_normalize.py``, where the names can actually be resolved.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core.errors import SelectionError
from pytest_graphql._core.selection.model import (
    AUTO,
    Field,
    InlineFragment,
    Selection,
)


def test_auto_is_one_sentinel_that_names_itself() -> None:
    assert repr(AUTO) == "AUTO"
    assert AUTO is AUTO


def test_a_field_is_keyed_by_its_alias_when_it_has_one() -> None:
    assert Field("total").response_key == "total"
    assert Field("total", alias="grand_total").response_key == "grand_total"


def test_a_field_copies_the_arguments_it_is_given() -> None:
    args = {"first": 10}
    field_ = Field("orders", args=args)
    args["first"] = 99
    assert field_.args == {"first": 10}


def test_a_field_argument_mapping_cannot_be_written_through() -> None:
    field_ = Field("orders", args={"first": 10})
    assert field_.args is not None
    with pytest.raises(TypeError):
        field_.args["first"] = 99  # type: ignore[index]


def test_a_directive_names_the_escape_hatch() -> None:
    with pytest.raises(SelectionError) as caught:
        Field("user", directives={"include": {"if": True}})
    message = str(caught.value)
    assert "directives are not supported in v0.1" in message
    assert "gql.execute(...)" in message
    assert "include" in message


def test_an_empty_directive_mapping_is_not_a_directive() -> None:
    assert Field("user", directives={}).directives == {}


def test_a_field_is_frozen() -> None:
    field_ = Field("user")
    with pytest.raises(Exception):  # noqa: B017 -- dataclasses.FrozenInstanceError
        field_.name = "other"  # type: ignore[misc]


def test_adding_two_selections_keeps_both_operands() -> None:
    left = Selection("id")
    right = Selection("name")
    joined = left + right
    assert joined.parts == (left, right)
    assert joined.removals == ()


def test_subtracting_records_the_removal_against_the_whole_operand() -> None:
    """``(a + b) - "x"`` removes from the union, and ``(a - "x") + b`` does not."""
    a = Selection("id")
    b = Selection("name")
    from_union = (a + b) - "name"
    assert from_union.removals == ("name",)
    union = from_union.parts[0]
    assert isinstance(union, Selection)
    assert union.parts == (a, b)

    scoped = (a - "id") + b
    assert scoped.removals == ()
    left = scoped.parts[0]
    assert isinstance(left, Selection)
    assert left.removals == ("id",)
    assert left.parts == (a,)


def test_adding_something_that_is_not_a_selection_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        Selection("id") + "name"  # type: ignore[operator]


def test_subtracting_something_that_is_not_a_string_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        Selection("id") - Selection("name")  # type: ignore[operator]


def test_selection_of_makes_an_inline_fragment() -> None:
    selection = Selection.of("User", "id", "name")
    assert len(selection.parts) == 1
    fragment = selection.parts[0]
    assert isinstance(fragment, InlineFragment)
    assert fragment.on == "User"
    assert isinstance(fragment.fields, Selection)
    assert fragment.fields.parts == ("id", "name")


def test_selection_of_with_no_fields_takes_the_generated_selection() -> None:
    """An inline fragment cannot be empty, so bare ``of`` means AUTO."""
    fragment = Selection.of("User").parts[0]
    assert isinstance(fragment, InlineFragment)
    assert fragment.fields is AUTO


def test_a_selection_reprs_its_parts_and_removals() -> None:
    assert repr(Selection("id")) == "Selection('id')"
    assert repr(Selection("id") - "id") == "Selection(Selection('id')) - 'id'"
