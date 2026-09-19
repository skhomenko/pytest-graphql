"""Schema-aware materialization (C5).

The materializer holds the schema and the document that was sent. It answers
two questions about a JSON value: is it the shape its declared type says
(``check``, run once and eagerly at the boundary), and what should a caller
see when reading it (``wrap``, run lazily, one field at a time).

The GraphQL type of every value comes from walking the selection set beside
the JSON, never from the JSON's shape. Only object, interface and union
values become ``Node``. Only a list whose elements are such values becomes
``NodeList``. A custom scalar is never wrapped: it goes to its registered
parse function, or stays the raw JSON value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLEnumType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    is_abstract_type,
    is_composite_type,
    is_object_type,
    is_specified_scalar_type,
)

from pytest_graphql._core.errors import ResponseShapeError
from pytest_graphql._core.response.node import Node, NodeList

#: Object and list nesting a response may reach before it is refused. A
#: document's own depth is capped upstream by ``max_depth``, but ``fields=``
#: can be deeper, and a hostile server can nest lists as deep as JSON allows.
MAX_DEPTH = 128

ScalarParsers = Mapping[str, Callable[[Any], Any]]

_TYPENAME_TYPE = GraphQLNonNull(GraphQLString)


@dataclass(frozen=True)
class FieldPlan:
    """One response key of an object: its schema field, type and sub-selections."""

    field_name: str
    type_: GraphQLOutputType
    selections: tuple[SelectionSetNode, ...]


def _unwrap_non_null(type_: GraphQLOutputType) -> GraphQLOutputType:
    return type_.of_type if isinstance(type_, GraphQLNonNull) else type_


def _kind(value: Any) -> str:
    """The JSON kind of ``value``, for a message that must not echo the value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_BUILTIN_SCALAR_CHECKS: dict[str, Callable[[Any], bool]] = {
    "String": lambda v: isinstance(v, str),
    "ID": lambda v: isinstance(v, str) or _is_int(v),
    "Int": _is_int,
    "Float": lambda v: _is_int(v) or isinstance(v, float),
    "Boolean": lambda v: isinstance(v, bool),
}


