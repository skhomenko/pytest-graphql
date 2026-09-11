"""Small helpers over a graphql-core ``GraphQLSchema``.

Nothing here mutates a schema or talks to a transport. Each helper reads one
shape out of the schema object graphql-core already builds, in schema
declaration order, for callers that need field or type name lists: error
message rendering (``errors.py``), naming maps (``naming.py``), and the
selection engine (M3).
"""

from __future__ import annotations

from typing import Literal

from graphql import (
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
)

OperationKind = Literal["query", "mutation", "subscription"]


def root_type(schema: GraphQLSchema, kind: OperationKind) -> GraphQLObjectType | None:
    """Return the root operation type of the given kind, if the schema has one."""
    if kind == "query":
        return schema.query_type
    if kind == "mutation":
        return schema.mutation_type
    return schema.subscription_type


def operation_names(schema: GraphQLSchema, kind: OperationKind) -> list[str]:
    """Return the field names of the root operation type, in schema order."""
    type_ = root_type(schema, kind)
    if type_ is None:
        return []
    return list(type_.fields)


def field_names(type_: GraphQLObjectType | GraphQLInterfaceType) -> list[str]:
    """Return the field names of an object or interface type, in schema order."""
    return list(type_.fields)


def input_field_names(type_: GraphQLInputObjectType) -> list[str]:
    """Return the field names of an input object type, in schema order."""
    return list(type_.fields)


def possible_type_names(
    schema: GraphQLSchema, abstract_type: GraphQLInterfaceType | GraphQLUnionType
) -> list[str]:
    """Return the names of every concrete type that can satisfy an abstract type."""
    return [possible.name for possible in schema.get_possible_types(abstract_type)]
