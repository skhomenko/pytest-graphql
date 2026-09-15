"""Auto-selection against the hostile schema: SPEC 5.4's nine rules.

Each rule gets its own assertion about the generated document, because the
rules interact and a test that only checked the whole printed output would
not say which rule broke. The property test in
``test_selection_property.py`` covers the rules acting together.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from graphql import (
    DocumentNode,
    FieldNode,
    GraphQLCompositeType,
    GraphQLSchema,
    InlineFragmentNode,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    VariableDefinitionNode,
    VariableNode,
    is_composite_type,
    parse_type,
    print_ast,
    validate,
)
from graphql import (
    build_schema as build_sdl_schema,
)

from pytest_graphql._core.errors import SelectionError, SelectionTooLargeError
from pytest_graphql._core.selection.builder import (
    MAX_CACHE_ENTRIES,
    PAGE_SIZE_VARIABLE,
    BuiltSelection,
    SelectionBuilder,
)
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()


def composite(name: str, schema: GraphQLSchema = SCHEMA) -> GraphQLCompositeType:
    type_ = schema.type_map[name]
    assert is_composite_type(type_)
    return cast(GraphQLCompositeType, type_)


def build(
    type_name: str,
    policy: SelectionPolicy | None = None,
    schema: GraphQLSchema = SCHEMA,
) -> BuiltSelection:
    return SelectionBuilder(schema).build(
        composite(type_name, schema), policy or SelectionPolicy()
    )


def keys(selection_set: SelectionSetNode) -> list[str]:
    """The response keys of the plain fields in a selection set, in order."""
    return [
        selection.name.value
        for selection in selection_set.selections
        if isinstance(selection, FieldNode)
    ]


def child(selection_set: SelectionSetNode, name: str) -> FieldNode:
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode) and selection.name.value == name:
            return selection
    raise AssertionError(f"{name} is not in {keys(selection_set)}")


def fragment(selection_set: SelectionSetNode, type_name: str) -> InlineFragmentNode:
    for selection in selection_set.selections:
        if (
            isinstance(selection, InlineFragmentNode)
            and selection.type_condition is not None
            and selection.type_condition.name.value == type_name
        ):
            return selection
    raise AssertionError(f"no fragment on {type_name}")


def fragment_names(selection_set: SelectionSetNode) -> list[str]:
    return [
        selection.type_condition.name.value
        for selection in selection_set.selections
        if isinstance(selection, InlineFragmentNode)
        and selection.type_condition is not None
    ]


def sub(node: FieldNode) -> SelectionSetNode:
    assert node.selection_set is not None
    return node.selection_set


# Rule 1: depth, and the per-type cap.


def test_depth_one_selects_leaf_fields_only() -> None:
    built = build("User", SelectionPolicy(max_depth=1))
    assert "id" in keys(built.selection_set)
    assert "manager" not in keys(built.selection_set)
    assert "team" not in keys(built.selection_set)


def test_depth_two_expands_one_level_of_objects() -> None:
    built = build("User", SelectionPolicy(max_depth=2, cycle_policy="stop"))
    team = child(built.selection_set, "team")
    assert "name" in keys(sub(team))
    assert "captain" not in keys(sub(team))


def test_a_per_type_cap_lowers_the_budget_below_that_type() -> None:
    """``{"Team": 0}`` means Team expands no further, wherever it appears."""
    policy = SelectionPolicy(
        max_depth=3, cycle_policy="stop", per_type_depth_cap={"Team": 0}
    )
    team = child(build("User", policy).selection_set, "team")
    assert "name" in keys(sub(team))
    assert "captain" not in keys(sub(team))
    assert "members" not in keys(sub(team))


def test_a_per_type_cap_never_raises_the_budget() -> None:
    policy = SelectionPolicy(
        max_depth=1, cycle_policy="stop", per_type_depth_cap={"User": 5}
    )
    assert "team" not in keys(build("User", policy).selection_set)


# Rule 2: cycles, and what is not one.


def test_diamond_reuse_is_not_a_cycle() -> None:
    """Post.author and Post.editor are both User, in sibling branches."""
    built = build("Post", SelectionPolicy(max_depth=2))
    author = keys(sub(child(built.selection_set, "author")))
    editor = keys(sub(child(built.selection_set, "editor")))
    assert author == editor
    assert "name" in author


def test_cycle_policy_stop_omits_the_branch() -> None:
    built = build("User", SelectionPolicy(cycle_policy="stop"))
    assert "manager" not in keys(built.selection_set)


def test_cycle_policy_shallow_emits_leaf_fields() -> None:
    built = build("User", SelectionPolicy(cycle_policy="shallow"))
    manager = keys(sub(child(built.selection_set, "manager")))
    assert "id" in manager
    assert "name" in manager
    assert "team" not in manager


def test_cycle_policy_id_only_emits_the_identity() -> None:
    built = build("User", SelectionPolicy(cycle_policy="id_only"))
    manager = sub(child(built.selection_set, "manager"))
    assert keys(manager) == ["__typename", "id"]


def test_an_indirect_cycle_is_detected() -> None:
    """User -> Team -> User closes through Team.captain."""
    built = build("User", SelectionPolicy(max_depth=3, cycle_policy="id_only"))
    team = sub(child(built.selection_set, "team"))
    captain = sub(child(team, "captain"))
    assert keys(captain) == ["__typename", "id"]


def test_a_cycle_is_measured_along_the_path_and_not_globally() -> None:
    """Team appears under User once; reaching User again is the cycle, not Team."""
    built = build("Team", SelectionPolicy(max_depth=3, cycle_policy="stop"))
    members = sub(child(built.selection_set, "members"))
    assert "name" in keys(members)
    assert "team" not in keys(members)


# Rule 3, widened by A6: required arguments.


def test_a_composite_field_with_a_required_argument_is_skipped() -> None:
    assert "posts" not in keys(build("User").selection_set)


def test_a_leaf_field_with_a_required_argument_is_skipped() -> None:
    """A6: skipping only composite fields would emit an invalid scalar field."""
    root = keys(build("Query").selection_set)
    assert "pyKeyword" in root
    assert "pyKeywordEcho" not in root


def test_an_argument_with_a_default_does_not_skip_the_field() -> None:
    assert "friends" in keys(build("User").selection_set)


# Rule 4, with the C1 conditions: Relay connections.


def test_a_connection_is_expanded_as_the_documented_template() -> None:
    connection = sub(child(build("User").selection_set, "postsConnection"))
    assert keys(sub(child(connection, "pageInfo"))) == [
        "__typename",
        "hasNextPage",
        "endCursor",
    ]
    edges = sub(child(connection, "edges"))
    assert keys(edges) == ["__typename", "cursor", "node"]


def test_the_page_size_travels_as_a_variable_and_not_as_text() -> None:
    built = build("User", SelectionPolicy(connection_page_size=25))
    printed = print_ast(child(built.selection_set, "postsConnection"))
    assert f"first: ${PAGE_SIZE_VARIABLE}" in printed
    assert "25" not in printed
    assert len(built.variables) == 1
    variable = built.variables[0]
    assert variable.name == PAGE_SIZE_VARIABLE
    assert variable.value == 25
    assert str(variable.type_) == "Int!"


def test_no_page_size_variable_when_no_connection_was_expanded() -> None:
    assert build("Settings").variables == ()


UNPAGED_SDL = """
type Query { ping: Boolean!, feed: ItemConnection! }
type ItemConnection { edges: [ItemEdge!]!, pageInfo: PageInfo! }
type ItemEdge { cursor: String!, node: Item! }
type Item { id: ID!, title: String }
type PageInfo { hasNextPage: Boolean!, endCursor: String }
"""


def test_a_connection_field_with_no_page_size_argument_is_skipped() -> None:
    """C1: no field cap can bound the rows such a field returns.

    The hostile schema has no connection field without a page-size argument,
    because C1 postdates it, so this rule is exercised against its own
    minimal schema.
    """
    schema = build_sdl_schema(UNPAGED_SDL)
    root = keys(build("Query", schema=schema).selection_set)
    assert root == ["__typename", "ping"]


def test_a_connection_is_an_ordinary_object_when_relay_is_off() -> None:
    policy = SelectionPolicy(relay_aware=False, cycle_policy="stop")
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    assert print_ast(child(connection, "edges")) != ""
    assert build("User", policy).variables == ()


def test_max_connection_depth_zero_skips_every_connection() -> None:
    policy = SelectionPolicy(max_connection_depth=0)
    assert "postsConnection" not in keys(build("User", policy).selection_set)


def test_edges_and_node_cost_no_depth() -> None:
    """Rule 4: the node's type sits at the connection's own level."""
    policy = SelectionPolicy(max_depth=2, cycle_policy="stop")
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    node = sub(child(sub(child(connection, "edges")), "node"))
    assert "title" in keys(node)


# Rule 5: deprecated fields.


def test_deprecated_fields_are_out_by_default() -> None:
    root = keys(build("User").selection_set)
    assert "oldName" not in root
    assert "legacyManager" not in root


def test_deprecated_fields_come_back_when_asked_for() -> None:
    root = keys(build("User", SelectionPolicy(include_deprecated=True)).selection_set)
    assert "oldName" in root
    assert "legacyManager" in root


# Rule 6: interfaces and unions.


def test_an_interface_emits_its_own_leaves_and_one_fragment_per_member() -> None:
    built = build("Node", SelectionPolicy(max_depth=2, cycle_policy="stop"))
    assert keys(built.selection_set) == ["__typename", "id"]
    assert fragment_names(built.selection_set) == ["User", "Team", "Post"]


def test_a_union_emits_typename_and_one_fragment_per_member() -> None:
    built = build("SearchResult", SelectionPolicy(max_depth=2, cycle_policy="stop"))
    assert keys(built.selection_set) == ["__typename"]
    assert fragment_names(built.selection_set) == ["User", "Team", "Attachment"]


def test_members_past_the_cap_collapse_to_typename_and_id() -> None:
    policy = SelectionPolicy(max_union_members=1, max_depth=2, cycle_policy="stop")
    built = build("SearchResult", policy)
    expanded = fragment(built.selection_set, "User")
    assert "name" in keys(expanded.selection_set)
    collapsed = fragment(built.selection_set, "Team")
    assert keys(collapsed.selection_set) == ["__typename", "id"]


def test_a_collapsed_member_with_no_id_is_typename_alone() -> None:
    policy = SelectionPolicy(max_union_members=0, max_depth=2)
    built = build("SearchResult", policy)
    assert keys(fragment(built.selection_set, "Attachment").selection_set) == [
        "__typename"
    ]


def test_an_inline_fragment_costs_no_depth() -> None:
    """A member expands with the budget the abstract type itself had."""
    policy = SelectionPolicy(max_depth=2, cycle_policy="stop")
    built = build("SearchResult", policy)
    assert "name" in keys(fragment(built.selection_set, "User").selection_set)


# Rule 7: excludes, through the policy hook.


def test_an_exclude_removes_the_field_everywhere_it_matches() -> None:
    built = build("User", SelectionPolicy(exclude=["*.name"], cycle_policy="shallow"))
    assert "name" not in keys(built.selection_set)
    assert "name" not in keys(sub(child(built.selection_set, "manager")))


def test_an_exclude_scoped_to_one_type_leaves_others_alone() -> None:
    policy = SelectionPolicy(exclude=["Team.name"], max_depth=2, cycle_policy="stop")
    built = build("User", policy)
    assert "name" in keys(built.selection_set)
    assert "name" not in keys(sub(child(built.selection_set, "team")))


def test_a_custom_hook_decides_by_position() -> None:
    class OnlyRootName(SelectionPolicy):
        def should_include(
            self,
            parent_type: str,  # noqa: ARG002 -- the hook's contract
            field_name: str,
            path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
            depth: int,
        ) -> bool:
            return field_name != "name" or depth == 0

    with pytest.warns(UserWarning):
        policy = OnlyRootName(max_depth=2, cycle_policy="stop")
    built = build("User", policy)
    assert "name" in keys(built.selection_set)
    assert "name" not in keys(sub(child(built.selection_set, "team")))


# Rule 8: the size guard.


def test_the_size_guard_raises_with_the_documented_message() -> None:
    with pytest.raises(SelectionTooLargeError) as caught:
        build("WideType", SelectionPolicy(max_fields=10))
    message = str(caught.value)
    assert message.startswith("auto-selection of 'WideType' produced 11 fields")
    assert "(limit 10)" in message
    assert "max_depth=2" in message
    assert "per_type_depth_cap" in message
    assert 'fields=["id", "name"]' in message


def test_typename_does_not_count_against_the_size_guard() -> None:
    """Settings has two fields, so a limit of two must be enough."""
    built = build("Settings", SelectionPolicy(max_fields=2))
    assert keys(built.selection_set) == ["__typename", "theme", "locale"]
    with pytest.raises(SelectionTooLargeError):
        build("Settings", SelectionPolicy(max_fields=1))


# Rule 9: nothing to select.


def test_a_type_that_yields_nothing_raises_rather_than_emitting_braces() -> None:
    """Every mutation takes a required argument, so Mutation selects nothing."""
    with pytest.raises(SelectionError) as caught:
        build("Mutation")
    message = str(caught.value)
    assert "auto-selection of 'Mutation' produced no fields" in message
    assert "max_depth=4" in message


def test_excluding_every_field_of_a_type_raises() -> None:
    with pytest.raises(SelectionError):
        build("Settings", SelectionPolicy(exclude=["Settings.*"]))


# B12: __typename everywhere.


def test_typename_is_emitted_on_every_object_selection() -> None:
    built = build("User", SelectionPolicy(max_depth=3, cycle_policy="shallow"))
    printed = print_ast(built.selection_set)
    assert printed.count("__typename") > 1
    assert keys(built.selection_set)[0] == "__typename"
    assert keys(sub(child(built.selection_set, "team")))[0] == "__typename"


# Memoization, and C18.


def test_one_build_is_memoized_per_type_and_fingerprint() -> None:
    builder = SelectionBuilder(SCHEMA)
    policy = SelectionPolicy()
    first = builder.build(composite("User"), policy)
    second = builder.build(composite("User"), policy)
    assert first is second
    assert builder.cache_size == 1

    builder.build(composite("User"), SelectionPolicy(max_depth=2))
    assert builder.cache_size == 2


def test_two_policies_with_equal_fields_share_one_entry() -> None:
    builder = SelectionBuilder(SCHEMA)
    builder.build(composite("User"), SelectionPolicy(max_depth=2))
    builder.build(composite("User"), SelectionPolicy(max_depth=2))
    assert builder.cache_size == 1


class _DropNamed(SelectionPolicy):
    """A custom policy whose decision the base fingerprint cannot see."""

    dropped: str = ""

    def should_include(
        self,
        parent_type: str,  # noqa: ARG002 -- the hook's contract
        field_name: str,
        path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
        depth: int,  # noqa: ARG002 -- the hook's contract
    ) -> bool:
        return field_name != object.__getattribute__(self, "dropped")


def _drop(dropped: str) -> _DropNamed:
    policy = _DropNamed(max_depth=1)
    object.__setattr__(policy, "dropped", dropped)
    return policy


def test_a_custom_policy_is_never_served_an_earlier_instance_entry() -> None:
    """C18's first case, as behaviour: two short-lived instances, one builder."""
    import gc

    builder = SelectionBuilder(SCHEMA)
    with pytest.warns(UserWarning):
        first = _drop("name")
    assert "name" not in keys(builder.build(composite("User"), first).selection_set)
    del first
    gc.collect()

    second = _drop("id")
    second_keys = keys(builder.build(composite("User"), second).selection_set)
    assert "id" not in second_keys
    assert "name" in second_keys
    assert builder.cache_size == 0


