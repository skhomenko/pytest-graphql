"""Auto-selection: the schema walker behind ``AUTO``.

This module implements SPEC 5.4's nine traversal rules together, with A6, B12
and the C1 limits. The rules interact, so each one is applied at the single
point where it can be decided and nowhere else:

1. Depth is carried as ``remaining``, the number of further levels of
   composite expansion allowed below the selection set being built.
   ``per_type_depth_cap`` lowers it when a named type is entered, so the cap
   means "levels below this type" wherever the type appears.
2. Cycles are checked against ``ancestors``, the named composite types on the
   current path. A global visited set would call diamond reuse a cycle, which
   it is not: the same type in two sibling branches is ordinary.
3. Required arguments skip the field, whatever it returns (A6).
4. A Relay connection field is expanded only when the field accepts a
   page-size argument (C1), and the generated ``first`` value travels as a
   variable, never as text. The template's shape is rule 4's, and only the
   ``node`` expansion is an ordinary traversal, costing no depth. The template
   is not exempt from the other rules: rule 7 applies to every field in it.
5. Deprecated fields are out unless the policy asks for them (C1).
6. An interface emits its own leaf fields and one inline fragment per
   possible type, capped by ``max_union_members``.
7. Excludes are the policy's ``should_include`` hook and are asked once per
   field.
8. The size guard counts fields as they are emitted and raises the moment the
   limit is passed. Raising immediately, rather than counting the whole
   selection first, is what makes the walk terminate on a hostile schema. The
   count travels out on ``BuiltSelection.field_count``, because ``max_fields``
   guards the whole document and a caller composing this selection into a
   larger one has to charge it without rebuilding it (C56).
9. A root type that yields nothing raises, because ``{}`` is not a document.

``__typename`` is emitted on every object selection and never counted (B12).
The policy's excludes are not consulted for it: B12 states the rule without a
condition.

One memoization entry covers one whole build. The key is the root type name
and the policy's cache key, and nothing else, which is exactly correct because
a build always starts from the same state, and C56 is the rule that keeps it
so: a build is a scope, and nothing above it reaches in. The one thing the
root type itself decides is connection nesting, because a scope rooted at a
connection is already one connection deep. Memoizing a type *inside* the walk
would not be correct, because what that type expands to does depend on where
it was reached from. Callers treat a returned ``BuiltSelection`` as immutable,
since the same node object is handed to every caller that asks for the same
key.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, cast

from graphql import (
    ArgumentNode,
    FieldNode,
    GraphQLCompositeType,
    GraphQLField,
    GraphQLInputType,
    GraphQLInt,
    GraphQLInterfaceType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    NamedTypeNode,
    NameNode,
    SelectionNode,
    SelectionSetNode,
    VariableNode,
    get_named_type,
    is_leaf_type,
)

from pytest_graphql._core.diagnostics import (
    MAX_OMISSION_RECORDS,
    OmissionReason,
    OmissionRecord,
)
from pytest_graphql._core.errors import SelectionError, SelectionTooLargeError
from pytest_graphql._core.selection.policy import (
    PAGE_SIZE_ARGUMENT,
    SelectionPolicy,
    is_connection_type,
    missing_required_arguments,
    page_size_argument,
)

TYPENAME = "__typename"

#: The one generated variable auto-selection needs. It is a fixed name rather
#: than a path-derived one so that a build depends on the type and the policy
#: alone, which is what lets a whole build be memoized on that pair. Every
#: connection in one document is paged by the same policy value, so one
#: variable is also the honest shape.
PAGE_SIZE_VARIABLE = "pytest_graphql_page_size"

#: Its declared type. Non-null, because the engine always sends a value and a
#: non-null Int is accepted at both ``first: Int`` and ``first: Int!``. A
#: nullable declaration is refused at the second, which real schemas write.
PAGE_SIZE_VARIABLE_TYPE = GraphQLNonNull(GraphQLInt)

#: How many builds one builder memoizes before evicting the least recently
#: used. A schema has far fewer commonly selected types than this, so an
#: ordinary session never evicts; the bound exists for the case C18 invites,
#: where a custom fingerprint changes per test and every build is a new key.
MAX_CACHE_ENTRIES = 128


@dataclass(frozen=True)
class GeneratedVariable:
    """A variable the engine created, for the operation layer to declare."""

    name: str
    type_: GraphQLInputType
    value: Any


def page_size_variable(policy: SelectionPolicy) -> GeneratedVariable:
    """The one page-size variable, however the connection was reached.

    Both generation paths declare the same variable: the walk, when it crosses
    a connection field itself, and ``normalize.py``, when a caller named the
    field and asked for ``AUTO`` under it. One constructor keeps the declared
    type in one place, so the two cannot disagree about it.
    """
    return GeneratedVariable(
        name=PAGE_SIZE_VARIABLE,
        type_=PAGE_SIZE_VARIABLE_TYPE,
        value=policy.connection_page_size,
    )


@dataclass(frozen=True)
class BuiltSelection:
    """A selection set and the variables it refers to.

    The operation layer (M4) declares the variables and sends the values. The
    engine never writes a value into document text.
    """

    selection_set: SelectionSetNode
    variables: tuple[GeneratedVariable, ...] = ()

    #: Fields in this selection, excluding ``__typename`` (B12). ``max_fields``
    #: guards document size, so a caller composing this selection into a larger
    #: one adds this count to its own rather than rebuilding to find it (C56).
    field_count: int = 0

    #: C58: what this selection dropped, and why, bounded by
    #: ``MAX_OMISSION_RECORDS``. Paths are relative to this scope's root, so a
    #: caller composing the selection into a larger document rebases them.
    omissions: tuple[OmissionRecord, ...] = ()

    #: How many omissions the walk made in total, including past the bound. A
    #: skipped field is not counted by ``max_fields``, so this is the only
    #: number that states the true size.
    omissions_total: int = 0


class SelectionBuilder:
    """Builds and memoizes auto-selections for one schema.

    The memo is bounded and evicts least-recently-used first. The number of
    distinct keys is not bounded by the schema: C18 invites a custom policy to
    opt back into caching by overriding ``fingerprint``, and requires that
    value to cover external state, so a policy that obeys C18 and reads state
    that changes per test produces a new key every build. Without a bound, a
    session would retain every selection set it ever generated. Eviction costs
    only time, by C18's own argument: the limits in ``policy.py`` bound every
    traversal, so rebuilding is never wrong.
    """

    def __init__(
        self, schema: GraphQLSchema, max_entries: int = MAX_CACHE_ENTRIES
    ) -> None:
        self._schema = schema
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], BuiltSelection] = OrderedDict()

    def build(
        self, type_: GraphQLCompositeType, policy: SelectionPolicy
    ) -> BuiltSelection:
        """The generated selection set for ``type_`` under ``policy``."""
        key = policy.cache_key
        cache_key = None if key is None else (type_.name, key)
        if cache_key is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached
        built = _Walk(self._schema, policy).run(type_)
        if cache_key is not None:
            self._cache[cache_key] = built
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        return built

    @property
    def cache_size(self) -> int:
        """How many builds are memoized. Read by the C18 and bound tests."""
        return len(self._cache)


def typename_node() -> FieldNode:
    return FieldNode(
        name=NameNode(value=TYPENAME), arguments=(), directives=(), selection_set=None
    )


def has_selectable_content(selections: list[SelectionNode]) -> bool:
    """Whether a selection set holds anything beyond a bare ``__typename``.

    An object selection always carries ``__typename``, so it is never empty in
    the literal sense. This is the question rule 9 actually asks, and it is
    also how a child whose expansion collapsed to nothing is recognised.
    """
    for selection in selections:
        if isinstance(selection, InlineFragmentNode):
            return True
        if isinstance(selection, FieldNode) and selection.name.value != TYPENAME:
            return True
    return False


class _Walk:
    """One build. Holds the state that must not leak between builds."""

    def __init__(self, schema: GraphQLSchema, policy: SelectionPolicy) -> None:
        self._schema = schema
        self._policy = policy
        self._emitted = 0
        self._root_name = ""
        self._uses_page_size = False
        self._omissions: list[OmissionRecord] = []
        self._omissions_total = 0

    def run(self, type_: GraphQLCompositeType) -> BuiltSelection:
        self._root_name = type_.name
        remaining = _lower_to_cap(
            self._policy.max_depth - 1, self._policy.depth_cap_for(type_.name)
        )
        # C56: a scope rooted at a connection is already one connection deep.
        # Starting at zero would let AUTO on a connection field reset the C1
        # nesting cap and expand a connection inside a connection.
        at_connection = self._policy.relay_aware and is_connection_type(type_)
        selections = self._expand(
            type_,
            remaining=remaining,
            path=(),
            ancestors=(type_.name,),
            connection_depth=1 if at_connection else 0,
        )
        if not has_selectable_content(selections):
            raise SelectionError.no_selectable_fields(type_.name)
        variables: tuple[GeneratedVariable, ...] = ()
        if self._uses_page_size:
            variables = (page_size_variable(self._policy),)
        return BuiltSelection(
            selection_set=SelectionSetNode(selections=tuple(selections)),
            variables=variables,
            field_count=self._emitted,
            omissions=tuple(self._omissions),
            omissions_total=self._omissions_total,
        )

    def _omit(
        self, parent_type_name: str, path: tuple[str, ...], reason: OmissionReason
    ) -> None:
        """Record one automatic omission (C58, SPEC 5.4 rule 3).

        The record carries the parent type, the field path relative to this
        scope, and the reason, and never an argument value. The bound is the
        records' own, because a skipped field is not counted by
        ``max_fields``: the first ``MAX_OMISSION_RECORDS`` in traversal order
        are kept and the total is tracked, so what was cut is reported rather
        than lost.
        """
        self._omissions_total += 1
        if len(self._omissions) < MAX_OMISSION_RECORDS:
            self._omissions.append(
                OmissionRecord(parent_type=parent_type_name, path=path, reason=reason)
            )

    def _count(self) -> None:
        self._emitted += 1
        if self._emitted > self._policy.max_fields:
            raise SelectionTooLargeError(
                type_name=self._root_name,
                field_count=self._emitted,
                limit=self._policy.max_fields,
            )

    def _expand(
        self,
        type_: GraphQLCompositeType,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode]:
        """The selection set for one composite type."""
        if isinstance(type_, (GraphQLUnionType, GraphQLInterfaceType)):
            return self._expand_abstract(
                type_,
                remaining=remaining,
                path=path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
        if self._policy.relay_aware and is_connection_type(type_):
            return self._expand_connection(
                type_,
                remaining=remaining,
                path=path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
        return self._expand_object(
            type_,
            remaining=remaining,
            path=path,
            ancestors=ancestors,
            connection_depth=connection_depth,
        )

    def _expand_object(
        self,
        type_: GraphQLObjectType,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode]:
        selections: list[SelectionNode] = [typename_node()]
        for name, field_ in type_.fields.items():
            node = self._field_node(
                type_.name,
                name,
                field_,
                remaining=remaining,
                path=path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
            if node is not None:
                selections.append(node)
        return selections

    def _expand_abstract(
        self,
        type_: GraphQLInterfaceType | GraphQLUnionType,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode]:
        """Rule 6: ``__typename``, own leaf fields, then one fragment per member."""
        selections: list[SelectionNode] = [typename_node()]
        if isinstance(type_, GraphQLInterfaceType):
            for name, field_ in type_.fields.items():
                if not is_leaf_type(get_named_type(field_.type)):
                    continue
                node = self._field_node(
                    type_.name,
                    name,
                    field_,
                    remaining=remaining,
                    path=path,
                    ancestors=ancestors,
                    connection_depth=connection_depth,
                )
                if node is not None:
                    selections.append(node)

        for index, member in enumerate(self._schema.get_possible_types(type_)):
            inner = self._member_selections(
                member,
                collapsed=index >= self._policy.max_union_members,
                remaining=remaining,
                path=path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
            if inner is None:
                continue
            selections.append(
                InlineFragmentNode(
                    type_condition=NamedTypeNode(name=NameNode(value=member.name)),
                    directives=(),
                    selection_set=SelectionSetNode(selections=tuple(inner)),
                )
            )
        return selections

    def _member_selections(
        self,
        member: GraphQLObjectType,
        *,
        collapsed: bool,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode] | None:
        """One member of an interface or union, expanded or collapsed.

        An inline fragment adds no level, so the member expands with the same
        budget the abstract type had, lowered by the member's own cap.
        """
        if collapsed:
            return self._collapsed_member(member, path)
        if member.name in ancestors:
            return self._cycle_selections(
                member,
                path=path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
        inner = self._expand(
            member,
            remaining=_lower_to_cap(remaining, self._policy.depth_cap_for(member.name)),
            path=path,
            ancestors=(*ancestors, member.name),
            connection_depth=connection_depth,
        )
        return inner if has_selectable_content(inner) else None

    def _leaf_node(
        self, parent_type_name: str, name: str, path: tuple[str, ...]
    ) -> FieldNode | None:
        """Emit one leaf field the engine chose by name, or refuse it.

        Every field this engine emits goes through the policy hook, whichever
        rule asked for it. Rule 4's template names its fields itself rather
        than walking to them, and rule 2 and rule 6 name ``id`` the same way,
        so without this they would be the only emissions rule 7 never saw.
        ``__typename`` is the one exemption, and B12 states it without a
        condition.
        """
        if not self._policy.should_include(parent_type_name, name, path, len(path)):
            return None
        self._count()
        return FieldNode(
            name=NameNode(value=name),
            arguments=(),
            directives=(),
            selection_set=None,
        )

    def _id_node(
        self, type_: GraphQLCompositeType, path: tuple[str, ...]
    ) -> FieldNode | None:
        """The ``id`` field of a type, when it has one the policy allows."""
        if not isinstance(type_, GraphQLObjectType) or "id" not in type_.fields:
            return None
        return self._leaf_node(type_.name, "id", path)

    def _collapsed_member(
        self, member: GraphQLObjectType, path: tuple[str, ...]
    ) -> list[SelectionNode]:
        """Rule 6 past ``max_union_members``: ``__typename`` plus ``id`` if any.

        The fragment is still emitted for a member with no ``id``, because
        ``__typename`` is what tells a reader which member came back.
        """
        selections: list[SelectionNode] = [typename_node()]
        id_node = self._id_node(member, path)
        if id_node is not None:
            selections.append(id_node)
        return selections

    def _cycle_identity(
        self, type_: GraphQLCompositeType, path: tuple[str, ...]
    ) -> list[SelectionNode] | None:
        """Rule 2's ``id_only``: ``id`` alone, or nothing if the type has none."""
        id_node = self._id_node(type_, path)
        if id_node is None:
            return None
        return [typename_node(), id_node]

    def _field_node(
        self,
        parent_type_name: str,
        name: str,
        field_: GraphQLField,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
        depth_cost: int = 1,
    ) -> FieldNode | None:
        """One field, or ``None`` when a rule removes it.

        Every ``return None`` below is one of C58's seven reasons, and each
        records why before it returns. A silent skip here is the defect SPEC
        5.4 rule 3 exists to close: the field is gone from the document and
        nothing tells the reader which rule removed it.
        """
        field_path = (*path, name)
        if not self._policy.should_include(parent_type_name, name, path, len(path)):
            self._omit(parent_type_name, field_path, "should-include")
            return None
        if (
            field_.deprecation_reason is not None
            and not self._policy.include_deprecated
        ):
            self._omit(parent_type_name, field_path, "deprecated")
            return None

        named = get_named_type(field_.type)
        if is_leaf_type(named):
            if missing_required_arguments(field_):
                self._omit(parent_type_name, field_path, "required-argument")
                return None
            self._count()
            return FieldNode(
                name=NameNode(value=name),
                arguments=(),
                directives=(),
                selection_set=None,
            )

        # A named output type that is not a leaf is an object, an interface or
        # a union. There is no fourth case, so this narrows rather than checks.
        named = cast(GraphQLCompositeType, named)
        child_path = field_path
        is_connection = self._policy.relay_aware and is_connection_type(named)
        arguments: tuple[ArgumentNode, ...] = ()
        supplied: tuple[str, ...] = ()

        if is_connection:
            if connection_depth + 1 > self._policy.max_connection_depth:
                self._omit(parent_type_name, child_path, "connection-depth")
                return None
            if page_size_argument(field_) is None:
                # C1: a connection with no page-size argument returns an
                # unbounded number of rows, and no field cap can bound that.
                self._omit(parent_type_name, child_path, "connection-page-size")
                return None
            supplied = (PAGE_SIZE_ARGUMENT,)
            arguments = (
                ArgumentNode(
                    name=NameNode(value=PAGE_SIZE_ARGUMENT),
                    value=VariableNode(name=NameNode(value=PAGE_SIZE_VARIABLE)),
                ),
            )

        if missing_required_arguments(field_, supplied):
            self._omit(parent_type_name, child_path, "required-argument")
            return None
        if remaining - depth_cost < 0:
            self._omit(parent_type_name, child_path, "depth")
            return None

        if named.name in ancestors:
            inner = self._cycle_selections(
                named,
                path=child_path,
                ancestors=ancestors,
                connection_depth=connection_depth,
            )
            if inner is None:
                # The cycle policy is what removed it: "stop" refuses the
                # expansion outright, and "shallow" can collapse to nothing.
                self._omit(parent_type_name, child_path, "cycle")
                return None
        else:
            inner = self._expand(
                named,
                remaining=_lower_to_cap(
                    remaining - depth_cost, self._policy.depth_cap_for(named.name)
                ),
                path=child_path,
                ancestors=(*ancestors, named.name),
                connection_depth=connection_depth + (1 if is_connection else 0),
            )
        if inner is None or not has_selectable_content(inner):
            return None
        if is_connection:
            self._uses_page_size = True
        self._count()
        return FieldNode(
            name=NameNode(value=name),
            arguments=arguments,
            directives=(),
            selection_set=SelectionSetNode(selections=tuple(inner)),
        )

    def _cycle_selections(
        self,
        type_: GraphQLCompositeType,
        *,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode] | None:
        """Rule 2: what a type that is already on the current path expands to."""
        policy = self._policy.cycle_policy
        if policy == "stop":
            return None
        if policy == "id_only":
            return self._cycle_identity(type_, path)
        # "shallow": leaf fields only, which is the same walk with no budget
        # left for composites. ``id`` is a leaf, so it is already included.
        inner = self._expand(
            type_,
            remaining=0,
            path=path,
            ancestors=(*ancestors, type_.name),
            connection_depth=connection_depth,
        )
        return inner if has_selectable_content(inner) else None

    def _expand_connection(
        self,
        type_: GraphQLObjectType,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> list[SelectionNode]:
        """Rule 4's template, subject to the rules that surround it.

        The shape is rule 4's, and only the ``node`` expansion is an ordinary
        traversal: it costs no depth, so the type behind a connection sits at
        the connection's own level rather than two below it. Rule 7 still
        applies to every field named here. Rule 4 grants the template no
        exemption from it, and C1 makes ``exclude`` the documented answer for a
        field the test identity cannot read, so a template field has to be
        removable by the same mechanism as any other.
        """
        selections: list[SelectionNode] = [typename_node()]
        page_info = self._page_info_node(type_, path)
        if page_info is not None:
            selections.append(page_info)
        edges = self._edges_node(
            type_,
            remaining=remaining,
            path=path,
            ancestors=ancestors,
            connection_depth=connection_depth,
        )
        if edges is not None:
            selections.append(edges)
        return selections

    def _page_info_node(
        self, type_: GraphQLObjectType, path: tuple[str, ...]
    ) -> FieldNode | None:
        if not self._policy.should_include(type_.name, "pageInfo", path, len(path)):
            return None
        page_info_type = get_named_type(type_.fields["pageInfo"].type)
        if not isinstance(page_info_type, GraphQLObjectType):
            return None
        child_path = (*path, "pageInfo")
        inner: list[SelectionNode] = [typename_node()]
        for name in ("hasNextPage", "endCursor"):
            if name not in page_info_type.fields:
                continue
            node = self._leaf_node(page_info_type.name, name, child_path)
            if node is not None:
                inner.append(node)
        if not has_selectable_content(inner):
            return None
        self._count()
        return FieldNode(
            name=NameNode(value="pageInfo"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=tuple(inner)),
        )

    def _edges_node(
        self,
        type_: GraphQLObjectType,
        *,
        remaining: int,
        path: tuple[str, ...],
        ancestors: tuple[str, ...],
        connection_depth: int,
    ) -> FieldNode | None:
        if not self._policy.should_include(type_.name, "edges", path, len(path)):
            return None
        edge_type = get_named_type(type_.fields["edges"].type)
        if not isinstance(edge_type, GraphQLObjectType):
            return None
        child_path = (*path, "edges")
        inner: list[SelectionNode] = [typename_node()]
        if "cursor" in edge_type.fields:
            cursor = self._leaf_node(edge_type.name, "cursor", child_path)
            if cursor is not None:
                inner.append(cursor)
        node_field = edge_type.fields.get("node")
        if node_field is not None:
            node = self._field_node(
                edge_type.name,
                "node",
                node_field,
                remaining=remaining,
                path=child_path,
                ancestors=(*ancestors, edge_type.name),
                connection_depth=connection_depth,
                depth_cost=0,
            )
            if node is not None:
                inner.append(node)
        if not has_selectable_content(inner):
            return None
        self._count()
        return FieldNode(
            name=NameNode(value="edges"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=tuple(inner)),
        )


def _lower_to_cap(remaining: int, cap: int | None) -> int:
    """A per-type cap lowers the budget below that type, never raises it."""
    return remaining if cap is None else min(remaining, cap)
