"""The message of an error a caller can fix (DESIGN_DECISIONS.md section 13.5).

One test per mistake class. Each call passes a marker value next to the mistake,
and the test asserts the problem line, the suggestion, the fix line, and that the
marker is in no line of the message.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest
from graphql import GraphQLSchema, build_schema

from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLTestError,
    OperationNotFoundError,
    SchemaError,
    SelectionError,
    caller_message,
)
from pytest_graphql._core.schema.source import SDLFileSource
from pytest_graphql._core.selection.model import Selection
from tests.unit.scripted_steps import envelope, make_client

SDL = """
type Query {
  user(id: ID!): User
  users(first: Int): [User!]!
  find(raw: String!): User
  ping: String
}
type Mutation { createPost(input: CreatePostInput!): Post }
type Subscription { onUserAdded: User }
type User { id: ID!, name: String, email: String }
type Post { id: ID!, title: String }
input CreatePostInput { title: String!, authorId: ID! }
"""

#: A value that no message may repeat.
MARKER = "marker-value-4f2a9c"


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema(SDL)


def lines_of(error: GraphQLTestError) -> list[str]:
    message = str(error)
    assert MARKER not in message
    # A traceback prints the chain of causes as well, so check it as pytest does.
    assert MARKER not in "".join(traceback.format_exception(error))
    lines = message.splitlines()
    assert not lines[0].startswith(" ")
    assert lines[-1].startswith("  Fix: ")
    return lines


def test_the_builder_writes_the_problem_the_suggestion_and_the_fix() -> None:
    message = caller_message(
        "no thing named 'nme'.",
        context=["Things: id, name"],
        wrong="nme",
        known=["id", "name"],
        fix=lambda example: f"write {example}",
    )
    assert message.splitlines() == [
        "no thing named 'nme'.",
        "  Things: id, name",
        "  Did you mean: name?",
        "  Fix: write name",
    ]


def test_the_builder_leaves_out_the_suggestion_when_no_name_is_close() -> None:
    message = caller_message("no thing.", wrong="zzzz", known=["id"], fix="do it")
    assert message.splitlines() == ["no thing.", "  Fix: do it"]


def test_an_unknown_operation(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(OperationNotFoundError) as raised:
        client.query("usr", id=MARKER)
    lines = lines_of(raised.value)
    assert lines[0] == "no query named 'usr'."
    assert "  Did you mean: user, users?" in lines
    assert lines[-1].startswith("  Fix: pass the name of a query of the schema")


def test_an_unknown_argument(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.query("user", idd=MARKER)
    assert lines_of(raised.value) == [
        "query 'user' has no argument 'idd'.",
        "  Signature: user(id: ID!): User",
        "  Did you mean: id?",
        '  Fix: use an argument from the signature, such as gql.query("user", id=...)',
    ]


def test_an_unknown_field_in_fields(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(SelectionError) as raised:
        client.query("user", id=MARKER, fields=["nme"])
    assert lines_of(raised.value) == [
        "no field 'nme' on User.",
        "  Available fields: id, name, email",
        "  Did you mean: name?",
        '  Fix: use a field of User, such as fields=["name"]',
    ]


def test_an_unknown_input_field(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.mutation("createPost", titel=MARKER, author_id=MARKER)
    assert lines_of(raised.value) == [
        "mutation 'createPost' has no argument 'titel'.",
        "  Signature: createPost(input: CreatePostInput!): Post",
        "  CreatePostInput fields: title, authorId",
        "  Did you mean: title?",
        "  Fix: use an argument from the signature, such as "
        'gql.mutation("createPost", title=...)',
    ]


def test_a_value_of_the_wrong_type(schema: GraphQLSchema) -> None:
    client, transport = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.query("user", id={"inner": MARKER})
    assert lines_of(raised.value) == [
        "query 'user' received an invalid value for argument 'id'.",
        "  $id is not a valid value of type 'ID!'.",
        "  Fix: pass a value of type 'ID!' for $id",
    ]
    assert transport.sent == []


def test_a_missing_required_argument(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.query("user", headers={"x-token": MARKER})
    assert lines_of(raised.value) == [
        "query 'user' is missing required argument 'id'.",
        "  Signature: user(id: ID!): User",
        '  Fix: pass it as a keyword, such as gql.query("user", id=...)',
    ]


def test_a_subscription_through_query(schema: GraphQLSchema) -> None:
    client, transport = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.query("onUserAdded", headers={"x-token": MARKER})
    assert lines_of(raised.value) == [
        "query 'onUserAdded' is a subscription, and query() cannot run one.",
        "  Fix: subscriptions are not available in this release; call a query "
        "that reads the same data",
    ]
    assert transport.sent == []


def test_a_subscription_through_wait_until(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.wait_until("onUserAdded", until=bool)
    assert lines_of(raised.value) == [
        "wait_until polls queries only, and 'onUserAdded' is a subscription.",
        "  Fix: poll a query that reads the same data",
    ]


def test_an_argument_that_collides_with_an_option(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.query("find", headers={"x-token": MARKER})
    assert lines_of(raised.value) == [
        "query 'find' has an argument 'raw' that collides with a call option.",
        "  Signature: find(raw: String!): User",
        '  Fix: use variables={"raw": ...}',
    ]


def test_an_unknown_type_in_selection_of(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema)
    with pytest.raises(SchemaError) as raised:
        client.query("user", id=MARKER, fields=[Selection.of("Usre", "id")])
    assert lines_of(raised.value) == [
        "no type named 'Usre'.",
        "  Did you mean: User?",
        '  Fix: use the name of a type of the schema, such as Selection.of("User")',
    ]


def test_a_selection_text_that_does_not_parse_names_the_place_only(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema)
    with pytest.raises(SelectionError) as raised:
        client.query("user", id="u1", fields=f'id name "{MARKER}"')
    assert lines_of(raised.value) == [
        "could not parse the fields= text of User at line 1, column 9.",
        '  Fix: write field names as GraphQL text, as in fields="id name"',
    ]


def test_a_literal_that_the_schema_refuses_names_the_place_only(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema)
    with pytest.raises(SelectionError) as raised:
        client.execute('{ users(first: "' + MARKER + '") { id } }')
    assert lines_of(raised.value) == [
        "a literal value in the document is not valid for its place "
        "(line 1, column 16).",
        "  Fix: write the value as a variable and pass it in variables, "
        'as in gql.execute(document, {"name": ...})',
    ]
    assert transport.sent == []


def test_a_document_error_about_a_name_keeps_the_text_of_graphql_core(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema)
    with pytest.raises(SelectionError) as raised:
        client.execute("{ user(id: $id, nope: 1) { id } }")
    assert lines_of(raised.value)[0].startswith("Unknown argument 'nope'")


def test_an_sdl_file_that_does_not_parse_names_the_place_only(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.graphql"
    broken.write_text(
        f'type Query {{ a: String }}\ntype "{MARKER}"\n', encoding="utf-8"
    )
    with pytest.raises(SchemaError) as raised:
        SDLFileSource(broken).load()
    lines = lines_of(raised.value)
    assert lines[1] == "  syntax error at line 2, column 6"


def test_an_input_field_is_no_suggestion_when_input_is_given(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema)
    with pytest.raises(ArgumentError) as raised:
        client.mutation(
            "createPost", input={"title": "x", "authorId": "1"}, titel=MARKER
        )
    lines = lines_of(raised.value)
    assert lines == [
        "mutation 'createPost' has no argument 'titel'.",
        "  Signature: createPost(input: CreatePostInput!): Post",
        "  CreatePostInput fields: title, authorId",
        "  Fix: use an argument from the signature, such as "
        'gql.mutation("createPost", input=...)',
    ]


def test_the_input_field_suggestion_is_a_call_that_works(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(
        schema, envelope({"createPost": {"id": "p1", "title": "x"}})
    )
    with pytest.raises(ArgumentError) as raised:
        client.mutation("createPost", titel="x", author_id="1")
    assert 'gql.mutation("createPost", title=...)' in str(raised.value)
    client.mutation("createPost", title="x", author_id="1")


def test_an_operation_without_arguments_gets_no_invented_variables(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, envelope({"ping": "pong"}))
    with pytest.raises(ArgumentError) as raised:
        client.query("ping", variables=MARKER)
    assert lines_of(raised.value)[-1] == (
        "  Fix: remove variables=, because 'ping' takes no arguments"
    )
    assert client.query("ping") == "pong"
