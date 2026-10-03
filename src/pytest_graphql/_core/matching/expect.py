"""``gql.expect``: object matchers validated against the schema (SPEC 3.6).

``expect.User(frist_name="John")`` raises ``SelectionError`` when it is built,
not when it is compared, so a typo in a test fails at the line that has it.
Only the field names of the named type are checked. A nested matcher, a plain
dict or a helper inside it carries no schema of its own.

Names follow B10: the exact schema spelling and its snake_case form both
resolve, and a snake form shared by two fields is an error that names them.
Type names are exact (B11).
"""

from __future__ import annotations

from typing import Any

from graphql import (
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    is_enum_type,
    is_input_object_type,
    is_scalar_type,
)

from pytest_graphql._core.errors import SchemaError, SelectionError
from pytest_graphql._core.matching.objects import Fields, ObjectMatcher
from pytest_graphql._core.naming import NameMap

TYPENAME = "__typename"

Composite = GraphQLObjectType | GraphQLInterfaceType | GraphQLUnionType


class TypeExpectation:
    """What ``expect.User`` returns: call it with field values to get a matcher."""

    __slots__ = ("_accepted", "_names", "_type")

    def __init__(self, schema: GraphQLSchema, type_: Composite) -> None:
        self._type = type_
        if isinstance(type_, GraphQLObjectType):
            self._accepted = frozenset({type_.name})
        else:
            members = schema.get_possible_types(type_)
            self._accepted = frozenset({type_.name, *(m.name for m in members)})
        self._names = (
            None if isinstance(type_, GraphQLUnionType) else NameMap.build(type_.fields)
        )

    @property
    def type_name(self) -> str:
        return self._type.name

    def __call__(self, /, **fields: Any) -> ObjectMatcher:
        resolved: list[tuple[str, str, Any]] = []
        for written, want in fields.items():
            resolved.append((written, self._resolve(written), want))
        return ObjectMatcher(self._type.name, tuple(resolved), accepted=self._accepted)

    def _resolve(self, written: str) -> str:
        if written == TYPENAME:
            return TYPENAME
        if self._names is None:
            raise SelectionError(
                f"{self._type.name} is a union, so it has no field {written!r}.\n"
                f"  A union matcher can only match {TYPENAME}; use the matcher "
                "of a member type for its fields."
            )
        exact = self._names.get(written)
        if exact is not None:
            return exact
        if self._names.is_ambiguous(written):
            raise SelectionError.ambiguous_field(
                self._type.name, written, self._names.ambiguous_names(written)
            )
        assert isinstance(self._type, (GraphQLObjectType, GraphQLInterfaceType))
        raise SelectionError.unknown_field(
            self._type.name, written, list(self._type.fields)
        )

    def __repr__(self) -> str:
        return f"expect.{self._type.name}"


class ExpectNamespace:
    """The ``gql.expect`` namespace over one schema."""

    __slots__ = ("_cache", "_schema")

    def __init__(self, schema: GraphQLSchema) -> None:
        self._schema = schema
        self._cache: dict[str, TypeExpectation] = {}

    def __getattr__(self, name: str) -> TypeExpectation:
        cached = self._cache.get(name)
        if cached is None:
            # GraphQL reserves the ``__`` prefix, so a dunder is never a type.
            # A single leading underscore is a legal type name, so it is looked
            # up like any other and refused only when the schema has no such
            # composite type, which keeps introspection probes working.
            if name.startswith("__") or (
                name.startswith("_") and not self._is_composite_name(name)
            ):
                raise AttributeError(name)
            cached = TypeExpectation(self._schema, self._composite(name))
            self._cache[name] = cached
        return cached

    def _is_composite_name(self, name: str) -> bool:
        return isinstance(
            self._schema.type_map.get(name),
            (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType),
        )

    def _composite(self, name: str) -> Composite:
        found = self._schema.type_map.get(name)
        if found is None:
            raise SchemaError.unknown_type(name, self._names())
        if not isinstance(
            found, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
        ):
            raise SchemaError(
                f"{name!r} is {_describe_kind(found)}, which has no fields to "
                "match.\n  expect needs an object, interface or union type."
            )
        return found

    def _names(self) -> list[str]:
        return sorted(
            name
            for name, type_ in self._schema.type_map.items()
            if not name.startswith("__")
            and isinstance(
                type_, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
            )
        )

    def __dir__(self) -> list[str]:
        return self._names()

    def __repr__(self) -> str:
        return f"ExpectNamespace({len(self._names())} types)"


def _describe_kind(type_: GraphQLNamedType) -> str:
    if is_enum_type(type_):
        return "an enum"
    if is_input_object_type(type_):
        return "an input object"
    if is_scalar_type(type_):
        return "a scalar"
    return "not a composite type"


__all__ = ["ExpectNamespace", "Fields", "TypeExpectation"]
