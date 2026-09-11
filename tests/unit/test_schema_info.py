"""Helpers over a graphql-core ``GraphQLSchema`` (SPEC 6, schema/info.py)."""

from __future__ import annotations

from graphql import (
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
)

from pytest_graphql._core.schema import info
from tests.schema.resolvers import build_schema


def _schema() -> GraphQLSchema:
    return build_schema()


def test_operation_names_lists_query_fields_in_schema_order() -> None:
    names = info.operation_names(_schema(), "query")
    assert names[:3] == ["node", "user", "users"]
    assert "matrix" in names
    assert len(names) == 16


def test_operation_names_lists_mutation_fields_in_schema_order() -> None:
    names = info.operation_names(_schema(), "mutation")
    assert names == ["updateUser", "createPost", "movePost"]


def test_operation_names_empty_for_absent_subscription_root() -> None:
    assert info.operation_names(_schema(), "subscription") == []


def test_field_names_on_an_object_type() -> None:
    user_type = _schema().type_map["User"]
    assert isinstance(user_type, GraphQLObjectType)
    fields = info.field_names(user_type)
    assert "userId" in fields
    assert "user_id" in fields


def test_input_field_names() -> None:
    create_post_input = _schema().type_map["CreatePostInput"]
    assert isinstance(create_post_input, GraphQLInputObjectType)
    assert info.input_field_names(create_post_input) == ["title", "authorId"]


def test_possible_type_names_for_a_union() -> None:
    schema = _schema()
    search_result = schema.type_map["SearchResult"]
    assert isinstance(search_result, GraphQLUnionType)
    names = info.possible_type_names(schema, search_result)
    assert set(names) == {"User", "Team", "Attachment"}


def test_possible_type_names_for_an_interface() -> None:
    schema = _schema()
    node = schema.type_map["Node"]
    assert isinstance(node, GraphQLInterfaceType)
    names = info.possible_type_names(schema, node)
    assert set(names) == {"User", "Team", "Post"}