def test_a_policy_reading_changing_external_state_is_rebuilt_each_time() -> None:
    """C18's second case: a stable key would freeze the first answer."""
    hidden: set[str] = {"name"}

    class ReadsOutside(SelectionPolicy):
        def should_include(
            self,
            parent_type: str,  # noqa: ARG002 -- the hook's contract
            field_name: str,
            path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
            depth: int,  # noqa: ARG002 -- the hook's contract
        ) -> bool:
            return field_name not in hidden

    builder = SelectionBuilder(SCHEMA)
    with pytest.warns(UserWarning):
        policy = ReadsOutside(max_depth=1)

    assert "name" not in keys(builder.build(composite("User"), policy).selection_set)
    hidden.clear()
    hidden.add("id")
    later = keys(builder.build(composite("User"), policy).selection_set)
    assert "name" in later
    assert "id" not in later
    assert builder.cache_size == 0


def test_a_custom_policy_with_a_fingerprint_is_cached_and_kept_apart() -> None:
    """C18's third case: opting back in, without sharing across values."""

    class Owned(SelectionPolicy):
        version: str = "a"

        def should_include(
            self,
            parent_type: str,  # noqa: ARG002 -- the hook's contract
            field_name: str,
            path: tuple[str, ...],  # noqa: ARG002 -- the hook's contract
            depth: int,  # noqa: ARG002 -- the hook's contract
        ) -> bool:
            return field_name != (
                "name" if object.__getattribute__(self, "version") == "a" else "id"
            )

        @property
        def fingerprint(self) -> str:
            return f"owned-{object.__getattribute__(self, 'version')}"

    def owned(version: str) -> Owned:
        policy = Owned(max_depth=1)
        object.__setattr__(policy, "version", version)
        return policy

    builder = SelectionBuilder(SCHEMA)
    first = builder.build(composite("User"), owned("a"))
    assert builder.build(composite("User"), owned("a")) is first
    assert builder.cache_size == 1

    other = builder.build(composite("User"), owned("b"))
    assert other is not first
    assert "name" not in keys(first.selection_set)
    assert "id" not in keys(other.selection_set)
    assert builder.cache_size == 2