class Materializer:
    """Walks one document's selection sets against a schema."""

    def __init__(
        self,
        schema: GraphQLSchema,
        document: DocumentNode,
        parsers: ScalarParsers | None = None,
        *,
        operation_name: str | None = None,
    ) -> None:
        self.schema = schema
        self.parsers = dict(parsers or {})
        self._fragments = {
            definition.name.value: definition
            for definition in document.definitions
            if isinstance(definition, FragmentDefinitionNode)
        }
        self._operation = self._pick_operation(document, operation_name)
        self._plans: dict[
            tuple[tuple[int, ...], str, str | None], Mapping[str, FieldPlan]
        ] = {}

    @staticmethod
    def _pick_operation(
        document: DocumentNode, operation_name: str | None
    ) -> OperationDefinitionNode:
        operations = [
            definition
            for definition in document.definitions
            if isinstance(definition, OperationDefinitionNode)
        ]
        for operation in operations:
            name = operation.name.value if operation.name else None
            if operation_name is None or name == operation_name:
                return operation
        raise ValueError("the document has no matching operation definition")

    # -- entry points -------------------------------------------------------

    def root_type(self) -> GraphQLObjectType:
        kind = self._operation.operation.value
        root = {
            "query": self.schema.query_type,
            "mutation": self.schema.mutation_type,
            "subscription": self.schema.subscription_type,
        }[kind]
        if root is None:
            raise ResponseShapeError(f"the schema has no {kind} type.", path=())
        return root

    def check_data(self, data: Any) -> None:
        """Validate ``data`` against the operation's root type, or raise."""
        root = self.root_type()
        self.check(data, root, (self._operation.selection_set,), ())

    def wrap_data(self, data: Any) -> Any:
        """The caller-facing form of ``data``: a ``Node`` over the root type."""
        return self.wrap(data, self.root_type(), (self._operation.selection_set,))

    # -- plans --------------------------------------------------------------

    def object_plan(
        self,
        parent: GraphQLNamedType,
        selections: tuple[SelectionSetNode, ...],
        runtime: str | None,
    ) -> Mapping[str, FieldPlan]:
        key = (tuple(id(s) for s in selections), parent.name, runtime)
        cached = self._plans.get(key)
        if cached is not None:
            return cached
        entries: dict[str, list[Any]] = {}
        visited: set[str] = set()
        for selection_set in selections:
            self._collect(selection_set, parent, runtime, entries, visited)
        plan = {
            response_key: FieldPlan(name, type_, tuple(sets))
            for response_key, (name, type_, sets) in entries.items()
        }
        self._plans[key] = plan
        return plan

    def _collect(
        self,
        selection_set: SelectionSetNode,
        parent: GraphQLNamedType,
        runtime: str | None,
        entries: dict[str, list[Any]],
        visited: set[str],
    ) -> None:
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                name = selection.name.value
                response_key = selection.alias.value if selection.alias else name
                if name == "__typename":
                    type_: GraphQLOutputType | None = _TYPENAME_TYPE
                elif isinstance(parent, (GraphQLObjectType, GraphQLInterfaceType)):
                    definition = parent.fields.get(name)
                    type_ = definition.type if definition else None
                else:
                    type_ = None
                if type_ is None:
                    continue
                entry = entries.setdefault(response_key, [name, type_, []])
                if selection.selection_set is not None:
                    entry[2].append(selection.selection_set)
            elif isinstance(selection, InlineFragmentNode):
                condition = (
                    selection.type_condition.name.value
                    if selection.type_condition
                    else None
                )
                self._descend(
                    selection.selection_set,
                    condition,
                    parent,
                    runtime,
                    entries,
                    visited,
                )
            elif isinstance(selection, FragmentSpreadNode):
                name = selection.name.value
                fragment = self._fragments.get(name)
                if fragment is None or name in visited:
                    continue
                visited.add(name)
                self._descend(
                    fragment.selection_set,
                    fragment.type_condition.name.value,
                    parent,
                    runtime,
                    entries,
                    visited,
                )

    def _descend(
        self,
        selection_set: SelectionSetNode,
        condition: str | None,
        parent: GraphQLNamedType,
        runtime: str | None,
        entries: dict[str, list[Any]],
        visited: set[str],
    ) -> None:
        target = parent
        if condition is not None:
            condition_type = self.schema.get_type(condition)
            if condition_type is None or not is_composite_type(condition_type):
                return
            if runtime is not None and not self._applies(condition_type, runtime):
                return
            target = condition_type
        self._collect(selection_set, target, runtime, entries, visited)

    def _applies(self, condition: GraphQLNamedType, runtime: str) -> bool:
        if condition.name == runtime:
            return True
        runtime_type = self.schema.get_type(runtime)
        if runtime_type is None or not is_abstract_type(condition):
            return False
        return self.schema.is_sub_type(condition, runtime_type)  # type: ignore[arg-type]

    def runtime_name(
        self,
        value: Mapping[str, Any],
        named: GraphQLNamedType,
        path: tuple[str | int, ...] = (),
    ) -> str | None:
        """The concrete type name of an object value, or ``None`` if unknown."""
        if is_object_type(named):
            return named.name
        typename = value.get("__typename")
        if typename is None:
            return None
        possible = self.schema.get_type(typename) if isinstance(typename, str) else None
        if (
            possible is None
            or not is_object_type(possible)
            or not self.schema.is_sub_type(named, possible)  # type: ignore[arg-type]
        ):
            raise ResponseShapeError(
                f"'__typename' at {_render_path(path)} is not a possible type of "
                f"{named.name}.",
                path=path,
            )
        return possible.name

    # -- validation ---------------------------------------------------------

    def check(
        self,
        value: Any,
        type_: GraphQLOutputType,
        selections: tuple[SelectionSetNode, ...],
        path: tuple[str | int, ...],
    ) -> None:
        declared = type_
        type_ = _unwrap_non_null(type_)
        if value is None:
            return
        if isinstance(type_, GraphQLList):
            if not isinstance(value, list):
                raise _mismatch(declared, value, path)
            for index, item in enumerate(value):
                self.check(item, type_.of_type, selections, (*path, index))
            return
        if isinstance(type_, GraphQLEnumType):
            if not isinstance(value, str):
                raise _mismatch(declared, value, path)
            return
        if isinstance(type_, GraphQLScalarType):
            test = (
                _BUILTIN_SCALAR_CHECKS.get(type_.name)
                if is_specified_scalar_type(type_)
                else None
            )
            if test is not None and not test(value):
                raise _mismatch(declared, value, path)
            return
        if not isinstance(value, dict):
            raise _mismatch(declared, value, path)
        runtime = self.runtime_name(value, type_, path)  # type: ignore[arg-type]
        plan = self.object_plan(type_, selections, runtime)  # type: ignore[arg-type]
        for response_key, field_plan in plan.items():
            if response_key in value:
                self.check(
                    value[response_key],
                    field_plan.type_,
                    field_plan.selections,
                    (*path, response_key),
                )

    # -- wrapping -----------------------------------------------------------

    def wrap(
        self,
        value: Any,
        type_: GraphQLOutputType,
        selections: tuple[SelectionSetNode, ...],
    ) -> Any:
        type_ = _unwrap_non_null(type_)
        if value is None:
            return None
        if isinstance(type_, GraphQLList):
            if not self._needs_wrap(type_.of_type):
                return value  # leaves with nothing to parse stay as received
            element = _unwrap_non_null(type_.of_type)
            items = [self.wrap(item, type_.of_type, selections) for item in value]
            if is_composite_type(element):
                return NodeList(items)
            return items
        if isinstance(type_, GraphQLScalarType):
            parse = self.parsers.get(type_.name)
            if parse is None or is_specified_scalar_type(type_):
                return value
            return parse(value)
        if isinstance(type_, GraphQLEnumType):
            return value
        return Node(value, self, type_, selections)  # type: ignore[arg-type]

    def _needs_wrap(self, type_: GraphQLOutputType) -> bool:
        """Whether a value of ``type_`` differs from its raw JSON once wrapped."""
        named = get_named_type(type_)
        if is_composite_type(named):
            return True
        return (
            isinstance(named, GraphQLScalarType)
            and not is_specified_scalar_type(named)
            and named.name in self.parsers
        )


