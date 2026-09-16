"""Operation assembly and validation (SPEC 8.2; D3, B10, B14, B15, B23).

M4's guarantee is that no invalid document leaves ``assemble_operation``.
Each test below is one row of SPEC 8.2's "before sending" table, or one
clause of the B15 auto-wrap rule, proven against the hostile schema.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from graphql import (
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLString,
    OperationDefinitionNode,
    OperationType,
    parse,
    print_ast,
    validate,
)
from graphql import (
    build_schema as build_sdl_schema,
)

from pytest_graphql._core.errors import (
    ArgumentError,
    OperationNotFoundError,
    SelectionError,
)
from pytest_graphql._core.operation import (
    assemble_operation,
    resolve_operation,
    resolve_root_arguments,
    type_node,
)
from pytest_graphql._core.selection.builder import SelectionBuilder
from pytest_graphql._core.selection.model import AUTO, Field
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()


# ---------------------------------------------------------------------------
# Operation name resolution (D3, B10)
# ---------------------------------------------------------------------------


def test_resolves_operation_by_exact_name() -> None:
    name, field_def = resolve_operation(SCHEMA, "query", "user")
    assert name == "user"
    assert "id" in field_def.args


def test_resolves_operation_by_snake_name() -> None:
    name, field_def = resolve_operation(SCHEMA, "query", "reserved_word_fields")
    assert name == "reservedWordFields"
    assert field_def.args == {}


def test_operation_not_found_raises_with_near_matches() -> None:
    with pytest.raises(OperationNotFoundError) as excinfo:
        resolve_operation(SCHEMA, "query", "usr")
    assert "no query named 'usr'" in str(excinfo.value)


def test_operation_not_found_for_wrong_kind() -> None:
    # 'user' is a query, not a mutation.
    with pytest.raises(OperationNotFoundError):
        resolve_operation(SCHEMA, "mutation", "user")


def test_operation_not_found_when_the_schema_has_no_such_root_type() -> None:
    # The hostile schema declares no Subscription type at all.
    with pytest.raises(OperationNotFoundError) as excinfo:
        resolve_operation(SCHEMA, "subscription", "anything")
    assert excinfo.value.total_count == 0


# ---------------------------------------------------------------------------
# Direct argument resolution: no input-object argument in play
# ---------------------------------------------------------------------------


def test_list_returning_operation_selects_on_the_item_type() -> None:
    # 'users' returns [User!]!; the selection applies to User, its item type.
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="users",
        kwargs={},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    text = print_ast(assembled.document)
    assert "users {\n    id\n  }" in text


def test_direct_two_scalar_arguments() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="updateUser",
        kwargs={"id": "U1", "name": "New Name"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    text = print_ast(assembled.document)
    assert "updateUser(id: $id, name: $name)" in text
    assert assembled.variables == {"id": "U1", "name": "New Name"}
    assert assembled.field_name == "updateUser"


def test_unknown_argument_raises_with_signature_and_candidates() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={"id2": "U1"},
            fields=AUTO,
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
        )
    message = str(excinfo.value)
    assert "query 'user' has no argument 'id2'" in message
    assert "Signature: user(id: ID!): User" in message
    assert "Did you mean 'id'?" in message


def test_missing_required_argument_raises() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={},
            fields=AUTO,
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
        )
    message = str(excinfo.value)
    assert "query 'user' is missing required argument 'id'" in message
    assert "Signature: user(id: ID!): User" in message


# ---------------------------------------------------------------------------
# B15 auto-wrap: createPost(input: CreatePostInput!) is the single-argument,
# single-input-object case.
# ---------------------------------------------------------------------------


def test_auto_wrap_wraps_flat_kwargs_into_the_input_object() -> None:
    resolved = resolve_root_arguments(
        kind="mutation",
        operation_name="createPost",
        field_def=SCHEMA.mutation_type.fields["createPost"],  # type: ignore[union-attr]
        candidates={"title": "Hello", "author_id": "U1"},
    )
    assert resolved == {"input": {"title": "Hello", "authorId": "U1"}}


def test_auto_wrap_end_to_end_document_shape() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="createPost",
        kwargs={"title": "Hello", "authorId": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    text = print_ast(assembled.document)
    assert "createPost(input: $input)" in text
    assert assembled.variables == {"input": {"title": "Hello", "authorId": "U1"}}


def test_explicit_input_argument_wins_over_auto_wrap() -> None:
    resolved = resolve_root_arguments(
        kind="mutation",
        operation_name="createPost",
        field_def=SCHEMA.mutation_type.fields["createPost"],  # type: ignore[union-attr]
        candidates={"input": {"title": "Hello", "authorId": "U1"}},
    )
    assert resolved == {"input": {"title": "Hello", "authorId": "U1"}}


def test_unmatched_kwarg_under_auto_wrap_lists_both_name_sets() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        resolve_root_arguments(
            kind="mutation",
            operation_name="createPost",
            field_def=SCHEMA.mutation_type.fields["createPost"],  # type: ignore[union-attr]
            candidates={"title": "Hello", "bogus": "x"},
        )
    message = str(excinfo.value)
    assert "mutation 'createPost' has no argument 'bogus'" in message
    assert "CreatePostInput fields: title, authorId" in message


def test_exact_and_snake_spelling_of_the_same_wrapped_field_raises() -> None:
    # 'authorId' and 'author_id' both resolve to CreatePostInput.authorId.
    # Silently keeping one and dropping the other would lose a value the
    # caller wrote, so this is refused rather than picked arbitrarily.
    with pytest.raises(ArgumentError) as excinfo:
        resolve_root_arguments(
            kind="mutation",
            operation_name="createPost",
            field_def=SCHEMA.mutation_type.fields["createPost"],  # type: ignore[union-attr]
            candidates={"title": "Hi", "authorId": "X", "author_id": "Y"},
        )
    message = str(excinfo.value)
    assert "received argument 'authorId' twice" in message
    assert "'authorId'" in message
    assert "'author_id'" in message


def test_no_candidate_matching_the_input_falls_back_to_direct_resolution() -> None:
    # Rule 3 fails outright: nothing supplied matches CreatePostInput's own
    # fields either, so wrapping never triggers and this falls through to
    # ordinary resolution against createPost's own arguments, which also
    # does not have a 'bogus' argument.
    with pytest.raises(ArgumentError) as excinfo:
        resolve_root_arguments(
            kind="mutation",
            operation_name="createPost",
            field_def=SCHEMA.mutation_type.fields["createPost"],  # type: ignore[union-attr]
            candidates={"bogus": "x"},
        )
    message = str(excinfo.value)
    assert "mutation 'createPost' has no argument 'bogus'" in message
    assert "Signature: createPost(input: CreatePostInput!): Post!" in message
    assert "CreatePostInput fields: title, authorId" in message


# ---------------------------------------------------------------------------
# B15: movePost(id: ID!, data: MovePostInput!) mixes a scalar operation
# argument with an input-object one, so wrapping never applies (11.1).
# ---------------------------------------------------------------------------


def test_mixed_arguments_disable_auto_wrap() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        resolve_root_arguments(
            kind="mutation",
            operation_name="movePost",
            field_def=SCHEMA.mutation_type.fields["movePost"],  # type: ignore[union-attr]
            candidates={"id": "P1", "direction": "up"},
        )
    message = str(excinfo.value)
    assert "mutation 'movePost' has no argument 'direction'" in message
    assert "MovePostInput fields: teamId" in message


def test_mixed_arguments_pass_explicitly() -> None:
    resolved = resolve_root_arguments(
        kind="mutation",
        operation_name="movePost",
        field_def=SCHEMA.mutation_type.fields["movePost"],  # type: ignore[union-attr]
        candidates={"id": "P1", "data": {"teamId": "T1"}},
    )
    assert resolved == {"id": "P1", "data": {"teamId": "T1"}}


# ---------------------------------------------------------------------------
# Scalar-returning root fields: no sub-selection is possible.
# ---------------------------------------------------------------------------


def test_scalar_returning_operation_has_no_selection_set() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="pingScalar",
        kwargs={},
        fields=AUTO,
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    text = print_ast(assembled.document)
    lines = [line.strip() for line in text.splitlines()]
    assert "pingScalar" in lines
    assert not any(line.startswith("pingScalar ") for line in lines)


def test_explicit_fields_on_scalar_returning_operation_raises() -> None:
    with pytest.raises(SelectionError) as excinfo:
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="pingScalar",
            kwargs={},
            fields=Field("nope"),
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
        )
    assert "has no fields to select" in str(excinfo.value)


# ---------------------------------------------------------------------------
# B14: value coercion against the declared type.
# ---------------------------------------------------------------------------


def test_invalid_variable_value_raises_argument_error() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={"id": {"x": 1}},
            fields=Field("id"),
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
        )
    message = str(excinfo.value)
    assert "query 'user' received an invalid value for argument 'id'" in message


def test_valid_variable_values_coerce_cleanly() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="createPost",
        kwargs={"title": "Hi", "authorId": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    assert assembled.variables["input"] == {"title": "Hi", "authorId": "U1"}


# ---------------------------------------------------------------------------
# Document shape: assembled, valid, and end-to-end consistent.
# ---------------------------------------------------------------------------


def test_assembled_document_parses_and_validates() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="user",
        kwargs={"id": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    text = print_ast(assembled.document)
    reparsed = parse(text)
    assert validate(SCHEMA, reparsed) == []
    operation = cast(OperationDefinitionNode, assembled.document.definitions[0])
    assert operation.operation == OperationType.QUERY


def test_validate_false_skips_local_document_validation() -> None:
    # A valid call still assembles correctly with validate=False; the point
    # is that graphql-core's own validate() is never invoked on this path.
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="user",
        kwargs={"id": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
        validate=False,
    )
    assert assembled.variables == {"id": "U1"}


def test_operation_name_defaults_to_field_name() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="user",
        kwargs={"id": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    operation = cast(OperationDefinitionNode, assembled.document.definitions[0])
    assert operation.name is not None
    assert operation.name.value == "user"


def test_custom_operation_name_is_used_and_validated() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="query",
        name="user",
        kwargs={"id": "U1"},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
        operation_name="GetUser",
    )
    operation = cast(OperationDefinitionNode, assembled.document.definitions[0])
    assert operation.name is not None
    assert operation.name.value == "GetUser"

    with pytest.raises(SelectionError):
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={"id": "U1"},
            fields=Field("id"),
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
            operation_name="not a name!",
        )


def test_explicit_empty_operation_name_is_rejected_not_defaulted() -> None:
    # An explicit "" is a caller mistake, not the same as leaving the
    # argument unset, so it must not silently fall back to the field name.
    with pytest.raises(SelectionError):
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={"id": "U1"},
            fields=Field("id"),
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
            operation_name="",
        )


def test_explicit_variables_dict_merges_and_wins() -> None:
    assembled = assemble_operation(
        schema=SCHEMA,
        kind="mutation",
        name="updateUser",
        kwargs={"id": "wrong", "variables": {"id": "U1"}},
        fields=Field("id"),
        policy=SelectionPolicy(),
        builder=SelectionBuilder(SCHEMA),
    )
    assert assembled.variables["id"] == "U1"


# ---------------------------------------------------------------------------
# B23: a schema argument name that collides with a reserved per-call option.
# The hostile schema has no such argument by design, so a small dedicated
# schema exercises this one.
# ---------------------------------------------------------------------------

_COLLISION_SCHEMA = build_sdl_schema(
    """
    type Query {
      probe(timeout: Int!, headers: String): Boolean!
    }
    """
)


def test_reserved_name_collision_raises_when_not_supplied_explicitly() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=_COLLISION_SCHEMA,
            kind="query",
            name="probe",
            kwargs={"timeout": 5},
            fields=AUTO,
            policy=SelectionPolicy(),
            builder=SelectionBuilder(_COLLISION_SCHEMA),
        )
    message = str(excinfo.value)
    assert "collides with a reserved per-call option name" in message
    assert "variables={'timeout': ...}" in message


def test_reserved_name_collision_is_avoided_through_variables_dict() -> None:
    assembled = assemble_operation(
        schema=_COLLISION_SCHEMA,
        kind="query",
        name="probe",
        kwargs={"variables": {"timeout": 5}},
        fields=AUTO,
        policy=SelectionPolicy(),
        builder=SelectionBuilder(_COLLISION_SCHEMA),
    )
    assert assembled.variables == {"timeout": 5}


def test_optional_colliding_argument_named_by_a_bare_kwarg_raises() -> None:
    # 'headers' is optional, but headers="x" here is always captured as the
    # per-call headers option, never reaching the 'headers' argument. Without
    # this check that value would silently vanish instead of erroring.
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=_COLLISION_SCHEMA,
            kind="query",
            name="probe",
            kwargs={"variables": {"timeout": 5}, "headers": "x"},
            fields=AUTO,
            policy=SelectionPolicy(),
            builder=SelectionBuilder(_COLLISION_SCHEMA),
        )
    assert "'headers'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Malformed input this module refuses cleanly rather than crashing on.
# ---------------------------------------------------------------------------

_DUPLICATE_SCHEMA = build_sdl_schema(
    """
    type Query {
      probe(userId: ID!): Boolean!
    }
    """
)


def test_exact_and_snake_spelling_of_a_direct_argument_raises() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        resolve_root_arguments(
            kind="query",
            operation_name="probe",
            field_def=_DUPLICATE_SCHEMA.query_type.fields["probe"],  # type: ignore[union-attr]
            candidates={"userId": "A", "user_id": "B"},
        )
    message = str(excinfo.value)
    assert "received argument 'userId' twice" in message


def test_non_mapping_variables_option_raises_cleanly() -> None:
    with pytest.raises(ArgumentError) as excinfo:
        assemble_operation(
            schema=SCHEMA,
            kind="query",
            name="user",
            kwargs={"id": "U1", "variables": "not-a-mapping"},
            fields=Field("id"),
            policy=SelectionPolicy(),
            builder=SelectionBuilder(SCHEMA),
        )
    assert "variables= escape hatch must be a mapping" in str(excinfo.value)


# ---------------------------------------------------------------------------
# type_node: the reverse of graphql-core's own type parsing.
# ---------------------------------------------------------------------------


def test_type_node_handles_named_list_and_non_null_combinations() -> None:
    assert print_type_node(GraphQLString) == "String"
    assert print_type_node(GraphQLNonNull(GraphQLInt)) == "Int!"
    assert print_type_node(GraphQLList(GraphQLString)) == "[String]"
    assert (
        print_type_node(GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLInt))))
        == "[Int!]!"
    )


def print_type_node(type_: Any) -> str:
    from graphql import print_ast

    return print_ast(type_node(type_))