def test_the_walk_keeps_no_state_between_builds() -> None:
    """A shared field counter would make the second build raise."""
    builder = SelectionBuilder(SCHEMA)
    policy = SelectionPolicy(max_fields=400)
    first = builder.build(composite("WideType"), policy)
    second = SelectionBuilder(SCHEMA).build(composite("WideType"), policy)
    assert print_ast(first.selection_set) == print_ast(second.selection_set)


def test_the_hostile_schema_builds_under_the_defaults() -> None:
    """A smoke check that the deepest real type terminates and stays bounded."""
    built: dict[str, Any] = {}
    for name in ("User", "Team", "Post", "Node", "SearchResult", "PostConnection"):
        built[name] = print_ast(build(name).selection_set)
    assert all(text.startswith("{") for text in built.values())


# Shapes the hostile schema does not carry, against their own small schemas.


ODD_SHAPES_SDL = """
type Query {
  root: Thing
  weird(first: Int): WeirdConnection
  thin(first: Int): ThinConnection
  bare(first: Int): BareConnection
}
interface Shape { id: ID!, owner: Thing }
type Thing implements Shape { id: ID!, owner: Thing, back: Shape, tag: String }
type WeirdConnection { edges: String!, pageInfo: Int! }
type ThinConnection { edges: [ThinEdge!]!, pageInfo: ThinPageInfo! }
type ThinEdge { cursor: String! }
type ThinPageInfo { total: Int }
type BareConnection { edges: [BareEdge!]!, pageInfo: ThinPageInfo! }
type BareEdge { other: String }
"""


