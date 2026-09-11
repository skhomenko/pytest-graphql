"""Argument and field naming (B10): conversion and lookup, never regeneration."""

from __future__ import annotations

import warnings

import pytest

from pytest_graphql._core.naming import NameMap, to_camel, to_snake


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("firstName", "first_name"),
        ("userID", "user_id"),
        ("HTTPStatus", "http_status"),
        ("id", "id"),
        ("userId", "user_id"),
        ("user_id", "user_id"),
        ("A", "a"),
        ("nonNullListOfNullables", "non_null_list_of_nullables"),
    ],
)
def test_to_snake(name: str, expected: str) -> None:
    assert to_snake(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("first_name", "firstName"),
        ("user_id", "userId"),
        ("http_status", "httpStatus"),
        ("id", "id"),
    ],
)
def test_to_camel(name: str, expected: str) -> None:
    assert to_camel(name) == expected


def test_name_map_resolves_by_exact_spelling() -> None:
    names = NameMap.build(["firstName", "id"])
    assert names.get("firstName") == "firstName"
    assert names.get("id") == "id"


def test_name_map_resolves_by_snake_spelling() -> None:
    names = NameMap.build(["firstName"])
    assert names.get("first_name") == "firstName"


def test_name_map_unknown_key_resolves_to_none() -> None:
    names = NameMap.build(["firstName"])
    assert names.get("lastName") is None
    assert names.get("last_name") is None


def test_name_map_two_distinct_names_collapsing_is_ambiguous() -> None:
    # "userID" and "userId" both collapse to "user_id", and neither one is
    # literally spelled "user_id". That key is ambiguous: it resolves to
    # nothing, naming both spellings, while each exact spelling still
    # resolves to itself.
    names = NameMap.build(["userID", "userId"])
    assert names.is_ambiguous("user_id")
    assert names.ambiguous_names("user_id") == ("userID", "userId")
    assert names.get("user_id") is None
    assert names.get("userID") == "userID"
    assert names.get("userId") == "userId"


def test_name_map_three_distinct_names_collapsing_names_all_three() -> None:
    # "userId", "userID" and "user_Id" are three distinct, legal GraphQL
    # field names that all collapse to "user_id", none of them spelled that
    # way. Every one of the three must survive, not just the last pair seen.
    names = NameMap.build(["userId", "userID", "user_Id"])
    assert names.is_ambiguous("user_id")
    assert names.ambiguous_names("user_id") == ("userID", "userId", "user_Id")
    assert names.get("user_id") is None
    assert names.get("userId") == "userId"
    assert names.get("userID") == "userID"
    assert names.get("user_Id") == "user_Id"


def test_name_map_exact_name_wins_over_a_colliding_snake_form() -> None:
    # The hostile test schema (tests/schema/sdl.graphql) carries User.userId
    # and User.user_id side by side, deliberately, to exercise this rule:
    # "user_id" is both an exact schema name of its own field and the snake
    # form of "userId". The exact spelling wins, with a warning.
    names = NameMap.build(["userId", "user_id"])
    with pytest.warns(UserWarning, match="exact name wins"):
        resolved = names.get("user_id")
    assert resolved == "user_id"
    assert names.get("userId") == "userId"


def test_name_map_no_warning_when_exact_and_snake_agree() -> None:
    names = NameMap.build(["first_name"])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert names.get("first_name") == "first_name"
