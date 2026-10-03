"""The ``expect`` namespace and its construction-time validation (SPEC 3.6)."""

from __future__ import annotations

import pytest
from graphql import build_schema

from pytest_graphql._core.errors import SchemaError, SelectionError
from pytest_graphql._core.matching import ExpectNamespace, absent, gt
from tests.unit.matching_support import SCHEMA, user_node

expect = ExpectNamespace(SCHEMA)


def test_an_unknown_field_raises_at_construction_not_at_assertion() -> None:
    with pytest.raises(SelectionError) as caught:
        expect.User(frist_name="John")
    message = str(caught.value)
    assert "no field 'frist_name' on User" in message
    assert "Available fields:" in message


def test_the_error_suggests_the_exact_spelling() -> None:
    with pytest.raises(SelectionError, match="Did you mean"):
        expect.User(firstNme="John")


def test_the_available_fields_are_the_schema_names() -> None:
    with pytest.raises(SelectionError) as caught:
        expect.Settings(nope=1)
    assert "theme, locale" in str(caught.value)


def test_an_unknown_type_raises_with_a_suggestion() -> None:
    with pytest.raises(SchemaError, match="Did you mean 'User'"):
        expect.Usr  # noqa: B018


@pytest.mark.parametrize("name", ["UserInput", "Role", "String", "ID"])
def test_a_type_without_selectable_fields_is_refused(name: str) -> None:
    with pytest.raises(SchemaError, match="object, interface or union"):
        getattr(expect, name)


def test_a_snake_collision_is_ambiguous_but_exact_names_resolve() -> None:
    # ``userId`` and ``user_id`` share the snake form ``user_id``. The exact
    # spelling of each is a hit on its own.
    with pytest.warns(UserWarning, match="exact name wins"):
        expect.User(user_id=absent())
    expect.User(userId=absent())


def test_the_typename_meta_field_is_always_allowed() -> None:
    assert user_node() == expect.User(__typename="User")
    assert user_node() != expect.User(__typename="Order")


def test_the_union_has_no_fields_so_any_other_name_is_refused() -> None:
    with pytest.raises(SelectionError, match="union"):
        expect.Hit(id="1")


def test_a_field_named_like_a_python_keyword_or_self_is_accepted_by_name() -> None:
    # The call takes only keywords, so ``self`` as a field name must not
    # collide with the bound instance.
    with pytest.raises(SelectionError, match="no field 'self' on User"):
        expect.User(**{"self": 1})


def test_private_and_dunder_names_are_attribute_errors() -> None:
    with pytest.raises(AttributeError):
        expect._private  # noqa: B018
    assert not hasattr(expect, "__wrapped__")


def test_the_namespace_lists_its_type_names() -> None:
    names = dir(expect)
    assert {"User", "Order", "Team", "Settings", "Entity", "Hit"} <= set(names)
    assert "Query" in names  # a root type matches ``response.data``
    assert "String" not in names
    assert "UserInput" not in names
    assert not any(name.startswith("__") and name[2:3].isupper() for name in names)


def test_nested_helpers_are_not_validated_against_the_schema() -> None:
    # Only the field names of the named type are checked, as SPEC 3.6 says.
    matcher = expect.User(first_name=gt(0))
    assert user_node() != matcher


def test_a_matcher_remembers_its_type_name() -> None:
    assert expect.User(first_name="John").type_name == "User"


def test_a_type_name_with_a_leading_underscore_is_a_valid_type() -> None:
    schema = build_schema("type _User { id: ID } type Query { u: _User }")
    namespace = ExpectNamespace(schema)
    assert "_User" in dir(namespace)
    assert namespace._User(id="1").type_name == "_User"
    with pytest.raises(SelectionError, match="no field 'nope' on _User"):
        namespace._User(nope=1)


def test_an_unknown_underscored_name_stays_an_attribute_error() -> None:
    namespace = ExpectNamespace(
        build_schema("type _User { id: ID } type Query { u: _User }")
    )
    with pytest.raises(AttributeError):
        namespace._Missing  # noqa: B018
    with pytest.raises(AttributeError):
        namespace.__wrapped__  # noqa: B018