def odd() -> GraphQLSchema:
    return build_sdl_schema(ODD_SHAPES_SDL)


def test_an_interface_emits_no_composite_field_of_its_own() -> None:
    """Rule 6 says leaf fields; a composite one belongs in the fragments."""
    built = build("Shape", SelectionPolicy(max_depth=2), schema=odd())
    assert keys(built.selection_set) == ["__typename", "id"]
    assert "owner" in keys(fragment(built.selection_set, "Thing").selection_set)


def test_a_member_that_repeats_an_ancestor_takes_the_cycle_policy() -> None:
    """Thing -> Shape -> Thing closes inside the inline fragment."""
    policy = SelectionPolicy(max_depth=4, cycle_policy="id_only")
    back = child(build("Thing", policy, schema=odd()).selection_set, "back")
    assert keys(fragment(sub(back), "Thing").selection_set) == ["__typename", "id"]


def test_a_member_stripped_bare_by_the_policy_is_dropped() -> None:
    policy = SelectionPolicy(max_depth=2, cycle_policy="stop", exclude=["Thing.*"])
    built = build("Shape", policy, schema=odd())
    assert fragment_names(built.selection_set) == []


def test_a_connection_whose_parts_are_not_objects_is_dropped() -> None:
    """The rule 4 heuristic is name-based, so it can meet a type it misread.

    Neither half of the template can be built here, so the field is left out
    rather than emitted with an empty selection set.
    """
    policy = SelectionPolicy(max_depth=3)
    assert "weird" not in keys(build("Query", policy, schema=odd()).selection_set)


