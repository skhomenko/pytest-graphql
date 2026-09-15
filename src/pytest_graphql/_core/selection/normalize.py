"""Every selection input form, resolved against the schema, into one AST.

SPEC 3.3 gives four forms and calls them interchangeable, so they meet here
and nowhere else. The work happens in two passes, and the order matters.

The first pass collects. Names resolve against the schema by lookup (B10), so
both the exact schema name and its snake form reach the same field, and an
ambiguous snake form is an error rather than a guess. Entries that share a
response key merge, and two that share a key while differing in name,
arguments or type condition conflict and raise (C7). Merging has to happen
before anything is emitted, because merging emitted nodes would mean
comparing generated variable names that differ precisely because the nodes
were emitted twice.

The second pass emits. A user's argument value never reaches document text:
it is hoisted into a generated variable named from the field path and the
argument name (C7), so the document carries a variable reference and the
value travels beside it. Arguments written inside a raw GraphQL selection
string are the one thing left as typed, because there the user wrote the
document text themselves and there is no Python value to hoist.

``AUTO`` is a whole selection, not an item in a list. Where it appears, the
builder produces that position's selection and its variables are adopted
here. A position cannot be both automatic and explicit: that asks for two
different selection sets under one response key, and there is no defensible
way to merge them.

Each ``AUTO`` is also a scope (C56). The policy's depth, cycle and position
rules are measured from the generated selection's own root, so an explicit
prefix above it costs nothing and a reusable ``Selection`` means the same
thing wherever it is inserted. That is what lets a whole build be memoized on
the type and the policy alone. ``max_fields`` is the one policy value that
runs the other way: it guards document size, so this module charges every
explicit field and every spliced ``AUTO`` against one limit, reading each
generated selection's own reported count rather than rebuilding it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from graphql import (
    ArgumentNode,
    FieldNode,
    GraphQLCompositeType,
    GraphQLError,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    ListValueNode,
    NamedTypeNode,
    NameNode,
    NullValueNode,
    ObjectFieldNode,
    ObjectValueNode,
    SelectionNode,
    SelectionSetNode,
    Undefined,
    ValueNode,
    VariableNode,
    get_named_type,
    is_composite_type,
    parse,
    print_ast,
)
from graphql.language import FragmentSpreadNode
from graphql.pyutils import is_iterable
from graphql.utilities import ast_from_value, value_from_ast

from pytest_graphql._core.errors import (
    ArgumentError,
    SchemaError,
    SelectionError,
    SelectionTooLargeError,
)
from pytest_graphql._core.naming import NameMap
from pytest_graphql._core.selection.builder import (
    PAGE_SIZE_VARIABLE,
    TYPENAME,
    BuiltSelection,
    GeneratedVariable,
    SelectionBuilder,
    page_size_variable,
)
from pytest_graphql._core.selection.model import (
    Field,
    InlineFragment,
    Selection,
    SelectionInput,
    _Auto,
)
from pytest_graphql._core.selection.policy import (
    PAGE_SIZE_ARGUMENT,
    SelectionPolicy,
    is_connection_type,
    page_size_argument,
)


class VariableAllocator:
    """Names generated variables and keeps their values beside them.

    A name is derived from the field path and the argument name, which is
    unique for almost every argument in a document but not for all of them: a
    field ``a`` with an argument ``b_c`` and a field ``a_b`` with an argument
    ``c`` both want ``a_b_c``. The counter suffix settles that, and reserving
    the engine's own variable up front keeps a user's argument from taking
    its name.
    """

    def __init__(self, reserved: Sequence[str] = ()) -> None:
        self._taken: set[str] = set(reserved)
        self._generated: dict[str, GeneratedVariable] = {}

    def allocate(self, base: str, type_: Any, value: Any) -> str:
        name = base
        counter = 1
        while name in self._taken:
            counter += 1
            name = f"{base}_{counter}"
        self._taken.add(name)
        self._generated[name] = GeneratedVariable(name=name, type_=type_, value=value)
        return name

    def adopt(self, variable: GeneratedVariable) -> None:
        """Take over a variable the builder already made.

        One normalization runs under one policy, so every automatic selection
        in it names the same page-size variable with the same value. Adopting
        twice is therefore ordinary and the second is the same as the first.
        """
        self._taken.add(variable.name)
        self._generated.setdefault(variable.name, variable)

    @property
    def variables(self) -> tuple[GeneratedVariable, ...]:
        return tuple(self._generated.values())


@dataclass(frozen=True)
class _Argument:
    """One argument of a ``Field``, with its name resolved once.

    Resolution happens in the collecting pass because both passes need it: the
    signature compares by resolved name, and the emitter writes it. Doing it
    twice would also warn twice for a name that is both an exact schema name
    and another's snake form. ``resolved`` is ``None`` for a name the field
    does not have; the emitter raises for it, with the whole signature.
    """

    written: str
    resolved: str | None
    value: Any


@dataclass
class _Entry:
    """One response key at one level, before anything is emitted."""

    key: str
    name: str
    alias: str | None
    signature: str
    field_def: GraphQLField | None
    args: tuple[_Argument, ...] = ()
    arg_nodes: tuple[ArgumentNode, ...] = ()
    children: _Level | None = None
    auto: bool = False


@dataclass
class _Fragment:
    """One inline fragment at one level, keyed by its type condition."""

    type_name: str
    children: _Level | None = None
    auto: bool = False


@dataclass
class _Level:
    """One selection set: its fields by response key, its fragments by type."""

    fields: dict[str, _Entry] = field(default_factory=dict)
    fragments: dict[str, _Fragment] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.fields and not self.fragments


def normalize(
    fields: SelectionInput,
    *,
    schema: GraphQLSchema,
    parent_type: GraphQLCompositeType,
    policy: SelectionPolicy,
    builder: SelectionBuilder,
) -> BuiltSelection:
    """Turn any selection input into one selection set and its variables."""
    if isinstance(fields, _Auto):
        return builder.build(parent_type, policy)

    collector = _Collector(schema)
    level = collector.level(fields, parent_type, path=())
    if level.is_empty():
        raise SelectionError(
            f"the selection for {parent_type.name} is empty.\n"
            "  List at least one field, or pass fields=AUTO."
        )
    emitter = _Emitter(
        schema=schema, policy=policy, builder=builder, root_name=parent_type.name
    )
    selections = emitter.level(level, parent_type, path=())
    return BuiltSelection(
        selection_set=SelectionSetNode(selections=tuple(selections)),
        variables=emitter.allocator.variables,
        field_count=emitter.field_count,
    )


class _Collector:
    """The first pass: resolve names, merge by response key, apply removals."""

    def __init__(self, schema: GraphQLSchema) -> None:
        self._schema = schema

    def level(
        self,
        input_: SelectionInput,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> _Level:
        level = _Level()
        self._merge_into(level, input_, parent_type, path=path)
        return level

    def _merge_into(
        self,
        level: _Level,
        input_: SelectionInput,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        if isinstance(input_, _Auto):
            raise SelectionError(
                "AUTO is a whole selection, not one item in a list.\n"
                "  Write fields=AUTO, or Field(name, fields=AUTO) for one "
                "field."
            )
        if isinstance(input_, str):
            self._merge_raw(level, input_, parent_type, path=path)
            return
        if isinstance(input_, Field):
            self._merge_field(level, input_, parent_type, path=path)
            return
        if isinstance(input_, InlineFragment):
            self._merge_fragment(
                level, input_.on, input_.fields, parent_type, path=path
            )
            return
        if isinstance(input_, Selection):
            self._merge_selection(level, input_, parent_type, path=path)
            return
        if isinstance(input_, Mapping):
            for name, sub in input_.items():
                self._merge_field(
                    level, Field(name, fields=sub), parent_type, path=path
                )
            return
        if isinstance(input_, Sequence):
            for item in input_:
                self._merge_into(level, item, parent_type, path=path)
            return
        raise SelectionError(
            f"{type(input_).__name__} is not a selection.\n"
            "  Use AUTO, a list of field names, Field objects, a Selection, "
            "or a GraphQL selection string."
        )

    def _merge_selection(
        self,
        level: _Level,
        selection: Selection,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        """A Selection resolves on its own first, so its removals stay its own."""
        inner = _Level()
        for part in selection.parts:
            self._merge_into(inner, part, parent_type, path=path)
        for removal in selection.removals:
            _remove_path(inner, removal, parent_type.name)
        _merge_levels(level, inner)

    def _merge_field(
        self,
        level: _Level,
        field_: Field,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        if field_.on is not None:
            # A field carrying a type condition is that condition's fragment
            # holding that one field.
            self._merge_fragment(
                level,
                field_.on,
                Selection(
                    Field(
                        field_.name,
                        args=field_.args,
                        alias=field_.alias,
                        fields=field_.fields,
                    )
                ),
                parent_type,
                path=path,
            )
            return

        if field_.name == TYPENAME:
            entry = _Entry(
                key=field_.alias or TYPENAME,
                name=TYPENAME,
                alias=field_.alias,
                signature="",
                field_def=None,
            )
            _merge_entry(level, entry, parent_type.name)
            return

        name, definition = self._resolve_field(parent_type, field_.name)
        key = field_.alias or name
        arguments = _resolve_arguments(field_.args, definition)
        entry = _Entry(
            key=key,
            name=name,
            alias=field_.alias,
            signature=_python_args_signature(arguments, definition),
            field_def=definition,
            args=arguments,
        )
        child_type = get_named_type(definition.type)
        if field_.fields is not None:
            if not is_composite_type(child_type):
                raise SelectionError(
                    f"{parent_type.name}.{name} returns {child_type.name}, "
                    "which has no fields to select.\n"
                    f"  Write {name!r} on its own."
                )
            composite = _as_composite(child_type)
            if isinstance(field_.fields, _Auto):
                entry.auto = True
            else:
                entry.children = self.level(
                    field_.fields, composite, path=(*path, name)
                )
        _merge_entry(level, entry, parent_type.name)

    def _merge_fragment(
        self,
        level: _Level,
        type_name: str,
        fields: SelectionInput,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        fragment_type = self._resolve_fragment_type(type_name, parent_type)
        if isinstance(fields, _Auto):
            fragment = _Fragment(type_name=fragment_type.name, auto=True)
        else:
            fragment = _Fragment(
                type_name=fragment_type.name,
                children=self.level(fields, fragment_type, path=path),
            )
        _merge_fragment_into(level, fragment)

    def _merge_raw(
        self,
        level: _Level,
        text: str,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        """Form 4: a GraphQL selection string, parsed and then resolved."""
        try:
            document = parse("{" + text + "}")
        except Exception as error:  # graphql-core raises GraphQLSyntaxError
            raise SelectionError(
                f"could not parse the selection {text!r}.\n  {error}"
            ) from error
        definition = document.definitions[0]
        assert hasattr(definition, "selection_set")
        self._merge_ast(level, definition.selection_set, parent_type, path=path)

    def _merge_ast(
        self,
        level: _Level,
        selection_set: SelectionSetNode,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        for selection in selection_set.selections:
            if selection.directives:
                raise SelectionError(
                    "directives are not supported in v0.1.\n"
                    "  Send a document with directives through gql.execute(...) "
                    "instead."
                )
            if isinstance(selection, FragmentSpreadNode):
                raise SelectionError(
                    f"named fragment {selection.name.value!r} cannot be used "
                    "here, because v0.1 sends no fragment definitions.\n"
                    "  Inline it, or use Selection.of(TypeName)."
                )
            if isinstance(selection, InlineFragmentNode):
                self._merge_ast_fragment(level, selection, parent_type, path=path)
                continue
            assert isinstance(selection, FieldNode)
            self._merge_ast_field(level, selection, parent_type, path=path)

    def _merge_ast_fragment(
        self,
        level: _Level,
        node: InlineFragmentNode,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        # graphql-core types this as always present, but the grammar allows
        # "... { a b }", and the parser leaves the field unset for it.
        condition: NamedTypeNode | None = node.type_condition
        if condition is None:
            # No condition means the parent's own fields, inlined.
            self._merge_ast(level, node.selection_set, parent_type, path=path)
            return
        fragment_type = self._resolve_fragment_type(condition.name.value, parent_type)
        inner = _Level()
        self._merge_ast(
            inner,
            node.selection_set,
            fragment_type,
            path=path,
        )
        _merge_fragment_into(
            level, _Fragment(type_name=fragment_type.name, children=inner)
        )

    def _merge_ast_field(
        self,
        level: _Level,
        node: FieldNode,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> None:
        alias = node.alias.value if node.alias else None
        if node.name.value == TYPENAME:
            entry = _Entry(
                key=alias or TYPENAME,
                name=TYPENAME,
                alias=alias,
                signature="",
                field_def=None,
            )
            _merge_entry(level, entry, parent_type.name)
            return
        name, definition = self._resolve_field(parent_type, node.name.value)
        key = alias or name
        arguments = tuple(node.arguments)
        entry = _Entry(
            key=key,
            name=name,
            alias=alias,
            signature=_ast_args_signature(arguments, definition),
            field_def=definition,
            arg_nodes=arguments,
        )
        if node.selection_set is not None:
            child_type = get_named_type(definition.type)
            if not is_composite_type(child_type):
                raise SelectionError(
                    f"{parent_type.name}.{name} returns {child_type.name}, "
                    "which has no fields to select.\n"
                    f"  Write {name!r} on its own."
                )
            entry.children = _Level()
            self._merge_ast(
                entry.children,
                node.selection_set,
                _as_composite(child_type),
                path=(*path, name),
            )
        _merge_entry(level, entry, parent_type.name)

    def _resolve_field(
        self, parent_type: GraphQLCompositeType, written: str
    ) -> tuple[str, GraphQLField]:
        if isinstance(parent_type, GraphQLUnionType):
            raise SelectionError(
                f"{parent_type.name} is a union, so it has no field "
                f"{written!r}.\n"
                "  Select __typename, or use Selection.of(TypeName) for one "
                "of its members."
            )
        available = list(parent_type.fields)
        resolved = NameMap.build(available).get(written)
        if resolved is None:
            name_map = NameMap.build(available)
            if name_map.is_ambiguous(written):
                raise SelectionError(
                    f"{written!r} is ambiguous on {parent_type.name}: it "
                    f"matches {', '.join(name_map.ambiguous_names(written))}.\n"
                    "  Use the exact field name."
                )
            raise SelectionError.unknown_field(parent_type.name, written, available)
        return resolved, parent_type.fields[resolved]

    def _resolve_fragment_type(
        self, written: str, parent_type: GraphQLCompositeType
    ) -> GraphQLCompositeType:
        found = self._schema.type_map.get(written)
        if found is None or not is_composite_type(found):
            composite_names = [
                name
                for name, type_ in self._schema.type_map.items()
                if not name.startswith("__") and is_composite_type(type_)
            ]
            raise SchemaError.unknown_type(written, composite_names)
        fragment_type = _as_composite(found)
        if not self._possible_names(fragment_type) & self._possible_names(parent_type):
            raise SchemaError.not_a_possible_type(fragment_type.name, parent_type.name)
        return fragment_type

    def _possible_names(self, type_: GraphQLCompositeType) -> set[str]:
        if isinstance(type_, (GraphQLInterfaceType, GraphQLUnionType)):
            return {
                possible.name for possible in self._schema.get_possible_types(type_)
            }
        return {type_.name}


class _Emitter:
    """The second pass: AST nodes, with every user value hoisted to a variable."""

    def __init__(
        self,
        *,
        schema: GraphQLSchema,
        policy: SelectionPolicy,
        builder: SelectionBuilder,
        root_name: str,
    ) -> None:
        self._schema = schema
        self._policy = policy
        self._builder = builder
        self._root_name = root_name
        self.allocator = VariableAllocator(reserved=(PAGE_SIZE_VARIABLE,))
        self.field_count = 0

    def _count(self, fields: int = 1) -> None:
        """Charge fields against ``max_fields``, which guards the document.

        C56: the guard counts the complete normalized selection, so an
        explicit field and every field of every ``AUTO`` spliced into it are
        charged against one limit. A generated selection reports its own count
        (``BuiltSelection.field_count``), which is what lets a cached entry be
        composed without rebuilding it to find out how large it is.
        """
        self.field_count += fields
        if self.field_count > self._policy.max_fields:
            raise SelectionTooLargeError(
                type_name=self._root_name,
                field_count=self.field_count,
                limit=self._policy.max_fields,
            )

    def level(
        self,
        level: _Level,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> list[SelectionNode]:
        selections: list[SelectionNode] = []
        for entry in level.fields.values():
            selections.append(self._field(entry, parent_type, path=path))
        for fragment in level.fragments.values():
            selections.append(self._fragment(fragment, path=path))
        return selections

    def _field(
        self,
        entry: _Entry,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> FieldNode:
        # C56: a field path holds schema field names. A response key can be
        # an alias, which is the caller's label for the result and not part of
        # where the field sits in the schema.
        child_path = (*path, entry.name)
        if entry.name != TYPENAME:
            self._count()
        arguments = self._arguments(entry, parent_type, path=child_path)
        selection_set = None
        if entry.field_def is not None:
            named = get_named_type(entry.field_def.type)
            if is_composite_type(named):
                arguments = self._bound_connection(entry, named, arguments)
                selection_set = self._sub_selection(
                    entry, _as_composite(named), path=child_path
                )
        return FieldNode(
            alias=NameNode(value=entry.alias) if entry.alias else None,
            name=NameNode(value=entry.name),
            arguments=arguments,
            directives=(),
            selection_set=selection_set,
        )

    def _bound_connection(
        self,
        entry: _Entry,
        child_type: GraphQLNamedType,
        arguments: tuple[ArgumentNode, ...],
    ) -> tuple[ArgumentNode, ...]:
        """Apply C1's page-size bound to a generated connection selection.

        C1 makes the bound a property of the field that returns a connection,
        not of the code path that decided to expand it. The walk applies it
        when it crosses such a field itself; this is the other way in, where a
        caller named the field and asked for its selection with ``AUTO``. The
        caller's own page-size argument wins, which is what C1's "unless the
        caller already supplied one" means. It wins only over the generated
        value, never over the field-level condition that the field can be
        bounded at all.
        """
        if not self._policy.relay_aware or entry.field_def is None:
            return arguments
        if not _generates_connection_template(entry, child_type):
            return arguments
        if page_size_argument(entry.field_def) is None:
            # The field must be boundable before anything the caller wrote is
            # considered. A caller-supplied page size chooses the value; it
            # cannot make an argument of the wrong shape into a row bound.
            raise SelectionError(
                f"{entry.name!r} returns the connection {child_type.name}, and "
                f"it has no {PAGE_SIZE_ARGUMENT!r} argument of type Int or "
                "Int!, so the number of rows it returns cannot be bounded.\n"
                "  List the fields you need instead of asking for AUTO, or set "
                "relay_aware=False on the policy."
            )
        if any(argument.name.value == PAGE_SIZE_ARGUMENT for argument in arguments):
            return arguments
        self.allocator.adopt(page_size_variable(self._policy))
        return (
            *arguments,
            ArgumentNode(
                name=NameNode(value=PAGE_SIZE_ARGUMENT),
                value=VariableNode(name=NameNode(value=PAGE_SIZE_VARIABLE)),
            ),
        )

    def _sub_selection(
        self,
        entry: _Entry,
        child_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> SelectionSetNode:
        if entry.auto:
            return self._adopt(self._builder.build(child_type, self._policy))
        if entry.children is None or entry.children.is_empty():
            raise SelectionError(
                f"{entry.name!r} returns {child_type.name}, so it needs a "
                "sub-selection.\n"
                f'  Write {{"{entry.name}": ["id"]}}, or '
                f"Field({entry.name!r}, fields=AUTO)."
            )
        return SelectionSetNode(
            selections=tuple(self.level(entry.children, child_type, path=path))
        )

    def _fragment(
        self, fragment: _Fragment, *, path: tuple[str, ...]
    ) -> InlineFragmentNode:
        fragment_type = _as_composite(self._schema.type_map[fragment.type_name])
        if fragment.auto:
            selection_set = self._adopt(
                self._builder.build(fragment_type, self._policy)
            )
        else:
            assert fragment.children is not None
            selection_set = SelectionSetNode(
                selections=tuple(
                    self.level(fragment.children, fragment_type, path=path)
                )
            )
        return InlineFragmentNode(
            type_condition=NamedTypeNode(name=NameNode(value=fragment.type_name)),
            directives=(),
            selection_set=selection_set,
        )

    def _adopt(self, built: BuiltSelection) -> SelectionSetNode:
        """Splice a generated selection in, taking on its variables and size."""
        for variable in built.variables:
            self.allocator.adopt(variable)
        self._count(built.field_count)
        return built.selection_set

    def _arguments(
        self,
        entry: _Entry,
        parent_type: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
    ) -> tuple[ArgumentNode, ...]:
        if entry.arg_nodes:
            return entry.arg_nodes
        if not entry.args or entry.field_def is None:
            return ()
        definition = entry.field_def
        nodes: list[ArgumentNode] = []
        for argument in entry.args:
            if argument.resolved is None:
                raise ArgumentError(
                    kind="field",
                    operation_name=f"{parent_type.name}.{entry.name}",
                    bad_name=argument.written,
                    signature=_field_signature(entry.name, definition),
                    candidates=list(definition.args),
                )
            variable = self.allocator.allocate(
                base="_".join((*path, argument.resolved)),
                type_=definition.args[argument.resolved].type,
                value=argument.value,
            )
            nodes.append(
                ArgumentNode(
                    name=NameNode(value=argument.resolved),
                    value=VariableNode(name=NameNode(value=variable)),
                )
            )
        return tuple(nodes)


def _generates_connection_template(entry: _Entry, child_type: GraphQLNamedType) -> bool:
    """Whether the builder produces the relay template under this field.

    There are two ways to ask for it from an explicit selection: ``AUTO`` as
    the field's whole sub-selection, and an inline fragment on the connection
    type whose own fields are ``AUTO``. Both reach the same builder call, so
    both owe C1's page-size bound.
    """
    if not is_connection_type(child_type):
        return False
    if entry.auto:
        return True
    if entry.children is None:
        return False
    fragment = entry.children.fragments.get(child_type.name)
    return fragment is not None and fragment.auto


def _as_composite(type_: Any) -> GraphQLCompositeType:
    """Narrow a named type that ``is_composite_type`` has already accepted."""
    assert isinstance(
        type_, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
    )
    return type_


def _merge_entry(level: _Level, entry: _Entry, parent_type_name: str) -> None:
    existing = level.fields.get(entry.key)
    if existing is None:
        level.fields[entry.key] = entry
        return
    if existing.alias is not None or entry.alias is not None:
        # An alias must be unique within its selection set, so a response key
        # that carries one cannot appear twice. This is checked before the
        # difference checks below, whose advice is to add an alias.
        raise SelectionError(
            f"the response key {entry.key!r} is written twice in this "
            f"selection of {parent_type_name}, and at least one of the two is "
            "an alias.\n"
            f"  One is {_describe(existing)}, the other is {_describe(entry)}.\n"
            "  An alias names one result, so it can be used only once. Remove "
            "one of them, or give them different aliases."
        )
    if existing.name != entry.name or existing.signature != entry.signature:
        raise SelectionError(
            f"two selections of {parent_type_name} share the response key "
            f"{entry.key!r} but differ.\n"
            f"  One is {_describe(existing)}, the other is {_describe(entry)}.\n"
            "  Give one of them an alias."
        )
    if existing.auto != entry.auto:
        raise SelectionError(
            f"{entry.key!r} is selected both automatically and explicitly.\n"
            "  Keep one of them."
        )
    if existing.children is not None and entry.children is not None:
        _merge_levels(existing.children, entry.children)
    elif entry.children is not None:
        existing.children = entry.children


def _merge_levels(into: _Level, other: _Level) -> None:
    for entry in other.fields.values():
        _merge_entry(into, entry, "this selection")
    for fragment in other.fragments.values():
        _merge_fragment_into(into, fragment)


def _merge_fragment_into(level: _Level, fragment: _Fragment) -> None:
    """Put one fragment into a level, merging it with the one already there.

    Every path that produces a fragment ends here, whether it came from
    ``Selection.of``, from a ``Field`` carrying a type condition, from a raw
    GraphQL string, or from the union of two selections. One primitive means
    one answer to "these two disagree", rather than three that could drift.
    """
    existing = level.fragments.get(fragment.type_name)
    if existing is None:
        level.fragments[fragment.type_name] = fragment
        return
    if existing.auto != fragment.auto:
        raise SelectionError(
            f"the fragment on {fragment.type_name} is written twice, once "
            "automatically and once explicitly.\n"
            "  Keep one of them."
        )
    if existing.children is not None and fragment.children is not None:
        _merge_levels(existing.children, fragment.children)


def _remove_path(level: _Level, removal: str, parent_type_name: str) -> None:
    """Apply one ``-`` operand: a response key, or a dotted path to one."""
    head, _, tail = removal.partition(".")
    entry = level.fields.get(head)
    if entry is None:
        raise SelectionError(
            f"cannot remove {removal!r}: {head!r} is not in this selection of "
            f"{parent_type_name}.\n"
            f"  It holds: {', '.join(level.fields) or 'nothing'}"
        )
    if not tail:
        del level.fields[head]
        return
    if entry.children is None:
        raise SelectionError(
            f"cannot remove {removal!r}: {head!r} has no listed sub-selection "
            "to remove from.\n"
            "  Remove the whole field, or list its sub-fields first."
        )
    _remove_path(entry.children, tail, entry.name)


def _describe(entry: _Entry) -> str:
    if entry.signature:
        return f"{entry.name} with arguments {entry.signature}"
    return entry.name


def _canonical(value: Any) -> str:
    """A repr that does not change when an equal mapping is built differently."""
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{key!r}: {_canonical(value[key])}" for key in sorted(value, key=repr)
        )
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_canonical(item) for item in value) + "]"
    return repr(value)


def _resolve_arguments(
    args: Mapping[str, Any] | None, definition: GraphQLField
) -> tuple[_Argument, ...]:
    """Resolve every argument name of a ``Field`` against the schema, once."""
    if not args:
        return ()
    name_map = NameMap.build(list(definition.args))
    return tuple(
        _Argument(written=written, resolved=name_map.get(written), value=value)
        for written, value in args.items()
    )


def _canonical_literal(node: ValueNode, type_: GraphQLInputType) -> str | None:
    """One string per request, or ``None`` when the literal has no canonical form.

    A literal is not yet the value it denotes. ``ID!`` accepts both ``1`` and
    ``"1"`` for the same identifier, an input object may be written with its
    fields in any order and with a defaulted field left out, and a block
    string denotes an ordinary string. This walks the declared type and the
    written literal together and removes every one of those differences.

    It walks rather than coercing the whole literal in one call, because a
    coerced input object is a Python mapping keyed by ``out_name``, which is a
    resolver-side name a schema may set freely. Two different requests can
    share one coerced mapping, so the coerced form is not a key. The written
    field names are, and they survive this walk untouched.

    ``None`` means the literal has no canonical form and the caller keeps it as
    written. That covers a variable reference, whose meaning belongs to the
    document it came from, and every invalid literal: an undeclared or repeated
    input field, a missing required field, a one-of object without exactly one
    non-null field, a value the type cannot coerce. Invalid text is document
    validation's finding to report, so it must reach validation as the caller
    wrote it.
    """
    if isinstance(type_, GraphQLNonNull):
        if isinstance(node, NullValueNode):
            return None
        return _canonical_literal(node, type_.of_type)
    if isinstance(node, VariableNode):
        return None
    if isinstance(node, NullValueNode):
        return "null"
    if isinstance(type_, GraphQLList):
        return _canonical_list(node, type_.of_type)
    if isinstance(type_, GraphQLInputObjectType):
        return _canonical_object(node, type_)
    return _canonical_leaf(node, type_)


def _canonical_list(node: ValueNode, item_type: GraphQLInputType) -> str | None:
    """A list literal, with the one-item shorthand written out in full."""
    items = node.values if isinstance(node, ListValueNode) else (node,)
    rendered: list[str] = []
    for item in items:
        canonical = _canonical_literal(item, item_type)
        if canonical is None:
            return None
        rendered.append(canonical)
    return "[" + ", ".join(rendered) + "]"


def _canonical_object(node: ValueNode, type_: GraphQLInputObjectType) -> str | None:
    """An input object literal in schema field order, with defaults filled in."""
    if not isinstance(node, ObjectValueNode):
        return None
    written: dict[str, ValueNode] = {}
    for field_node in node.fields:
        name = field_node.name.value
        if name in written or name not in type_.fields:
            return None
        written[name] = field_node.value
    if _breaks_one_of(type_, list(written.values())):
        return None
    rendered: list[str] = []
    for name, declared in type_.fields.items():
        if name in written:
            canonical = _canonical_literal(written[name], declared.type)
        elif declared.default_value is not Undefined:
            canonical = _canonical_default(declared.default_value, declared.type)
        elif isinstance(declared.type, GraphQLNonNull):
            return None
        else:
            continue
        if canonical is None:
            return None
        rendered.append(f"{name}: {canonical}")
    return "{" + ", ".join(rendered) + "}"


def _breaks_one_of(type_: GraphQLInputObjectType, values: Sequence[Any]) -> bool:
    """Whether a one-of input object holds anything but a single non-null field.

    ``@oneOf`` is a condition on the type, not on any one field, so no
    field-by-field check can see it. A value that breaks it is invalid, and an
    invalid value has no canonical form: it stays as the caller wrote it and
    validation reports it. Both value kinds this module walks pass through
    here, so a stored ``None`` and a written ``null`` are the same answer.
    """
    if not type_.is_one_of:
        return False
    if len(values) != 1:
        return True
    only = values[0]
    return only is None or isinstance(only, NullValueNode)


def _canonical_default(value: Any, type_: GraphQLInputType) -> str | None:
    """An input field's stored default, in the same terms as a written literal.

    A default is delivered to a resolver exactly as stored: execution fills in
    nothing further, at any depth, when the argument that owns it is omitted.
    So a canonical form exists only when rendering the stored value to a
    literal and coercing that literal back reproduces the exact same value, in
    both type and structure, checked once over the whole value rather than
    once per leaf. One round trip over the assembled literal is what catches
    every way the rendered form could describe a different resolver-visible
    value than the default actually is: a stored tuple or set that a list
    literal's coercion would turn into a ``list``, a stored object missing a
    field that a literal's own missing-field defaulting would insert, or a
    leaf value the type serializes differently than it was stored.

    An input object arrives as a mapping keyed by
    ``GraphQLInputField.out_name`` and shaped by the type's ``out_type``, so
    the literal is built by reading each field under the name the schema
    stores it by and writing the schema name back; ``ast_from_value`` alone
    cannot do this because it reads schema field names and simply drops a
    renamed field.

    A Python argument value written by the caller is the other direction and
    stays on the existing path, because the caller writes schema field names.
    """
    if isinstance(type_, GraphQLNonNull):
        return None if value is None else _canonical_default(value, type_.of_type)
    if value is None:
        return "null"
    node = _default_literal(value, type_)
    if node is None:
        return None
    round_tripped = value_from_ast(node, type_)
    if round_tripped is Undefined or not _exact_match(round_tripped, value):
        return None
    return print_ast(node)


def _exact_match(a: Any, b: Any) -> bool:
    """Whether two values are the same value at every depth, not only the top.

    Python ``==`` treats ``1`` and ``1.0`` as equal, and extends that to any
    list or mapping holding them: two containers compare equal as soon as
    their corresponding elements do, regardless of the concrete type of each
    element. A resolver does not see it that way. It receives the actual
    Python type stored at every list position and every mapping value, so an
    ``Int`` default stored as ``1`` and a round-tripped ``Float`` of ``1.0``
    are different resolver-visible values even nested inside an otherwise
    identical list or object. Checking ``type(...) is not type(...)`` only at
    the value ``_canonical_default`` was called with, then falling back to
    ``==`` for everything under it, misses exactly that case. This instead
    checks type and content together at every depth before it ever calls
    ``==``, so no depth is left to Python's looser notion of equal.

    A mapping key is a resolver-visible value in exactly the same sense as a
    mapping value or a list item, so it needs the same recursive check, not
    the plain ``==`` that ``dict.keys()`` comparison and dict lookup fall back
    to. That plain ``==`` is also the deeper problem, not only at keys: it
    trusts whatever ``__eq__`` and ``__hash__`` the value's own class
    implements, and a ``str`` subclass is free to override both so that two
    different payloads compare and hash equal. GraphQL input-object storage
    keys, and every string leaf this module deals in, are ``str`` at the
    core, and it is that core payload a resolver actually reads, not
    whatever an override claims. So a ``str`` is never compared with ``==``
    here; a plain ``str`` is compared through ``str.__eq__`` directly, which
    reaches the real payload no override can hide, and a ``str`` subclass is
    excluded from that content check entirely rather than let through it,
    for the same reason every other builtin's subclass is, stated in full
    below. Keys are matched by that same content check against a candidate
    list, not through a ``dict`` or a key-view, because building or
    indexing a ``dict`` from the operands' own keys is exactly the
    ``__eq__``-and-``__hash__`` trust this rule exists to remove. Matching
    is by content, not position, so a mapping built with its entries in a
    different order still compares equal when every key and value otherwise
    matches.

    That same trust problem exists for every fallback shape, not only
    ``str``: a custom scalar's ``parse_value``/``parse_literal`` is free to
    return an instance of any type, including a subclass of a numeric
    builtin with an overridden ``__eq__`` that reports unequal payloads
    equal, so a plain ``a == b`` fallback would repeat exactly the mistake
    already fixed for keys. This never falls back to ``==`` on an operand of
    unknown shape. It recognizes only the finite set of shapes graphql-core's
    own scalar coercion and this module's own recursion are known to
    produce, and it recognizes a shape only when the operand's concrete type
    *is* that exact builtin, never a subclass of it: ``None`` (a type that
    cannot be subclassed, so type equality already proves sameness), and
    ``bool``, ``int``, ``float``, ``bytes``, ``str``, ``dict``, ``list``, and
    ``tuple`` each recognized only by ``type(a) is <builtin>``.

    Trusting ``isinstance`` here instead would recognize a subclass too, and
    a subclass defeats this in two different ways depending on the shape,
    not only through an overridden ``__eq__``. A subclass of an immutable
    scalar builtin can carry its own extra instance attributes that the
    base builtin's ``__eq__`` never inspects, so two instances holding
    different attached state, such as a custom scalar's parsed value paired
    with a hidden tag, still compare equal through ``int.__eq__`` even with
    no override at all: the base payload matches while the resolver-visible
    state does not. A subclass of a container builtin can instead override
    the very operations this function reads its structure through, such as
    ``__len__``, ``__iter__``, or ``items``, so two instances can present an
    identical view while their real backing content differs. Excluding every
    subclass, not only a maliciously-overridden one, is what closes both
    variants at once: a value is compared through a builtin's own behavior
    only when it truly is that builtin, with no subclass layer able to add
    unseen state or misreport its own content.

    A ``float`` needs one further correction beyond exact-type matching:
    ``0.0 == -0.0`` is ``True`` under plain ``float.__eq__``, but the two
    carry different sign bits, and a custom scalar's own serializer is free
    to print that sign, making them different resolver-visible literals. So
    two ``float`` operands are matched by value and by the sign of zero
    together, treating ``0.0`` and ``-0.0`` as different. A ``nan`` is not
    special-cased: ``nan == nan`` is already ``False``, so it can only ever
    make this function report two values as different, never as the same,
    which is the safe direction for a content check that exists to prevent
    an incorrect merge.

    Any type not on that exact-type list, including a subclass of any
    builtin on it, has no known base implementation to fall back to, so it
    is never proven equal by content. The only override-proof test left for
    it is object identity, which every type supports without dispatching to
    a possibly-overridden method: two independently produced values are
    essentially never the same object, so this conservatively treats such a
    value as having no canonical form rather than trust a fallback ``==`` or
    a fallback structural read it cannot verify.
    """
    if type(a) is not type(b):
        return False
    if a is None:
        return True
    if type(a) is str:
        return str.__eq__(a, b)
    if type(a) is bool:
        return bool.__eq__(a, b)
    if type(a) is int:
        return int.__eq__(a, b)
    if type(a) is float:
        return float.__eq__(a, b) and math.copysign(1.0, a) == math.copysign(1.0, b)
    if type(a) is bytes:
        return bytes.__eq__(a, b)
    if type(a) is dict:
        if len(a) != len(b):
            return False
        remaining = list(b.items())
        for a_key, a_value in a.items():
            for index, (b_key, b_value) in enumerate(remaining):
                if _exact_match(a_key, b_key) and _exact_match(a_value, b_value):
                    del remaining[index]
                    break
            else:
                return False
        return True
    if type(a) is list or type(a) is tuple:
        return len(a) == len(b) and all(
            _exact_match(x, y) for x, y in zip(a, b, strict=True)
        )
    return a is b


def _default_literal(value: Any, type_: GraphQLInputType) -> ValueNode | None:
    """The literal a stored default would print as, holding only what it holds.

    Never fills in anything the default does not itself carry: a stored
    default is delivered exactly as stored, so a literal that inserted a
    missing list wrapper or a missing field's own default would build a value
    execution never produces from this default. ``_canonical_default`` proves
    the result by a full round trip against the original value, so this only
    has to build the candidate literal, not prove it is equivalent.
    """
    if isinstance(type_, GraphQLNonNull):
        return None if value is None else _default_literal(value, type_.of_type)
    if value is None:
        return NullValueNode()
    if isinstance(type_, GraphQLList):
        return _default_list_literal(value, type_.of_type)
    if isinstance(type_, GraphQLInputObjectType):
        return _default_object_literal(value, type_)
    try:
        return ast_from_value(value, type_)
    except GraphQLError:
        return None


def _default_list_literal(
    value: Any, item_type: GraphQLInputType
) -> ListValueNode | None:
    """A stored list default's candidate literal, or ``None`` for a bare value.

    The literal shorthand lets a caller write a bare value where a list is
    expected, but coercion never manufactures a list from a bare stored value
    the way that shorthand does: a default that skipped coercion, and so is
    not already iterable, delivers the bare value itself to a resolver, not a
    one-item list. Such a default has no literal here. A stored tuple or set
    does build one, exactly like a list would; the round trip in
    ``_canonical_default`` is what tells them apart from a genuine list,
    because coercion never produces anything but a ``list``.
    """
    if not is_iterable(value):
        return None
    rendered: list[ValueNode] = []
    for item in value:
        node = _default_literal(item, item_type)
        if node is None:
            return None
        rendered.append(node)
    return ListValueNode(values=tuple(rendered))


def _default_object_literal(
    value: Any, type_: GraphQLInputObjectType
) -> ObjectValueNode | None:
    """A stored input-object default's candidate literal, read under ``out_name``.

    ``None`` when nothing here can reverse the stored form: a custom
    ``out_type`` returns whatever it likes, a non-mapping is not an input
    object at all, a key no field claims belongs to neither name space, and
    two declared fields that store under the same key, because a stored value
    under that key could then belong to either field and there is no way to
    tell which one it was. A field missing from the stored value is left out
    of the literal rather than filled from its own default: filling it here
    would build the same literal for two different stored values, one that
    truly holds the field's default and one that never set it at all. The
    round trip in ``_canonical_default`` catches whichever way that guess
    would have been wrong.

    A storage key is attributed to a field by its exact content, never by
    ``in`` or ``[]`` on a ``dict`` keyed by the storage keys themselves: a
    schema-supplied ``out_name`` is caller-controlled and free to override
    ``__eq__``/``__hash__``, and a ``dict`` built or indexed with such a key
    trusts that override to decide which field a value belongs to. The same
    ``_exact_match`` that closes this gap for comparing two coerced values
    closes it here too, since a storage key is a string compared the same
    way regardless of which side of that comparison it is on.
    """
    if type_.out_type is not GraphQLInputObjectType.out_type:
        return None
    if not isinstance(value, Mapping):
        return None
    storage_keys: list[tuple[Any, str]] = []
    for name, declared in type_.fields.items():
        key = declared.out_name or name
        if any(_exact_match(key, existing) for existing, _ in storage_keys):
            return None
        storage_keys.append((key, name))
    attributed: dict[str, Any] = {}
    for value_key, value_value in value.items():
        name = next(
            (
                candidate_name
                for candidate_key, candidate_name in storage_keys
                if _exact_match(value_key, candidate_key)
            ),
            None,
        )
        if name is None:
            return None
        attributed[name] = value_value
    if _breaks_one_of(type_, list(attributed.values())):
        return None
    fields: list[ObjectFieldNode] = []
    for _key, name in storage_keys:
        if name not in attributed:
            continue
        node = _default_literal(attributed[name], type_.fields[name].type)
        if node is None:
            return None
        fields.append(ObjectFieldNode(name=NameNode(value=name), value=node))
    return ObjectValueNode(fields=tuple(fields))


def _canonical_leaf(node: ValueNode, type_: GraphQLInputType) -> str | None:
    """A scalar or enum literal, coerced through its own type.

    Coercion is safe here and nowhere else in this walk: a leaf value is not a
    mapping, so it carries no field names that coercion could rename.
    """
    coerced = value_from_ast(node, type_)
    if coerced is Undefined:
        return None
    rendered = ast_from_value(coerced, type_)
    return None if rendered is None else print_ast(rendered)


def _written_literal(node: ValueNode, type_: GraphQLInputType) -> str:
    """The canonical form of a written literal, or the literal as written."""
    canonical = _canonical_literal(node, type_)
    return print_ast(node) if canonical is None else canonical


def _python_literal(value: Any, type_: GraphQLInputType) -> str:
    """Print a Python argument value as the literal the schema would write.

    The value becomes a literal first and is then canonicalized by the one
    routine above. Going through the same routine is what makes the two input
    forms comparable: a shortcut that printed the value directly would skip
    canonicalization and disagree with the written form wherever it does
    anything, such as filling in an input field's default.

    A value with no canonical form falls back to its own repr rather than to a
    printed literal, because the literal is what dropped the difference. The
    fallback names the form it came from, and ``<`` cannot begin a GraphQL
    value, so it can never equal a written literal that also has no canonical
    form. Two such values have to reach validation as the caller gave them, and
    a merge would keep only one of them.
    """
    canonical = _canonical_python(value, type_)
    return f"<python value> {_canonical(value)}" if canonical is None else canonical


def _canonical_python(value: Any, type_: GraphQLInputType) -> str | None:
    """The canonical form of a Python argument value, or ``None`` if it has none."""
    if not _declares_every_key(value, type_):
        return None
    node = ast_from_value(value, type_)
    if node is None:
        return None
    return _canonical_literal(node, type_)


def _declares_every_key(value: Any, type_: GraphQLInputType) -> bool:
    """Whether every mapping key in a Python value names a declared input field.

    ``ast_from_value`` drops a key the input type does not declare, so the
    literal it produces cannot be trusted to tell two such values apart. The
    value is invalid either way; this only keeps the difference visible.
    """
    if isinstance(type_, GraphQLNonNull):
        return _declares_every_key(value, type_.of_type)
    if isinstance(type_, GraphQLList):
        if not is_iterable(value):
            return _declares_every_key(value, type_.of_type)
        return all(_declares_every_key(item, type_.of_type) for item in value)
    if not isinstance(type_, GraphQLInputObjectType):
        return True
    if not isinstance(value, Mapping):
        return True
    return all(
        key in type_.fields and _declares_every_key(value[key], type_.fields[key].type)
        for key in value
    )


def _python_args_signature(
    arguments: Sequence[_Argument], definition: GraphQLField
) -> str:
    """The arguments of a ``Field``, rendered the way GraphQL would write them.

    The signature decides whether two selections of one response key request
    the same thing. SPEC 3.3 calls the four input forms interchangeable, so
    the comparison has to be about what was asked for and not about which form
    asked. Rendering Python values through the argument's schema type puts
    them in the same terms a raw GraphQL string is already in.

    An argument name that resolves to nothing is kept as written, so that two
    different unknown names do not look like one. The emitter raises for it.
    """
    if not arguments:
        return ""
    rendered: list[str] = []
    for argument in arguments:
        if argument.resolved is None:
            rendered.append(f"{argument.written}: {_canonical(argument.value)}")
            continue
        rendered.append(
            f"{argument.resolved}: "
            + _python_literal(argument.value, definition.args[argument.resolved].type)
        )
    return ", ".join(sorted(rendered))


def _ast_args_signature(
    arguments: Sequence[ArgumentNode], definition: GraphQLField
) -> str:
    """The arguments of a raw GraphQL field, canonicalized before comparison.

    A written literal goes through the same canonical form the Python value
    does, so the two forms describe the same request with the same string. An
    argument the field does not declare is kept as written; the emitter
    reports it.
    """
    if not arguments:
        return ""
    rendered: list[str] = []
    for argument in arguments:
        declared = definition.args.get(argument.name.value)
        if declared is None:
            rendered.append(print_ast(argument))
            continue
        rendered.append(
            f"{argument.name.value}: " + _written_literal(argument.value, declared.type)
        )
    return ", ".join(sorted(rendered))


def _field_signature(name: str, definition: GraphQLField) -> str:
    arguments = ", ".join(
        f"{argument_name}: {argument.type}"
        for argument_name, argument in definition.args.items()
    )
    return f"{name}({arguments}): {definition.type}"
