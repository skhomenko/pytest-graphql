"""Message quality for the public exception hierarchy (SPEC 8.1, 8.3).

The four cases below reproduce the exact example text SPEC 8.3 gives, byte for
byte. The remaining tests cover the rest of the hierarchy: every
``GraphQLClientError`` states what was wrong, what was expected, and what to
do, and every leaf class SPEC 8.1 names exists and is reachable from
``GraphQLTestError``, the root SPEC 8.1 calls ``GraphQLError`` and
`docs/reference/DESIGN_DECISIONS.md` section 1 renames to avoid clashing with
``graphql.GraphQLError``.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core.errors import (
    ArgumentError,
    ExpectedErrorNotRaised,
    GraphQLClientError,
    GraphQLConnectionError,
    GraphQLExecutionError,
    GraphQLFieldError,
    GraphQLHTTPStatusError,
    GraphQLPartialDataError,
    GraphQLTestError,
    GraphQLTimeoutError,
    GraphQLTransportError,
    OperationNotFoundError,
    ScalarNotRegisteredError,
    SchemaError,
    SelectionError,
    SelectionTooLargeError,
    WaitTimeoutError,
)


def test_operation_not_found_matches_the_spec_shape() -> None:
    # SPEC 8.3's own example ('usr' against user/users/userSettings) is
    # illustrative prose: against real difflib.get_close_matches defaults,
    # 'usr' to 'userSettings' scores 0.4, below the 0.6 cutoff, so that
    # exact triple never actually round-trips. This proves the same shape,
    # three lines and all, with a typo that really does produce three
    # suggestions under the same default parameters.
    error = OperationNotFoundError(
        kind="query",
        name="useres",
        available=["user", "users", "userSettings"],
        total_count=84,
    )
    assert str(error) == (
        "no query named 'useres'.\n"
        "  Did you mean: users, user, userSettings?\n"
        "  This schema has 84 queries. "
        "Run with --gql-show-schema-stats to list them."
    )


def test_argument_error_matches_the_spec_example() -> None:
    error = ArgumentError(
        kind="mutation",
        operation_name="createUser",
        bad_name="emial",
        signature="createUser(input: CreateUserInput!): User!",
        candidates=["email", "name", "role", "settings"],
        input_type_name="CreateUserInput",
        input_fields=["email", "name", "role", "settings"],
    )
    assert str(error) == (
        "mutation 'createUser' has no argument 'emial'.\n"
        "  Signature: createUser(input: CreateUserInput!): User!\n"
        "  CreateUserInput fields: email, name, role, settings\n"
        "  Did you mean 'email'?"
    )


def test_graphql_field_error_matches_the_spec_example() -> None:
    error = GraphQLFieldError.unknown_field(
        "User",
        "frist_name",
        ["id", "first_name", "last_name", "email", "status"],
    )
    assert str(error) == (
        "no field 'frist_name' on User.\n"
        "  Available in this response: id, first_name, last_name, email, status\n"
        "  Did you mean 'first_name'?\n"
        "  Note: the field may exist on the type but not be in this selection set."
    )


def test_selection_too_large_matches_the_spec_example() -> None:
    error = SelectionTooLargeError(type_name="Retailer", field_count=3184, limit=2000)
    assert str(error) == (
        "auto-selection of 'Retailer' produced 3,184 fields (limit 2000).\n"
        "  Reduce it by one of:\n"
        "    max_depth=2\n"
        '    per_type_depth_cap={"User": 1}\n'
        '    fields=["id", "name"]'
    )


def test_operation_not_found_omits_suggestion_line_with_no_close_match() -> None:
    error = OperationNotFoundError(
        kind="mutation", name="zzz", available=["createUser"], total_count=1
    )
    assert "Did you mean" not in str(error)


def test_argument_error_omits_input_fields_line_when_not_wrapped() -> None:
    error = ArgumentError(
        kind="query",
        operation_name="user",
        bad_name="ide",
        signature="user(id: ID!): User",
        candidates=["id"],
    )
    assert str(error) == (
        "query 'user' has no argument 'ide'.\n"
        "  Signature: user(id: ID!): User\n"
        "  Did you mean 'id'?"
    )


def test_selection_error_unknown_field_reports_valid_names() -> None:
    error = SelectionError.unknown_field("User", "naem", ["id", "name"])
    text = str(error)
    assert "no field 'naem' on User." in text
    assert "Available fields: id, name" in text
    assert "Did you mean 'name'?" in text


def test_selection_error_from_validation_is_verbatim() -> None:
    error = SelectionError.from_validation("Cannot query field 'x' on type 'Y'.")
    assert str(error) == "Cannot query field 'x' on type 'Y'."


def test_schema_error_unknown_type_suggests_a_close_match() -> None:
    error = SchemaError.unknown_type("Usre", ["User", "Team", "Post"])
    assert "no type named 'Usre'." in str(error)
    assert "Did you mean 'User'?" in str(error)


def test_schema_error_not_a_possible_type() -> None:
    error = SchemaError.not_a_possible_type("Team", "SearchResult")
    assert str(error) == "'Team' is not a possible type of 'SearchResult'."


def test_scalar_not_registered_names_the_scalar_and_a_fix() -> None:
    error = ScalarNotRegisteredError("Money")
    text = str(error)
    assert "Money" in text
    assert "ScalarRegistry.register" in text


def test_graphql_field_error_ambiguous_names_both_spellings() -> None:
    error = GraphQLFieldError.ambiguous("User", "user_id", ("userID", "userId"))
    text = str(error)
    assert "userID" in text
    assert "userId" in text
    assert "ambiguous" in text


def test_graphql_field_error_ambiguous_names_every_colliding_spelling() -> None:
    error = GraphQLFieldError.ambiguous(
        "User", "user_id", ("userId", "userID", "user_Id")
    )
    text = str(error)
    assert "userID, userId and user_Id" in text


@pytest.mark.parametrize(
    ("leaf", "parent"),
    [
        (GraphQLClientError, GraphQLTestError),
        (OperationNotFoundError, GraphQLClientError),
        (ArgumentError, GraphQLClientError),
        (SelectionError, GraphQLClientError),
        (SelectionTooLargeError, GraphQLClientError),
        (SchemaError, GraphQLClientError),
        (ScalarNotRegisteredError, GraphQLClientError),
        (GraphQLTransportError, GraphQLTestError),
        (GraphQLConnectionError, GraphQLTransportError),
        (GraphQLTimeoutError, GraphQLTransportError),
        (GraphQLHTTPStatusError, GraphQLTransportError),
        (GraphQLExecutionError, GraphQLTestError),
        (GraphQLPartialDataError, GraphQLExecutionError),
        (GraphQLFieldError, GraphQLTestError),
        (WaitTimeoutError, GraphQLTestError),
        (ExpectedErrorNotRaised, GraphQLTestError),
    ],
)
def test_hierarchy_matches_spec_8_1(
    leaf: type[BaseException], parent: type[BaseException]
) -> None:
    assert issubclass(leaf, parent)
    assert issubclass(leaf, GraphQLTestError)