def test_a_connection_keeps_the_half_of_the_template_that_exists() -> None:
    """ThinPageInfo carries neither documented field, and ThinEdge has no node."""
    policy = SelectionPolicy(max_depth=3)
    thin = child(build("Query", policy, schema=odd()).selection_set, "thin")
    assert keys(sub(thin)) == ["__typename", "edges"]
    assert keys(sub(child(sub(thin), "edges"))) == ["__typename", "cursor"]


def test_a_connection_whose_edges_carry_neither_cursor_nor_node_is_dropped() -> None:
    policy = SelectionPolicy(max_depth=3)
    assert "bare" not in keys(build("Query", policy, schema=odd()).selection_set)


# Regressions for cycle CR-20260911T184241Z-5c65bfb-682dc35f.


def test_the_memo_cache_is_bounded_and_evicts_the_oldest() -> None:
    """F04: a C18-compliant fingerprint can change on every build."""

    class PerBuild(SelectionPolicy):
        token: str = ""

        @property
        def fingerprint(self) -> str:
            return object.__getattribute__(self, "token")

    def keyed(token: str) -> PerBuild:
        policy = PerBuild(max_depth=1)
        object.__setattr__(policy, "token", token)
        return policy

    builder = SelectionBuilder(SCHEMA, max_entries=4)
    for index in range(50):
        builder.build(composite("User"), keyed(f"token-{index}"))
    assert builder.cache_size == 4


