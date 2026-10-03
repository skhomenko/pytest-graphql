"""``gql.fake``: input factories validated against the schema (SPEC 3.7).

``fake.CreateUserInput(email="a@b.c")`` builds a plain ``dict`` for that input
type. An unknown override raises ``SelectionError`` when the call is made, so a
typo fails at the line that has it.

The namespace takes its inputs explicitly: the global seed, the node id and a
:class:`UniqueSource`, which carries the run id and the worker id. It reads
none of them from the environment. How a client supplies them is decided where
the client is built, never here.

Names follow B10: an override key may be the exact schema spelling or its
snake_case form, the exact name wins, and a snake form shared by two fields is
an error that names them. Type names are exact (B11). The result always uses
the exact schema names.
"""

from __future__ import annotations

from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
)

from pytest_graphql._core.errors import SchemaError, SelectionError
from pytest_graphql._core.factory.generate import Context, fake_input
from pytest_graphql._core.factory.scalars import ScalarRegistry
from pytest_graphql._core.factory.seed import derive_seed
from pytest_graphql._core.factory.unique import UniqueSource
from pytest_graphql._core.naming import NameMap

DEFAULT_DEPTH = 2


class TypeFactory:
    """What ``fake.CreateUserInput`` returns: call it to get a payload."""

    __slots__ = ("_base", "_names", "_type")

    def __init__(self, type_: GraphQLInputObjectType, base: Context) -> None:
        self._type = type_
        self._base = base
        self._names = NameMap.build(type_.fields)

    @property
    def type_name(self) -> str:
        return self._type.name

    def __call__(
        self,
        /,
        *,
        _required_only: bool = False,
        _depth: int = DEFAULT_DEPTH,
        **overrides: Any,
    ) -> dict[str, Any]:
        """Build a payload.

        ``_required_only`` skips nullable fields. ``_depth`` is how many
        levels of nested input objects are filled in full. Every other keyword
        overrides one field. An input field named ``_required_only`` or
        ``_depth`` cannot be overridden by keyword.
        """
        if not isinstance(_required_only, bool):
            raise TypeError(
                f"_required_only must be a bool, got {type(_required_only).__name__}."
            )
        if isinstance(_depth, bool) or not isinstance(_depth, int):
            raise TypeError(f"_depth needs an int, got {type(_depth).__name__}.")
        if _depth < 0:
            raise ValueError(f"_depth needs 0 or more, got {_depth}.")
        context = Context(
            seed=self._base.seed,
            node_id=self._base.node_id,
            scalars=self._base.scalars,
            unique_source=self._base.unique_source,
            required_only=_required_only,
            depth=_depth,
        )
        return fake_input(self._type, context, self._resolve(overrides))

    def _resolve(self, overrides: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        written_as: dict[str, str] = {}
        for written, value in overrides.items():
            exact = self._names.get(written)
            if exact is None:
                if self._names.is_ambiguous(written):
                    raise SelectionError.ambiguous_field(
                        self._type.name, written, self._names.ambiguous_names(written)
                    )
                raise SelectionError.unknown_field(
                    self._type.name, written, list(self._type.fields)
                )
            if exact in resolved:
                raise SelectionError(
                    f"{exact!r} was given twice on {self._type.name}, as both "
                    f"{written_as[exact]!r} and {written!r}.\n"
                    "  Use one spelling."
                )
            resolved[exact] = value
            written_as[exact] = written
        return resolved

    def __repr__(self) -> str:
        return f"fake.{self._type.name}"


class _State:
    """What a ``FakeNamespace`` holds. It is the namespace's only attribute.

    Every GraphQL name that does not start with ``__`` is a legal input type
    name, so a namespace attribute or method with any other name could shadow a
    type and hide it from ``getattr``. The namespace therefore keeps this one
    object under a name GraphQL reserves, and nothing else.
    """

    __slots__ = ("base", "cache", "schema")

    def __init__(self, schema: GraphQLSchema, base: Context) -> None:
        self.schema = schema
        self.base = base
        self.cache: dict[str, TypeFactory] = {}

    def is_input_name(self, name: str) -> bool:
        return isinstance(self.schema.type_map.get(name), GraphQLInputObjectType)

    def input_type(self, name: str) -> GraphQLInputObjectType:
        found = self.schema.type_map.get(name)
        if found is None:
            raise SchemaError.unknown_type(name, self.names())
        if not isinstance(found, GraphQLInputObjectType):
            raise SchemaError(
                f"{name!r} is {_describe_kind(found)}, not an input object.\n"
                "  fake builds input objects only."
            )
        return found

    def names(self) -> list[str]:
        return sorted(
            name
            for name, type_ in self.schema.type_map.items()
            if not name.startswith("__") and isinstance(type_, GraphQLInputObjectType)
        )


class FakeNamespace:
    """The ``gql.fake`` namespace over one schema.

    An input type is an attribute. The class has no attribute or method of its
    own besides ``__state__`` and the dunder methods, because GraphQL reserves
    the ``__`` prefix, so no type can share a name with any of them.
    """

    __slots__ = ("__state__",)

    def __init__(
        self,
        schema: GraphQLSchema,
        scalars: ScalarRegistry | None,
        *,
        global_seed: int,
        node_id: str,
        unique_source: UniqueSource,
    ) -> None:
        if not isinstance(unique_source, UniqueSource):
            raise TypeError(
                "unique_source must be a UniqueSource, got "
                f"{type(unique_source).__name__}."
            )
        if scalars is not None and not isinstance(scalars, ScalarRegistry):
            raise TypeError(
                "scalars must be a ScalarRegistry or None, got "
                f"{type(scalars).__name__}."
            )
        base = Context(
            seed=derive_seed(global_seed, node_id),
            node_id=node_id,
            scalars=scalars if scalars is not None else ScalarRegistry(),
            unique_source=unique_source,
            required_only=False,
            depth=DEFAULT_DEPTH,
        )
        self.__state__ = _State(schema, base)

    def __getattr__(self, name: str) -> TypeFactory:
        # GraphQL reserves the ``__`` prefix, so a dunder is never a type. This
        # also stops a lookup of ``__state__`` before ``__init__`` has set it
        # (copy and pickle probe for it) from calling this method again.
        if name.startswith("__"):
            raise AttributeError(name)
        state = self.__state__
        cached = state.cache.get(name)
        if cached is None:
            # A single leading underscore is a legal type name, so it is looked
            # up like any other and refused only when the schema has no such
            # input type, which keeps introspection probes working.
            if name.startswith("_") and not state.is_input_name(name):
                raise AttributeError(name)
            cached = TypeFactory(state.input_type(name), state.base)
            state.cache[name] = cached
        return cached

    def __dir__(self) -> list[str]:
        return self.__state__.names()

    def __repr__(self) -> str:
        return f"FakeNamespace({len(self.__state__.names())} input types)"


def _describe_kind(type_: GraphQLNamedType) -> str:
    if isinstance(type_, GraphQLObjectType):
        return "an object type"
    if isinstance(type_, GraphQLInterfaceType):
        return "an interface"
    if isinstance(type_, GraphQLUnionType):
        return "a union"
    if isinstance(type_, GraphQLEnumType):
        return "an enum"
    return "a scalar"