def _render_path(path: tuple[str | int, ...], root: str = "data") -> str:
    if not path:
        return root
    return root + "." + ".".join(str(segment) for segment in path)


def check_json_depth(value: Any, root: str = "data") -> None:
    """Refuse a JSON tree nested deeper than ``MAX_DEPTH`` containers.

    This is a structural guard, separate from schema validation, so a custom
    scalar, an unselected key and an ``errors`` or ``extensions`` entry are all
    bounded too. It follows every ``Mapping`` and every list or tuple, because
    a custom ``Transport`` may return any of them. It walks with an explicit
    stack: it must not itself recurse on the input it is protecting against.
    """
    stack: list[tuple[Any, tuple[str | int, ...]]] = [(value, ())]
    while stack:
        item, path = stack.pop()
        if isinstance(item, Mapping):
            children: Any = item.items()
        elif isinstance(item, (list, tuple)):
            children = enumerate(item)
        else:
            continue
        if len(path) >= MAX_DEPTH:
            raise ResponseShapeError(
                f"the response nests deeper than {MAX_DEPTH} levels at "
                f"{_render_path(path, root)}.",
                path=path,
            )
        stack.extend(
            (child, (*path, key))
            for key, child in children
            if isinstance(child, (Mapping, list, tuple))
        )


def _mismatch(
    type_: GraphQLOutputType, value: Any, path: tuple[str | int, ...]
) -> ResponseShapeError:
    return ResponseShapeError(
        f"expected {type_} at {_render_path(path)}, got a JSON {_kind(value)}.",
        path=path,
    )