def test_the_default_bound_is_the_stated_one() -> None:
    """Asserted through eviction, so the stated number is the one in force."""
    assert MAX_CACHE_ENTRIES == 128

    class PerBuild(SelectionPolicy):
        token: str = ""

        @property
        def fingerprint(self) -> str:
            return object.__getattribute__(self, "token")

    builder = SelectionBuilder(SCHEMA)
    for index in range(MAX_CACHE_ENTRIES + 40):
        policy = PerBuild(max_depth=1)
        object.__setattr__(policy, "token", f"token-{index}")
        builder.build(composite("Settings"), policy)
    assert builder.cache_size == MAX_CACHE_ENTRIES


def test_a_reused_entry_is_kept_over_a_newer_one() -> None:
    """Eviction is least-recently-used, so a hot entry survives a cold one."""
    builder = SelectionBuilder(SCHEMA, max_entries=2)
    first = SelectionPolicy(max_depth=1)
    second = SelectionPolicy(max_depth=2)
    kept = builder.build(composite("User"), first)
    builder.build(composite("User"), second)
    builder.build(composite("User"), first)
    builder.build(composite("User"), SelectionPolicy(max_depth=3))
    assert builder.build(composite("User"), first) is kept


@pytest.mark.parametrize(
    ("pattern", "gone"),
    [
        ("*.cursor", "cursor"),
        ("PageInfo.hasNextPage", "hasNextPage"),
        ("PageInfo.endCursor", "endCursor"),
    ],
)
def test_rule_seven_reaches_inside_the_relay_template(pattern: str, gone: str) -> None:
    """F05: rule 4 grants the template no exemption from rule 7."""
    policy = SelectionPolicy(exclude=[pattern])
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    assert gone not in print_ast(connection)


def test_excluding_page_info_removes_it_and_keeps_the_rest() -> None:
    policy = SelectionPolicy(exclude=["PostConnection.pageInfo"])
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    assert keys(connection) == ["__typename", "edges"]


def test_excluding_edges_leaves_page_info_behind() -> None:
    policy = SelectionPolicy(exclude=["PostConnection.edges"])
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    assert keys(connection) == ["__typename", "pageInfo"]


def test_excluding_the_whole_template_drops_the_connection_field() -> None:
    policy = SelectionPolicy(exclude=["PostConnection.*"])
    assert "postsConnection" not in keys(build("User", policy).selection_set)


def test_excluding_the_node_leaves_the_cursor_behind() -> None:
    policy = SelectionPolicy(exclude=["PostEdge.node"])
    connection = sub(child(build("User", policy).selection_set, "postsConnection"))
    assert keys(sub(child(connection, "edges"))) == ["__typename", "cursor"]


# C56: AUTO is a scope, and a scope rooted at a connection is one deep already.


NESTED_CONNECTION_SDL = """
type Query { feed(first: Int): OuterConnection }
type OuterConnection { edges: [OuterEdge!]!, pageInfo: PageInfo! }
type OuterEdge { cursor: String!, node: Item! }
type Item { id: ID!, inner(first: Int): InnerConnection }
type InnerConnection { edges: [InnerEdge!]!, pageInfo: PageInfo! }
type InnerEdge { cursor: String!, node: Leaf! }
type Leaf { id: ID! }
type PageInfo { hasNextPage: Boolean!, endCursor: String }
"""


def nested() -> GraphQLSchema:
    return build_sdl_schema(NESTED_CONNECTION_SDL)


def test_a_scope_rooted_at_a_connection_starts_one_connection_deep() -> None:
    """Otherwise AUTO at a connection type would reset the C1 nesting cap."""
    policy = SelectionPolicy(max_depth=4, max_connection_depth=1)
    built = build("OuterConnection", policy, schema=nested())
    assert "inner" not in print_ast(built.selection_set)


def test_that_scope_still_expands_a_nested_connection_when_allowed() -> None:
    policy = SelectionPolicy(max_depth=4, max_connection_depth=2)
    built = build("OuterConnection", policy, schema=nested())
    assert "inner" in print_ast(built.selection_set)


def test_a_scope_rooted_elsewhere_starts_at_zero() -> None:
    """Item is not a connection, so its own connection field is the first."""
    policy = SelectionPolicy(max_depth=4, max_connection_depth=1)
    built = build("Item", policy, schema=nested())
    assert "inner" in print_ast(built.selection_set)


def test_a_built_selection_reports_its_own_field_count() -> None:
    """C56: composition needs the count without rebuilding a cached entry."""
    built = build("Settings")
    assert keys(built.selection_set) == ["__typename", "theme", "locale"]
    assert built.field_count == 2


def count_fields(selection_set: SelectionSetNode) -> int:
    """Every field in a document except ``__typename``, which B12 exempts."""
    total = 0
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            if selection.name.value != "__typename":
                total += 1
            if selection.selection_set is not None:
                total += count_fields(selection.selection_set)
        elif isinstance(selection, InlineFragmentNode):
            total += count_fields(selection.selection_set)
    return total


@pytest.mark.parametrize(
    "type_name", ["User", "Team", "Post", "Node", "SearchResult", "PostConnection"]
)
def test_the_reported_count_is_what_the_document_holds(type_name: str) -> None:
    built = build(type_name, SelectionPolicy(max_depth=3))
    assert built.field_count == count_fields(built.selection_set)


# Regressions for cycle CR-20260911T230447Z-5c65bfb-0a8db502.


REQUIRED_PAGE_SIZE_SDL = """
type Query { ping: String feed(first: Int!): FeedConnection! }
type FeedConnection { edges: [FeedEdge!]! pageInfo: PageInfo! }
type FeedEdge { cursor: String! node: Item! }
type PageInfo { hasNextPage: Boolean! endCursor: String }
type Item { id: ID! name: String! }
"""

LIST_PAGE_SIZE_SDL = REQUIRED_PAGE_SIZE_SDL.replace("first: Int!", "first: [Int]")


def validation_errors(built: BuiltSelection, schema: GraphQLSchema) -> list[str]:
    """Validate a selection set against a schema, with its variables declared.

    Assembling an operation belongs to M4. This is the smallest thing that can
    carry a generated variable into ``validate``, which is where a declared
    type that does not fit its position is reported.
    """
    operation = OperationDefinitionNode(
        operation=OperationType.QUERY,
        name=NameNode(value="Probe"),
        variable_definitions=tuple(
            VariableDefinitionNode(
                variable=VariableNode(name=NameNode(value=generated.name)),
                type=parse_type(str(generated.type_)),
                directives=(),
            )
            for generated in built.variables
        ),
        directives=(),
        selection_set=built.selection_set,
    )
    document = DocumentNode(definitions=(operation,))
    return [error.message for error in validate(schema, document)]


def test_a_required_page_size_argument_still_validates() -> None:
    """F01: the generated variable's type must fit every position it is used at."""
    schema = build_sdl_schema(REQUIRED_PAGE_SIZE_SDL)
    built = build("Query", schema=schema)
    assert f"first: ${PAGE_SIZE_VARIABLE}" in print_ast(built.selection_set)
    assert str(built.variables[0].type_) == "Int!"
    assert validation_errors(built, schema) == []


def test_a_list_shaped_page_size_argument_is_not_one() -> None:
    """A variable of one declared type cannot be valid at ``first: [Int]``."""
    schema = build_sdl_schema(LIST_PAGE_SIZE_SDL)
    built = build("Query", schema=schema)
    assert keys(built.selection_set) == ["__typename", "ping"]
    assert built.variables == ()


TWO_LEAF_INTERFACE_SDL = """
interface Named { id: ID! label: String! }
type Thing implements Named { id: ID! label: String! extra: Int }
type Query { named: Named }
"""


def test_an_interface_emits_every_one_of_its_own_leaf_fields() -> None:
    schema = build_sdl_schema(TWO_LEAF_INTERFACE_SDL)
    built = build("Named", schema=schema)
    assert keys(built.selection_set) == ["__typename", "id", "label"]
