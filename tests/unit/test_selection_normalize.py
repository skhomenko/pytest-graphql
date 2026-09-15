"""The four selection input forms, resolved against the hostile schema.

SPEC 3.3 calls the forms interchangeable, so the first test here is that
equivalent selections written four ways produce one document. The rest is
C7's grammar: how names resolve, how ``+`` and ``-`` behave, which
combinations conflict, and the rule that no user value reaches document text.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

import pytest
from graphql import (
    FieldNode,
    GraphQLArgument,
    GraphQLCompositeType,
    GraphQLField,
    GraphQLFloat,
    GraphQLID,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLResolveInfo,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    OperationDefinitionNode,
    graphql_sync,
    is_composite_type,
    parse,
    print_ast,
    validate_schema,
)
from graphql import (
    build_schema as build_sdl_schema,
)

from pytest_graphql._core.errors import (
    ArgumentError,
    SchemaError,
    SelectionError,
    SelectionTooLargeError,
)
from pytest_graphql._core.selection.builder import (
    PAGE_SIZE_VARIABLE,
    BuiltSelection,
    SelectionBuilder,
)
from pytest_graphql._core.selection.model import (
    AUTO,
    Field,
    InlineFragment,
    Selection,
    SelectionInput,
)
from pytest_graphql._core.selection.normalize import normalize
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()


def composite(name: str, schema: GraphQLSchema = SCHEMA) -> GraphQLCompositeType:
    type_ = schema.type_map[name]
    assert is_composite_type(type_)
    return cast(GraphQLCompositeType, type_)


def run(
    fields: SelectionInput,
    type_name: str = "User",
    policy: SelectionPolicy | None = None,
    schema: GraphQLSchema = SCHEMA,
) -> BuiltSelection:
    return normalize(
        fields,
        schema=schema,
        parent_type=composite(type_name, schema),
        policy=policy or SelectionPolicy(),
        builder=SelectionBuilder(schema),
    )


def text(fields: SelectionInput, type_name: str = "User") -> str:
    return print_ast(run(fields, type_name).selection_set)


# The four forms.


def test_the_four_forms_produce_the_same_document() -> None:
    expected = text(["id", "name", {"team": ["id"]}])
    assert text(["id", "name", Field("team", fields=["id"])]) == expected
    assert text("id name team { id }") == expected
    assert text(Selection("id", "name", Field("team", fields=["id"]))) == expected


def test_auto_delegates_to_the_builder() -> None:
    builder = SelectionBuilder(SCHEMA)
    policy = SelectionPolicy()
    direct = builder.build(composite("User"), policy)
    through = normalize(
        AUTO,
        schema=SCHEMA,
        parent_type=composite("User"),
        policy=policy,
        builder=builder,
    )
    assert through is direct


def test_an_explicit_selection_adds_no_typename() -> None:
    """B12: ``__typename`` is generated, never added to what the user wrote."""
    assert "__typename" not in text(["id", "name"])


def test_typename_is_kept_when_the_user_asks_for_it() -> None:
    assert "__typename" in text(["__typename", "id"])


def test_a_nested_auto_expands_that_field_only() -> None:
    printed = text(["id", Field("team", fields=AUTO)])
    assert printed.startswith("{\n  id\n  team {")
    assert "__typename" in printed


def test_a_nested_auto_carries_its_generated_variable_up() -> None:
    built = run(["id", Field("manager", fields=AUTO)])
    assert [variable.name for variable in built.variables] == [PAGE_SIZE_VARIABLE]
    assert f"first: ${PAGE_SIZE_VARIABLE}" in print_ast(built.selection_set)


def test_auto_inside_a_list_is_refused() -> None:
    with pytest.raises(SelectionError, match="AUTO is a whole selection"):
        run([AUTO, "id"])


# Name resolution (B10).


def test_a_snake_name_resolves_to_the_schema_name() -> None:
    assert text(["joined_at", "external_id"]) == "{\n  joinedAt\n  externalId\n}"


def test_an_exact_name_that_is_also_a_snake_form_wins() -> None:
    """The hostile schema has both ``userId`` and ``user_id``."""
    with pytest.warns(UserWarning):
        printed = text(["user_id"])
    assert printed == "{\n  user_id\n}"
    assert text(["userId"]) == "{\n  userId\n}"


def test_an_unknown_field_names_the_close_match() -> None:
    with pytest.raises(SelectionError) as caught:
        run(["nmae"])
    message = str(caught.value)
    assert "no field 'nmae' on User" in message
    assert "Did you mean 'name'?" in message


def test_an_ambiguous_snake_name_is_an_error_and_not_a_guess() -> None:
    schema = build_sdl_schema(
        "type Query { userID: ID!, userId: ID! }\ntype Other { a: Int }"
    )
    with pytest.raises(SelectionError) as caught:
        run(["user_id"], "Query", schema=schema)
    assert "ambiguous" in str(caught.value)


def test_a_union_has_no_fields_of_its_own() -> None:
    with pytest.raises(SelectionError, match="is a union"):
        run(["id"], "SearchResult")


# Sub-selections.


def test_a_composite_field_without_a_sub_selection_is_refused() -> None:
    with pytest.raises(SelectionError) as caught:
        run(["id", "team"])
    message = str(caught.value)
    assert "needs a sub-selection" in message
    assert "fields=AUTO" in message


def test_a_leaf_field_with_a_sub_selection_is_refused() -> None:
    with pytest.raises(SelectionError, match="which has no fields to select"):
        run([{"name": ["length"]}])


def test_an_empty_selection_is_refused() -> None:
    with pytest.raises(SelectionError, match="is empty"):
        run([])


# Arguments: hoisted, never inlined (C7).


def test_an_argument_value_never_reaches_document_text() -> None:
    built = run([Field("posts", args={"first": 3}, fields=["id"])])
    printed = print_ast(built.selection_set)
    assert "3" not in printed
    assert "first: $posts_first" in printed
    assert len(built.variables) == 1
    variable = built.variables[0]
    assert variable.name == "posts_first"
    assert variable.value == 3
    assert str(variable.type_) == "Int!"


def test_a_generated_variable_is_named_from_the_path_and_the_argument() -> None:
    built = run([Field("team", fields=[Field("members", fields=["id"])])], "User")
    assert built.variables == ()

    nested = run(
        [Field("manager", fields=[Field("posts", args={"first": 2}, fields=["id"])])]
    )
    assert [variable.name for variable in nested.variables] == ["manager_posts_first"]


def test_a_schema_field_name_is_the_path_segment_not_the_alias() -> None:
    # C56: a path holds GraphQL field names, and a generated variable name is
    # derived from a path. An alias is the caller's label for the result.
    built = run([Field("posts", args={"first": 3}, alias="recent", fields=["id"])])
    assert [variable.name for variable in built.variables] == ["posts_first"]


COLLIDING_SDL = """
type Query { a(b_c: Int): Int, a_b(c: Int): Int }
"""


def test_two_paths_wanting_one_variable_name_get_a_counter_suffix() -> None:
    schema = build_sdl_schema(COLLIDING_SDL)
    built = run(
        [Field("a", args={"b_c": 1}), Field("a_b", args={"c": 2})],
        "Query",
        schema=schema,
    )
    assert [variable.name for variable in built.variables] == ["a_b_c", "a_b_c_2"]
    assert [variable.value for variable in built.variables] == [1, 2]


def test_an_argument_name_resolves_by_lookup_like_a_field_name() -> None:
    schema = build_sdl_schema("type Query { search(firstName: String): Int }")
    built = run([Field("search", args={"first_name": "a"})], "Query", schema=schema)
    assert "firstName: $search_firstName" in print_ast(built.selection_set)
    assert [variable.value for variable in built.variables] == ["a"]


def test_an_unknown_argument_names_the_signature_and_the_close_match() -> None:
    with pytest.raises(ArgumentError) as caught:
        run([Field("posts", args={"frist": 1}, fields=["id"])])
    message = str(caught.value)
    assert "has no argument 'frist'" in message
    assert "Signature: posts(first: Int!): [Post!]!" in message
    assert "Did you mean 'first'?" in message


def test_a_raw_string_keeps_the_arguments_the_user_typed() -> None:
    """There is no Python value to hoist: the user wrote the document text."""
    built = run("posts(first: 3) { id }")
    assert "first: 3" in print_ast(built.selection_set)
    assert built.variables == ()


# Merging and conflicts (C7).


def test_adding_two_selections_unions_them_recursively() -> None:
    brief = Selection("id", Field("team", fields=["id"]))
    full = brief + Selection("name", Field("team", fields=["name"]))
    assert text(full) == "{\n  id\n  team {\n    id\n    name\n  }\n  name\n}"


def test_the_same_field_written_twice_merges() -> None:
    assert text(["id", "id"]) == "{\n  id\n}"


def test_two_fields_sharing_a_key_with_different_arguments_conflict() -> None:
    with pytest.raises(SelectionError) as caught:
        run(
            [
                Field("posts", args={"first": 1}, fields=["id"]),
                Field("posts", args={"first": 2}, fields=["id"]),
            ]
        )
    message = str(caught.value)
    assert "share the response key 'posts'" in message
    assert "Give one of them an alias." in message


def test_two_different_fields_sharing_an_alias_conflict() -> None:
    with pytest.raises(SelectionError, match="'x' is written twice"):
        run([Field("id", alias="x"), Field("name", alias="x")])


def test_the_same_alias_written_twice_raises() -> None:
    # C7: an alias must be unique within its selection set, so a duplicate is
    # reported even when the two requests would have merged cleanly.
    with pytest.raises(SelectionError, match="can be used only once"):
        run([Field("id", alias="same"), Field("id", alias="same")])


def test_an_alias_equal_to_the_field_name_still_conflicts() -> None:
    with pytest.raises(SelectionError, match="'name' is written twice"):
        run([Field("name"), Field("name", alias="name")])


def test_an_alias_keeps_two_argument_sets_apart() -> None:
    printed = text(
        [
            Field("posts", args={"first": 1}, alias="one", fields=["id"]),
            Field("posts", args={"first": 2}, alias="two", fields=["id"]),
        ]
    )
    # Both are named from the same schema field path, so the allocator's
    # collision counter is what keeps them apart.
    assert "one: posts(first: $posts_first)" in printed
    assert "two: posts(first: $posts_first_2)" in printed


def test_a_field_cannot_be_both_automatic_and_explicit() -> None:
    with pytest.raises(SelectionError, match="automatically and explicitly"):
        run([Field("team", fields=AUTO), Field("team", fields=["id"])])


# Removal (C7).


def test_a_removal_takes_out_a_response_key() -> None:
    assert text(Selection("id", "name") - "name") == "{\n  id\n}"


def test_a_removal_takes_a_dotted_path() -> None:
    selection = Selection("id", Field("team", fields=["id", "name"])) - "team.name"
    assert text(selection) == "{\n  id\n  team {\n    id\n  }\n}"


def test_removing_something_that_is_not_there_raises() -> None:
    with pytest.raises(SelectionError) as caught:
        run(Selection("id") - "name")
    message = str(caught.value)
    assert "cannot remove 'name'" in message
    assert "It holds: id" in message


def test_a_removal_applies_to_the_union_it_was_written_against() -> None:
    left = Selection("id", "name")
    right = Selection("userId")
    assert text((left + right) - "name") == "{\n  id\n  userId\n}"


def test_a_removal_written_before_a_union_does_not_reach_across_it() -> None:
    removed = (Selection("id", "name") - "name") + Selection("name")
    assert text(removed) == "{\n  id\n  name\n}"


def test_removing_from_a_field_with_no_listed_sub_selection_raises() -> None:
    with pytest.raises(SelectionError, match="no listed sub-selection"):
        run(Selection(Field("team", fields=AUTO)) - "team.id")


# Inline fragments and Selection.of.


def test_selection_of_emits_an_inline_fragment() -> None:
    printed = text(Selection("__typename", Selection.of("User", "name")), "Node")
    assert "... on User {" in printed
    assert "name" in printed


def test_a_fragment_on_an_unknown_type_suggests_a_name() -> None:
    with pytest.raises(SchemaError) as caught:
        run(Selection.of("Usre", "id"), "Node")
    message = str(caught.value)
    assert "no type named 'Usre'" in message
    assert "Did you mean 'User'?" in message


def test_a_fragment_on_a_type_that_cannot_appear_here_raises() -> None:
    with pytest.raises(SchemaError, match="is not a possible type of 'User'"):
        run(Selection.of("Team", "id"), "User")


def test_a_fragment_on_the_parent_type_itself_is_allowed() -> None:
    assert "... on User {" in text(Selection.of("User", "id"), "User")


def test_two_fragments_on_one_type_merge() -> None:
    selection = Selection(
        "__typename", Selection.of("User", "id"), Selection.of("User", "name")
    )
    printed = text(selection, "Node")
    assert printed.count("... on User") == 1
    assert "id" in printed
    assert "name" in printed


def test_a_field_with_a_type_condition_becomes_a_fragment() -> None:
    printed = text(Selection("__typename", Field("name", on="User")), "Node")
    assert "... on User {" in printed
    assert "name" in printed


def test_a_raw_inline_fragment_resolves_like_the_object_form() -> None:
    assert text("__typename ... on User { name }", "Node") == text(
        Selection("__typename", Selection.of("User", "name")), "Node"
    )


# Things v0.1 does not support.


def test_a_directive_in_a_raw_string_names_the_escape_hatch() -> None:
    with pytest.raises(SelectionError) as caught:
        run("id @include(if: true)")
    assert "gql.execute(...)" in str(caught.value)


def test_a_named_fragment_spread_is_refused() -> None:
    with pytest.raises(SelectionError) as caught:
        run("id ...UserParts")
    message = str(caught.value)
    assert "named fragment 'UserParts'" in message
    assert "Selection.of(TypeName)" in message


def test_an_unparsable_selection_string_says_so() -> None:
    with pytest.raises(SelectionError, match="could not parse"):
        run("id {{{")


def test_something_that_is_not_a_selection_lists_what_is() -> None:
    with pytest.raises(SelectionError, match="is not a selection"):
        run(42)  # type: ignore[arg-type]


# Shapes the first round of tests left unexercised.


def test_typename_written_as_a_field_object_is_kept() -> None:
    assert text([Field("__typename"), "id"]) == "{\n  __typename\n  id\n}"


def test_typename_can_be_aliased() -> None:
    assert "kind: __typename" in text([Field("__typename", alias="kind")])


def test_a_fragment_can_take_the_generated_selection() -> None:
    printed = text(Selection("__typename", Selection.of("User")), "Node")
    assert "... on User {" in printed
    assert "joinedAt" in printed


def test_a_fragment_cannot_be_both_automatic_and_explicit() -> None:
    selection = Selection(
        "__typename", Selection.of("User"), Selection.of("User", "id")
    )
    with pytest.raises(SelectionError, match="once automatically and once explicitly"):
        run(selection, "Node")


def test_a_raw_fragment_cannot_join_an_automatic_one() -> None:
    selection = Selection("__typename", Selection.of("User"), "... on User { id }")
    with pytest.raises(SelectionError, match="once automatically and once explicitly"):
        run(selection, "Node")


def test_a_raw_inline_fragment_without_a_condition_is_the_parent() -> None:
    assert text("... { id name }") == "{\n  id\n  name\n}"


def test_a_raw_leaf_field_with_a_sub_selection_is_refused() -> None:
    with pytest.raises(SelectionError, match="which has no fields to select"):
        run("name { length }")


def test_a_bare_field_gains_the_sub_selection_written_beside_it() -> None:
    """``["team", {"team": ["id"]}]`` is one field written in two places."""
    assert text(["team", {"team": ["id"]}]) == "{\n  team {\n    id\n  }\n}"


LIST_ARGUMENT_SDL = """
type Query { search(terms: [String!]): Int }
"""


def test_a_list_argument_value_is_hoisted_whole() -> None:
    schema = build_sdl_schema(LIST_ARGUMENT_SDL)
    built = run([Field("search", args={"terms": ["a", "b"]})], "Query", schema=schema)
    assert "terms: $search_terms" in print_ast(built.selection_set)
    assert built.variables[0].value == ["a", "b"]


def test_two_equal_argument_mappings_built_differently_still_merge() -> None:
    """The conflict test compares canonical values, not dict insertion order."""
    schema = build_sdl_schema("type Query { f(a: Int, b: Int): Int }")
    printed = print_ast(
        run(
            [Field("f", args={"a": 1, "b": 2}), Field("f", args={"b": 2, "a": 1})],
            "Query",
            schema=schema,
        ).selection_set
    )
    assert printed.count("f(") == 1


# Regressions for cycle CR-20260911T184241Z-5c65bfb-682dc35f.


INJECTION_ALIAS = 'ok: pingScalar\n  exfil: user(id: "1") { id name }\n  z'


def test_an_alias_that_is_not_a_graphql_name_is_refused_at_construction() -> None:
    """F01: an alias is the one user string with no schema to resolve against."""
    with pytest.raises(SelectionError) as caught:
        Field("pingScalar", alias=INJECTION_ALIAS)
    message = str(caught.value)
    assert "is not a GraphQL name" in message
    assert "starts with a letter or an underscore" in message


@pytest.mark.parametrize(
    "alias",
    [
        INJECTION_ALIAS,
        "bad name $x",
        "a } b {",
        "",
        "1leading",
        "with-hyphen",
        "trailing\u200b",
        # A pattern anchored with ``$`` accepts these three, because ``$``
        # also matches just before a final newline.
        "ok\n",
        "ok\r\n",
        'ok\n  exfil: user(id: "1") { id }',
    ],
)
def test_no_alias_can_add_text_to_the_document(alias: str) -> None:
    with pytest.raises(SelectionError):
        Field("id", alias=alias)


@pytest.mark.parametrize("alias", ["ok", "_private", "camelCase", "snake_case", "a1"])
def test_a_well_formed_alias_is_accepted(alias: str) -> None:
    assert Field("id", alias=alias).alias == alias


def test_the_printed_document_re_parses_to_what_was_asked_for() -> None:
    """F01's regression has to re-parse: validate() cannot see into a name.

    An injected payload sits inside one opaque name node, so an AST-level
    check passes on the very document this test exists to reject. Printing and
    re-parsing is what compares the wire document with the request.
    """
    built = run([Field("id", alias="ok"), "name"])
    reparsed = parse("query Probe " + print_ast(built.selection_set))
    definition = reparsed.definitions[0]
    assert isinstance(definition, OperationDefinitionNode)
    selections = definition.selection_set.selections
    assert len(selections) == 2
    assert [selection.name.value for selection in selections] == ["id", "name"]


def test_an_auto_connection_reached_through_an_explicit_field_is_bounded() -> None:
    """F02: C1's bound belongs to the field, not to the path that expanded it."""
    built = run([Field("postsConnection", fields=AUTO)])
    printed = print_ast(built.selection_set)
    assert f"postsConnection(first: ${PAGE_SIZE_VARIABLE})" in printed
    assert [variable.name for variable in built.variables] == [PAGE_SIZE_VARIABLE]
    assert built.variables[0].value == 10


def test_a_caller_supplied_page_size_wins_over_the_generated_one() -> None:
    built = run([Field("postsConnection", args={"first": 3}, fields=AUTO)])
    outer = built.selection_set.selections[0]
    assert isinstance(outer, FieldNode)
    assert [print_ast(argument) for argument in outer.arguments] == [
        "first: $postsConnection_first"
    ]
    assert built.variables[0].value == 3


UNPAGED_CONNECTION_SDL = """
type Query { feed: FeedConnection! }
type FeedConnection { edges: [FeedEdge!]!, pageInfo: FeedPageInfo! }
type FeedEdge { cursor: String!, node: Item! }
type Item { id: ID! }
type FeedPageInfo { hasNextPage: Boolean!, endCursor: String }
"""


def test_auto_on_a_connection_with_no_page_size_argument_is_refused() -> None:
    schema = build_sdl_schema(UNPAGED_CONNECTION_SDL)
    with pytest.raises(SelectionError) as caught:
        run([Field("feed", fields=AUTO)], "Query", schema=schema)
    message = str(caught.value)
    assert "cannot be bounded" in message
    assert "relay_aware=False" in message


def test_relay_unaware_policies_leave_an_auto_connection_alone() -> None:
    schema = build_sdl_schema(UNPAGED_CONNECTION_SDL)
    policy = SelectionPolicy(relay_aware=False)
    built = run([Field("feed", fields=AUTO)], "Query", policy=policy, schema=schema)
    assert "edges" in print_ast(built.selection_set)


def test_one_field_written_in_two_input_forms_merges() -> None:
    """F06: the four forms are interchangeable, so they must compare alike."""
    selection = Selection(
        Field("posts", args={"first": 1}, fields=["id"]),
        "posts(first: 1) { id }",
    )
    printed = text(selection)
    assert printed.count("posts(") == 1
    assert "first: $posts_first" in printed


def test_two_input_forms_asking_for_different_arguments_still_conflict() -> None:
    selection = Selection(
        Field("posts", args={"first": 1}, fields=["id"]),
        "posts(first: 2) { id }",
    )
    with pytest.raises(SelectionError, match="share the response key 'posts'"):
        run(selection)


def test_a_typo_in_an_argument_name_is_reported_even_with_a_mapping_value() -> None:
    """The merge signature must not turn a typo into a key conflict."""
    with pytest.raises(ArgumentError, match="has no argument 'inpt'"):
        run(
            [Field("createPost", args={"inpt": {"title": "x"}}, fields=["id"])],
            "Mutation",
        )


def test_two_mistyped_arguments_with_equal_values_merge_to_one_error() -> None:
    """An unresolved name still needs a signature that compares by value."""
    selection = Selection(
        Field("createPost", args={"inpt": ["a", "b"]}, fields=["id"]),
        Field("createPost", args={"inpt": ["a", "b"]}, fields=["id"]),
    )
    with pytest.raises(ArgumentError, match="has no argument 'inpt'"):
        run(selection, "Mutation")


def test_an_auto_fragment_on_a_connection_is_bounded_too() -> None:
    """F02's other way in: the template is generated by the fragment, not the field."""
    built = run([Field("postsConnection", fields=Selection.of("PostConnection"))])
    printed = print_ast(built.selection_set)
    assert f"postsConnection(first: ${PAGE_SIZE_VARIABLE})" in printed
    assert [variable.name for variable in built.variables] == [PAGE_SIZE_VARIABLE]


def test_a_connection_named_without_a_sub_selection_still_asks_for_one() -> None:
    with pytest.raises(SelectionError, match="needs a sub-selection"):
        run(["postsConnection"])


# C56: AUTO is a scope, and the size guard counts the whole document.


def test_an_inline_fragment_adds_no_path_component_to_a_variable_name() -> None:
    """C56: a path holds field names, and a fragment is not a field."""
    selection = Selection(
        "__typename",
        Selection.of("User", Field("posts", args={"first": 1}, fields=["id"])),
    )
    built = run(selection, "Node")
    assert [variable.name for variable in built.variables] == ["posts_first"]


def test_an_explicit_prefix_does_not_consume_the_auto_depth_budget() -> None:
    """C56: each AUTO measures max_depth from its own root, not the document."""
    policy = SelectionPolicy(max_depth=1, cycle_policy="stop")
    direct = run([Field("manager", fields=AUTO)], policy=policy)
    nested = run(
        [Field("manager", fields=[Field("manager", fields=AUTO)])], policy=policy
    )
    assert "name" in print_ast(direct.selection_set)
    assert "name" in print_ast(nested.selection_set)


def test_a_nested_auto_at_a_connection_keeps_the_nesting_cap() -> None:
    """C56 point 5, reached the way a caller reaches it."""
    schema = build_sdl_schema(
        """
        type Query { item: Item }
        type Item { id: ID!, outer(first: Int): OuterConnection }
        type OuterConnection { edges: [OuterEdge!]!, pageInfo: PageInfo! }
        type OuterEdge { cursor: String!, node: Inner! }
        type Inner { id: ID!, inner(first: Int): OuterConnection }
        type PageInfo { hasNextPage: Boolean!, endCursor: String }
        """
    )
    built = run(
        [Field("outer", fields=AUTO)],
        "Item",
        policy=SelectionPolicy(max_depth=4, max_connection_depth=1),
        schema=schema,
    )
    assert "inner" not in print_ast(built.selection_set)


def test_the_size_guard_counts_explicit_fields_and_every_auto_together() -> None:
    """C56: max_fields guards the document, not one scope of it."""
    one = run([Field("team", fields=AUTO)])
    assert one.field_count > 1

    policy = SelectionPolicy(max_fields=one.field_count + 2)
    with pytest.raises(SelectionTooLargeError) as caught:
        run([Field("team", fields=AUTO), Field("manager", fields=AUTO)], policy=policy)
    message = str(caught.value)
    assert "auto-selection of 'User'" in message
    assert f"(limit {one.field_count + 2})" in message


def test_a_normalized_selection_reports_the_count_it_charged() -> None:
    built = run(["id", "name", {"team": ["id"]}])
    assert built.field_count == 4


def test_typename_is_not_charged_even_when_the_caller_wrote_it() -> None:
    assert run(["__typename", "id"]).field_count == 1


def test_a_cached_auto_is_still_charged_to_the_document_it_joins() -> None:
    """The count travels with the cached entry, so no rebuild is needed."""
    builder = SelectionBuilder(SCHEMA)
    first = normalize(
        [Field("team", fields=AUTO)],
        schema=SCHEMA,
        parent_type=composite("User"),
        policy=SelectionPolicy(),
        builder=builder,
    )
    assert builder.cache_size == 1
    second = normalize(
        [Field("team", fields=AUTO)],
        schema=SCHEMA,
        parent_type=composite("User"),
        policy=SelectionPolicy(),
        builder=builder,
    )
    assert second.field_count == first.field_count
    assert second.field_count > 1


# Regressions for cycle CR-20260911T230447Z-5c65bfb-0a8db502.


def test_an_aliased_field_with_arguments_re_parses_to_what_was_asked_for() -> None:
    """F02: the generated variable name is the other route a name takes.

    An alias reaches the document twice: as the alias itself, and as a segment
    of the name of any variable generated under it. Both sit inside an opaque
    name node, so printing and re-parsing is the only check that sees them.
    """
    built = run([Field("posts", args={"first": 3}, alias="recent", fields=["id"])])
    printed = print_ast(built.selection_set)
    declared = ", ".join(f"${name.name}: Int" for name in built.variables)
    reparsed = parse(f"query Probe({declared}) " + printed)
    definition = reparsed.definitions[0]
    assert isinstance(definition, OperationDefinitionNode)
    selections = definition.selection_set.selections
    assert len(selections) == 1
    field = selections[0]
    assert isinstance(field, FieldNode)
    assert field.alias is not None
    assert field.alias.value == "recent"
    assert field.name.value == "posts"


def test_a_nested_auto_connection_with_a_required_page_size_validates() -> None:
    """F01: the explicit entry point declares the same variable as the walk."""
    schema = build_sdl_schema(
        """
        type Query { ping: String feed(first: Int!): FeedConnection! }
        type FeedConnection { edges: [FeedEdge!]! pageInfo: PageInfo! }
        type FeedEdge { cursor: String! node: Item! }
        type PageInfo { hasNextPage: Boolean! endCursor: String }
        type Item { id: ID! name: String! }
        """
    )
    built = run([Field("feed", fields=AUTO)], "Query", schema=schema)
    assert [variable.name for variable in built.variables] == [PAGE_SIZE_VARIABLE]
    assert str(built.variables[0].type_) == "Int!"


# F03: two forms of one value have to compare equal after the schema has
# coerced them, not before. Each case writes the same request twice.

PYTHON_INPUT = {"title": "x", "authorId": "1"}

EQUIVALENT_FORMS = [
    pytest.param('createPost(input: {title: "x", authorId: "1"})', id="same-literals"),
    pytest.param('createPost(input: {title: "x", authorId: 1})', id="id-as-integer"),
    pytest.param('createPost(input: {authorId: "1", title: "x"})', id="reordered"),
    pytest.param('createPost(input: {title: """x""", authorId: 1})', id="block-string"),
]


@pytest.mark.parametrize("raw_form", EQUIVALENT_FORMS)
def test_two_forms_of_one_argument_value_merge(raw_form: str) -> None:
    selection = Selection(
        Field("createPost", args={"input": PYTHON_INPUT}, fields=["id"]),
        raw_form + " { id }",
    )
    printed = print_ast(run(selection, "Mutation").selection_set)
    assert printed.count("createPost(") == 1


def test_two_forms_of_a_list_argument_merge() -> None:
    schema = build_sdl_schema(LIST_ARGUMENT_SDL)
    selection = Selection(
        Field("search", args={"terms": ["a", "b"]}),
        'search(terms: ["a", "b"])',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("search(") == 1


def test_two_forms_of_an_enum_argument_merge() -> None:
    schema = build_sdl_schema(
        """
        enum Colour { RED GREEN }
        type Query { paint(colour: Colour): Int }
        """
    )
    selection = Selection(
        Field("paint", args={"colour": "RED"}),
        "paint(colour: RED)",
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("paint(") == 1


def test_two_genuinely_different_values_still_conflict() -> None:
    """Coercion must not collapse requests that differ."""
    selection = Selection(
        Field("createPost", args={"input": {"title": "x", "authorId": "1"}}),
        'createPost(input: {title: "x", authorId: "2"})',
    )
    with pytest.raises(SelectionError, match="share the response key 'createPost'"):
        run(selection, "Mutation")


def test_a_variable_reference_in_a_raw_argument_is_compared_as_written() -> None:
    """A variable means nothing outside its own document, so it is not coerced."""
    selection = Selection(
        "posts(first: $outer) { id }",
        "posts(first: $outer) { id }",
    )
    printed = print_ast(run(selection).selection_set)
    assert printed.count("posts(") == 1


def test_a_raw_argument_the_field_does_not_declare_is_compared_as_written() -> None:
    """An unknown name has no type to coerce through, so it keeps its text."""
    selection = Selection("posts(nope: 1) { id }", "posts(nope: 1) { id }")
    printed = print_ast(run(selection).selection_set)
    assert printed.count("posts(") == 1
    assert "nope: 1" in printed


def test_two_raw_arguments_with_different_unknown_names_still_conflict() -> None:
    selection = Selection("posts(nope: 1) { id }", "posts(other: 1) { id }")
    with pytest.raises(SelectionError, match="share the response key 'posts'"):
        run(selection)


NESTED_INPUT_SDL = """
input Inner { id: ID! flag: Boolean = false }
input Outer { name: String! inner: Inner! }
type Query { save(data: Outer!): Int }
"""


def test_two_forms_of_a_nested_input_object_merge() -> None:
    """Coercion is recursive, so a nested object canonicalizes too."""
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    selection = Selection(
        Field("save", args={"data": {"name": "n", "inner": {"id": "7"}}}),
        'save(data: {inner: {flag: false, id: 7}, name: "n"})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("save(") == 1


def test_a_difference_deep_inside_a_nested_input_object_still_conflicts() -> None:
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    selection = Selection(
        Field("save", args={"data": {"name": "n", "inner": {"id": "7"}}}),
        'save(data: {name: "n", inner: {id: 8}})',
    )
    with pytest.raises(SelectionError, match="share the response key 'save'"):
        run(selection, "Query", schema=schema)


def test_a_python_value_with_no_literal_form_compares_by_value() -> None:
    """A value the type cannot write as a literal still needs a signature.

    ``None`` has no literal at a non-null argument. The value is wrong and
    M4's validation reports it. Until then it must not look equal to a
    different wrong value, and must not conflict with itself.
    """
    same = Selection(
        Field("posts", args={"first": None}, fields=["id"]),
        Field("posts", args={"first": None}, fields=["id"]),
    )
    assert print_ast(run(same).selection_set).count("posts(") == 1
    different = Selection(
        Field("posts", args={"first": None}, fields=["id"]),
        Field("posts", args={"first": 1}, fields=["id"]),
    )
    with pytest.raises(SelectionError, match="share the response key 'posts'"):
        run(different)


# CR-20260911T233259Z-5c65bfb-6fe9317e-F01: the field-level page-size condition
# is checked before anything the caller supplied.

LIST_PAGE_SIZE_CONNECTION_SDL = """
type Query { feed(first: [Int]): FeedConnection! }
type FeedConnection { edges: [FeedEdge!]! pageInfo: PageInfo! }
type FeedEdge { cursor: String! node: Item! }
type PageInfo { hasNextPage: Boolean! endCursor: String }
type Item { id: ID! name: String! }
"""


def test_a_connection_whose_page_size_has_the_wrong_shape_is_refused() -> None:
    schema = build_sdl_schema(LIST_PAGE_SIZE_CONNECTION_SDL)
    with pytest.raises(SelectionError, match="of type Int or Int!"):
        run([Field("feed", fields=AUTO)], "Query", schema=schema)


def test_a_supplied_page_size_cannot_bound_an_argument_of_the_wrong_shape() -> None:
    """C1's bound is a property of the field, not of what the caller wrote.

    The caller's own page size wins over the generated value. It must not win
    over the condition that the field can be bounded at all, or the result
    would depend on whether the caller supplied the incompatible argument.
    """
    schema = build_sdl_schema(LIST_PAGE_SIZE_CONNECTION_SDL)
    with pytest.raises(SelectionError, match="of type Int or Int!"):
        run(
            [Field("feed", args={"first": [10]}, fields=AUTO)],
            "Query",
            schema=schema,
        )


def test_the_fragment_route_to_the_template_refuses_it_too() -> None:
    schema = build_sdl_schema(LIST_PAGE_SIZE_CONNECTION_SDL)
    with pytest.raises(SelectionError, match="of type Int or Int!"):
        run(
            [
                Field(
                    "feed",
                    args={"first": [10]},
                    fields=[InlineFragment("FeedConnection", fields=AUTO)],
                )
            ],
            "Query",
            schema=schema,
        )


def test_a_supplied_page_size_of_the_right_shape_still_wins() -> None:
    schema = build_sdl_schema(
        LIST_PAGE_SIZE_CONNECTION_SDL.replace("first: [Int]", "first: Int")
    )
    built = run([Field("feed", args={"first": 3}, fields=AUTO)], "Query", schema=schema)
    printed = print_ast(built.selection_set)
    assert PAGE_SIZE_VARIABLE not in printed
    assert [variable.value for variable in built.variables] == [3]


# CR-20260911T233259Z-5c65bfb-6fe9317e-F02: the merge key is built from the
# names the caller wrote, never from a coerced input object.


def out_name_schema(**extra: Any) -> GraphQLSchema:
    """A schema whose input field carries a resolver-side name of its own.

    ``out_name`` renames an input field on the way to a resolver, and
    ``out_type`` replaces the coerced mapping with an object of the schema's
    choosing. Neither can be written in SDL, so this schema is built by hand.
    Both are graphql-core's way of transforming a coerced input object, which
    is why neither may take part in deciding whether two selections are equal.
    """
    inner = GraphQLInputObjectType(
        "Inner",
        {"camelName": GraphQLInputField(GraphQLString, out_name="snake_name")},
        **extra,
    )
    query = GraphQLObjectType(
        "Query",
        {"f": GraphQLField(GraphQLString, args={"input": GraphQLArgument(inner)})},
    )
    return GraphQLSchema(query=query)


def test_two_different_values_under_an_out_name_field_conflict() -> None:
    schema = out_name_schema()
    selection = Selection(
        Field("f", args={"input": {"camelName": "x"}}),
        Field("f", args={"input": {"camelName": "y"}}),
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(selection, "Query", schema=schema)


def test_two_equal_values_under_an_out_name_field_still_merge() -> None:
    schema = out_name_schema()
    selection = Selection(
        Field("f", args={"input": {"camelName": "x"}}),
        'f(input: {camelName: "x"})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("f(") == 1


def test_a_custom_out_type_does_not_decide_equality_either() -> None:
    schema = out_name_schema(out_type=lambda mapping: tuple(sorted(mapping)))
    selection = Selection(
        'f(input: {camelName: "x"})',
        'f(input: {camelName: "y"})',
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(selection, "Query", schema=schema)


def test_a_raw_input_object_with_an_undeclared_field_is_compared_as_written() -> None:
    """An invalid literal keeps its text so that validation still sees it."""
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    valid = 'save(data: {name: "n", inner: {id: 7}})'
    invalid = 'save(data: {name: "n", nope: 1, inner: {id: 7}})'
    with pytest.raises(SelectionError, match="share the response key 'save'"):
        run(Selection(valid, invalid), "Query", schema=schema)
    repeated_run = run(Selection(invalid, invalid), "Query", schema=schema)
    printed = print_ast(repeated_run.selection_set)
    assert printed.count("save(") == 1
    assert "nope: 1" in printed


def test_a_raw_input_object_with_a_repeated_field_is_compared_as_written() -> None:
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    valid = 'save(data: {name: "n", inner: {id: 7}})'
    repeated = 'save(data: {name: "n", inner: {id: 0, id: 7}})'
    with pytest.raises(SelectionError, match="share the response key 'save'"):
        run(Selection(valid, repeated), "Query", schema=schema)


def test_a_raw_input_object_missing_a_required_field_is_compared_as_written() -> None:
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    valid = 'save(data: {name: "n", inner: {id: 7}})'
    incomplete = "save(data: {inner: {id: 7}})"
    with pytest.raises(SelectionError, match="share the response key 'save'"):
        run(Selection(valid, incomplete), "Query", schema=schema)


def test_a_python_input_object_with_an_undeclared_key_is_compared_as_written() -> None:
    """``ast_from_value`` drops the key, so the literal cannot tell them apart."""
    schema = build_sdl_schema(NESTED_INPUT_SDL)
    valid = {"name": "n", "inner": {"id": "7"}}
    invalid = {"name": "n", "inner": {"id": "7", "nope": 1}}
    with pytest.raises(SelectionError, match="share the response key 'save'"):
        run(
            Selection(
                Field("save", args={"data": valid}),
                Field("save", args={"data": invalid}),
            ),
            "Query",
            schema=schema,
        )


def test_a_python_list_of_input_objects_checks_every_item() -> None:
    schema = build_sdl_schema(
        """
        input Tag { name: String }
        type Query { tag(tags: [Tag!]): Int }
        """
    )
    good = [{"name": "a"}, {"name": "b"}]
    bad = [{"name": "a"}, {"name": "b", "nope": 1}]
    with pytest.raises(SelectionError, match="share the response key 'tag'"):
        run(
            Selection(
                Field("tag", args={"tags": good}),
                Field("tag", args={"tags": bad}),
            ),
            "Query",
            schema=schema,
        )


def test_the_one_item_list_shorthand_matches_the_written_list() -> None:
    """GraphQL accepts a bare value at a list position, so the two are equal."""
    schema = build_sdl_schema(LIST_ARGUMENT_SDL)
    selection = Selection(
        Field("search", args={"terms": ["a"]}),
        'search(terms: "a")',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("search(") == 1


def test_a_null_inside_a_non_null_list_item_is_compared_as_written() -> None:
    schema = build_sdl_schema(LIST_ARGUMENT_SDL)
    with pytest.raises(SelectionError, match="share the response key 'search'"):
        run(
            Selection('search(terms: ["a"])', "search(terms: [null])"),
            "Query",
            schema=schema,
        )


OPTIONAL_FIELD_SDL = """
input Note { body: String tag: String }
type Query { note(note: Note): Int free(text: String): Int }
"""


def test_an_omitted_optional_field_with_no_default_is_simply_absent() -> None:
    schema = build_sdl_schema(OPTIONAL_FIELD_SDL)
    selection = Selection(
        Field("note", args={"note": {"body": "b"}}),
        'note(note: {body: "b"})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("note(") == 1


def test_an_omitted_optional_field_is_not_the_same_as_a_written_null() -> None:
    schema = build_sdl_schema(OPTIONAL_FIELD_SDL)
    selection = Selection(
        'note(note: {body: "b"})',
        'note(note: {body: "b", tag: null})',
    )
    with pytest.raises(SelectionError, match="share the response key 'note'"):
        run(selection, "Query", schema=schema)


def test_two_nulls_at_a_nullable_argument_merge() -> None:
    schema = build_sdl_schema(OPTIONAL_FIELD_SDL)
    selection = Selection("free(text: null)", "free(text: null)")
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("free(") == 1


def test_a_non_object_literal_at_an_input_object_is_compared_as_written() -> None:
    schema = build_sdl_schema(OPTIONAL_FIELD_SDL)
    with pytest.raises(SelectionError, match="share the response key 'note'"):
        run(
            Selection('note(note: {body: "b"})', "note(note: 1)"),
            "Query",
            schema=schema,
        )


def test_a_literal_the_scalar_cannot_coerce_is_compared_as_written() -> None:
    with pytest.raises(SelectionError, match="share the response key 'posts'"):
        run(Selection('posts(first: "x") { id }', "posts(first: 1) { id }"))


def test_a_python_non_mapping_at_an_input_object_compares_by_value() -> None:
    schema = build_sdl_schema(OPTIONAL_FIELD_SDL)
    with pytest.raises(SelectionError, match="share the response key 'note'"):
        run(
            Selection(
                Field("note", args={"note": 5}),
                Field("note", args={"note": 6}),
            ),
            "Query",
            schema=schema,
        )


def test_a_python_bare_value_at_a_list_of_input_objects_is_checked() -> None:
    schema = build_sdl_schema(
        """
        input Tag { name: String }
        type Query { tag(tags: [Tag!]): Int }
        """
    )
    with pytest.raises(SelectionError, match="share the response key 'tag'"):
        run(
            Selection(
                Field("tag", args={"tags": {"name": "a"}}),
                Field("tag", args={"tags": {"name": "a", "nope": 1}}),
            ),
            "Query",
            schema=schema,
        )


# CR-20260912T001159Z-5c65bfb-404c14d0-F01: a stored default is read under
# ``out_name``, and is left uncanonical when nothing can reverse it.


def input_arg_schema(outer: GraphQLInputObjectType) -> GraphQLSchema:
    """A one-field ``Query`` whose only argument takes the given input object."""
    query = GraphQLObjectType(
        "Query",
        {"f": GraphQLField(GraphQLString, args={"input": GraphQLArgument(outer)})},
    )
    return GraphQLSchema(query=query)


def nested_default_schema(default: Any, **extra: Any) -> GraphQLSchema:
    """A schema whose outer input field defaults to a renamed inner object.

    graphql-core stores a default already coerced, so the inner object is
    keyed by ``out_name`` rather than by the schema field name. The default
    here is passed in so a test can also store a shape nothing can reverse.
    """
    inner = GraphQLInputObjectType(
        "Inner",
        {"camelName": GraphQLInputField(GraphQLString, out_name="snake_name")},
        **extra,
    )
    return input_arg_schema(
        GraphQLInputObjectType(
            "Outer", {"inner": GraphQLInputField(inner, default_value=default)}
        )
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_an_omitted_out_name_default_is_not_an_explicitly_empty_object(
    python_first: bool,
) -> None:
    """Omitting the field asks for the default. Writing ``{}`` asks for nothing."""
    schema = nested_default_schema({"snake_name": "default"})
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_an_out_name_default_still_merges_with_the_value_it_stands_for() -> None:
    """The default renders under the schema field name, so both forms agree."""
    schema = nested_default_schema({"snake_name": "default"})
    selection = Selection(
        Field("f", args={"input": {}}),
        'f(input: {inner: {camelName: "default"}})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("f(") == 1


def test_two_omitted_out_name_defaults_still_merge() -> None:
    schema = nested_default_schema({"snake_name": "default"})
    selection = Selection(Field("f", args={"input": {}}), "f(input: {})")
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("f(") == 1


def test_a_default_behind_a_custom_out_type_has_no_canonical_form() -> None:
    """Nothing can reverse an out_type, so the default is never merged away."""
    schema = nested_default_schema(
        ("snake_name",), out_type=lambda mapping: tuple(sorted(mapping))
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), "f(input: {inner: {}})"),
            "Query",
            schema=schema,
        )


def test_a_stored_default_no_field_claims_has_no_canonical_form() -> None:
    schema = nested_default_schema({"nope": "default"})
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), "f(input: {inner: {}})"),
            "Query",
            schema=schema,
        )


def test_a_non_mapping_default_at_an_input_object_has_no_canonical_form() -> None:
    schema = nested_default_schema(5)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), "f(input: {inner: {}})"),
            "Query",
            schema=schema,
        )


def test_a_list_default_is_written_out_item_by_item() -> None:
    schema = build_sdl_schema(
        """
        input Tag { name: String }
        input Post { tags: [Tag] = [{name: "a"}] }
        type Query { post(post: Post): Int }
        """
    )
    selection = Selection(
        Field("post", args={"post": {}}),
        'post(post: {tags: [{name: "a"}]})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("post(") == 1


def test_an_sdl_bare_default_at_a_list_is_already_coerced_and_merges() -> None:
    """SDL coerces the shorthand at schema-build time, so the stored value is
    already list-shaped by the time this module sees it. The genuinely bare,
    uncoerced case is covered by
    ``test_a_bare_stored_default_at_a_list_has_no_canonical_form``, which
    builds the field programmatically to bypass that coercion.
    """
    schema = build_sdl_schema(
        """
        input Post { tags: [String] = "a" }
        type Query { post(post: Post): Int }
        """
    )
    selection = Selection(
        Field("post", args={"post": {}}),
        'post(post: {tags: ["a"]})',
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("post(") == 1


def test_a_null_default_is_written_as_null() -> None:
    schema = build_sdl_schema(
        """
        input Post { tag: String = null }
        type Query { post(post: Post): Int }
        """
    )
    selection = Selection(
        Field("post", args={"post": {}}),
        "post(post: {tag: null})",
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("post(") == 1


@pytest.mark.parametrize("stored", [{"id": None}, {}])
def test_a_default_the_declared_type_rejects_has_no_canonical_form(
    stored: dict[str, Any],
) -> None:
    """A null at a required field, and that field left out, are both invalid."""
    inner = GraphQLInputObjectType(
        "Inner", {"id": GraphQLInputField(GraphQLNonNull(GraphQLInt))}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer", {"inner": GraphQLInputField(inner, default_value=stored)}
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), "f(input: {inner: {id: 1}})"),
            "Query",
            schema=schema,
        )


@pytest.mark.parametrize("python_first", [True, False])
def test_an_omitted_field_inside_a_stored_default_has_no_canonical_form(
    python_first: bool,
) -> None:
    """A stored default is delivered exactly as stored: nothing fills in a
    nested field its own default would have supplied. That filling belongs
    only to a literal that omits the field, since coercion is what runs for a
    literal and never runs for an already-stored default.
    """
    deep = GraphQLInputObjectType("Deep", {"x": GraphQLInputField(GraphQLInt)})
    inner = GraphQLInputObjectType(
        "Inner",
        {
            "camelName": GraphQLInputField(GraphQLString, out_name="snake_name"),
            "deep": GraphQLInputField(deep, default_value={"x": 1}),
        },
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"snake_name": "d"})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {camelName: "d", deep: {x: 1}}})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_tuple_default_does_not_merge_with_an_explicit_list(
    python_first: bool,
) -> None:
    """Execution delivers the stored tuple as-is; a list literal coerces to a
    ``list``, never a ``tuple``, so the two are not the same resolver-visible
    value even though a tuple is iterable like a list.
    """
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "tags": GraphQLInputField(
                    GraphQLList(GraphQLString), default_value=("a",)
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {tags: ["a"]})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_set_default_does_not_merge_with_an_explicit_list(
    python_first: bool,
) -> None:
    """A set is iterable, exactly like a list, but coercion never produces one."""
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "tags": GraphQLInputField(
                    GraphQLList(GraphQLString), default_value={"a"}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {tags: ["a"]})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


# CR-20260914T235513Z-5c65bfb-4b70a125-F01: whole-value ``==`` still treats an
# ``Int`` and a ``Float`` at the same list position or mapping key as equal.


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_int_default_does_not_merge_with_an_explicit_float_in_a_list(
    python_first: bool,
) -> None:
    """``1 == 1.0`` in Python, and that equality survives inside a list, so a
    stored ``[1]`` default and an explicit ``[1.0]`` literal round-trip and
    compare equal by ``==`` even though a resolver receives an ``int`` for one
    and a ``float`` for the other.
    """
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"nums": GraphQLInputField(GraphQLList(GraphQLFloat), default_value=[1])},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {nums: [1.0]})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_int_default_does_not_merge_with_an_explicit_float_in_an_object(
    python_first: bool,
) -> None:
    """The same false merge, one level deeper: a nested input-object field's
    stored ``int`` and an explicit ``float`` at the same key.
    """
    inner = GraphQLInputObjectType("Inner", {"n": GraphQLInputField(GraphQLFloat)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"n": 1})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_float_default_merges_with_an_equal_explicit_float_in_a_list(
    python_first: bool,
) -> None:
    """A genuinely equivalent control: same type, same value, still merges."""
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"nums": GraphQLInputField(GraphQLList(GraphQLFloat), default_value=[1.0])},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {nums: [1.0]})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    printed = print_ast(run(Selection(*order), "Query", schema=schema).selection_set)
    assert printed.count("f(") == 1


def test_a_stored_field_default_reaches_a_resolver_as_int_not_float() -> None:
    """Grounds the type distinction in an actual resolver call, for the same
    case ``_canonical_default`` handles: the object argument is present in
    the request and only one of its fields is left out. Execution really
    does hand a resolver the field's stored ``int`` default unchanged, and a
    ``float`` for an explicit literal at the same field, whether that field
    sits directly on the object or inside a further-nested one. A normalizer
    that merged the two selections would silently change which value a
    shared response key's resolver receives.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    inner = GraphQLInputObjectType("Inner", {"n": GraphQLInputField(GraphQLFloat)})
    outer = GraphQLInputObjectType(
        "Outer",
        {
            "nums": GraphQLInputField(GraphQLList(GraphQLFloat), default_value=[1]),
            "inner": GraphQLInputField(inner, default_value={"n": 1}),
        },
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(
        schema, "{ f(input: {nums: [1.0], inner: {n: 1.0}}) }"
    )

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    assert received == [
        {"nums": [1], "inner": {"n": 1}},
        {"nums": [1.0], "inner": {"n": 1.0}},
    ]
    assert type(received[0]["nums"][0]) is int
    assert type(received[0]["inner"]["n"]) is int
    assert type(received[1]["nums"][0]) is float
    assert type(received[1]["inner"]["n"]) is float


# CR-20260915T001955Z-5c65bfb-4dde6b81-F01: exact mapping comparison must
# apply the same type-and-value rule to a mapping's keys as it does to its
# values, not the plain ``==`` that ``dict.keys()`` comparison falls back to.


class _StrKey(str):
    """A ``str`` subclass that hashes and compares equal to a plain ``str``
    of the same text, so it can stand in for a field name without ``dict``
    lookups noticing, while still being a different concrete type.
    """


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_key_of_a_str_subclass_does_not_merge_with_a_plain_str_key(
    python_first: bool,
) -> None:
    """A stored mapping key can be a ``str`` subclass that is `==`-equal and
    equal-hashing to the plain ``str`` GraphQL uses for the field name it
    stands in for. ``dict.keys()`` equality does not see the type
    difference; a resolver does, since it receives the mapping keyed by
    whichever concrete key object was actually stored.
    """
    inner = GraphQLInputObjectType("Inner", {"n": GraphQLInputField(GraphQLFloat)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={_StrKey("n"): 1.0})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_a_stored_default_key_of_a_plain_str_still_merges_with_the_same_key() -> None:
    """A genuinely equivalent control: an ordinary ``str`` key is the same
    concrete type and the same value on both sides, so the merge still
    succeeds.
    """
    inner = GraphQLInputObjectType("Inner", {"n": GraphQLInputField(GraphQLFloat)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"n": 1.0})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    printed = print_ast(
        run(Selection(omitted, explicit), "Query", schema=schema).selection_set
    )
    assert printed.count("f(") == 1


def test_a_stored_default_key_of_a_str_subclass_reaches_a_resolver_unchanged() -> None:
    """Grounds the key-type distinction in an actual resolver call, for the
    same case ``_canonical_default`` handles: the ``input`` argument is
    present and only the ``inner`` field is left out, so its stored default
    dict, key object and all, is delivered unmodified. An explicit literal
    at the same field builds a fresh, plain-``str``-keyed dict instead.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    inner = GraphQLInputObjectType("Inner", {"n": GraphQLInputField(GraphQLFloat)})
    outer = GraphQLInputObjectType(
        "Outer",
        {"inner": GraphQLInputField(inner, default_value={_StrKey("n"): 1.0})},
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(schema, "{ f(input: {inner: {n: 1.0}}) }")

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    assert received == [{"inner": {"n": 1.0}}, {"inner": {"n": 1.0}}]
    assert type(next(iter(received[0]["inner"]))) is _StrKey
    assert type(next(iter(received[1]["inner"]))) is str


# CR-20260915T003642Z-5c65bfb-6ee93a73-F01: matching a mapping key by ``==``
# or by ``dict`` lookup trusts the key's own class to decide whether two
# payloads are the same key, and a caller-controlled subclass can override
# ``__eq__``/``__hash__`` to make different payloads collide. The property
# holds for a mapping's own keys wherever this module attributes or compares
# them, not only inside ``_exact_match``: the same subclass can be used as an
# input field's ``out_name`` (read in ``_default_object_literal``) or as a
# key inside a stored default's mapping value (read in ``_exact_match``).


class _CollidingKey(str):
    """A ``str`` subclass whose equality and hash ignore its own text, so two
    instances holding different text still compare and hash equal to each
    other. Its real, distinct text is still the base ``str`` payload; only
    the override lies about it, the same way a resolver never sees the lie.
    """

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _CollidingKey)

    def __hash__(self) -> int:
        return hash(_CollidingKey)


def _colliding_key_schema() -> GraphQLSchema:
    """An ``Outer.inner`` default keyed by one ``_CollidingKey`` payload,
    naming an ``Inner`` field whose declared storage key is a different
    ``_CollidingKey`` payload. Ordinary ``dict`` membership cannot tell the
    two payloads apart, since the type's own equality says they match.
    """
    inner = GraphQLInputObjectType(
        "Inner",
        {"n": GraphQLInputField(GraphQLFloat, out_name=_CollidingKey("declared"))},
    )
    return input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={_CollidingKey("stored"): 1.0}
                )
            },
        )
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_key_that_collides_by_overridden_equality_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's key and the field's declared storage key are the
    same concrete type and hold different text, but the type's own
    ``__eq__``/``__hash__`` claim every instance is equal. Trusting that
    override, as plain ``dict`` membership and lookup do, would attribute the
    stored value to the field anyway; comparing the real text does not.
    """
    schema = _colliding_key_schema()
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_a_colliding_default_key_reaches_a_resolver_unchanged() -> None:
    """Grounds the mismatch in an actual resolver call: both requests
    execute successfully, but the omitted form delivers the stored default's
    own key text and the explicit form delivers the field's declared storage
    key text, even though the two key objects compare and hash equal.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    inner = GraphQLInputObjectType(
        "Inner",
        {"n": GraphQLInputField(GraphQLFloat, out_name=_CollidingKey("declared"))},
    )
    outer = GraphQLInputObjectType(
        "Outer",
        {
            "inner": GraphQLInputField(
                inner, default_value={_CollidingKey("stored"): 1.0}
            )
        },
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(schema, "{ f(input: {inner: {n: 1.0}}) }")

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    omitted_key = next(iter(received[0]["inner"]))
    explicit_key = next(iter(received[1]["inner"]))
    assert type(omitted_key) is _CollidingKey
    assert type(explicit_key) is _CollidingKey
    assert omitted_key == explicit_key
    assert str.__eq__(omitted_key, "stored")
    assert str.__eq__(explicit_key, "declared")


# CR-20260915T105334Z-5c65bfb-e703b70a-F01 narrows this further still: a
# ``str`` subclass is no longer given credit for matching real text either,
# even when it genuinely holds the same text as the other operand. The
# reason is the same one that applies to ``_TaggedInt``: recognizing any
# subclass at all, even one whose text happens to match here, would equally
# recognize one whose text matches while its own extra state, the thing a
# resolver can actually read, differs. Only a plain ``str`` is compared by
# content now; a subclass falls back to identity like every other subclass.


def test_a_colliding_default_key_of_the_same_subclass_no_longer_merges() -> None:
    """The stored default's key and the field's declared storage key are the
    same concrete ``_CollidingKey`` subclass and hold the same real text,
    but a subclass is no longer trusted through its base ``str`` payload at
    all, so two distinct instances no longer merge even here. This trades
    away the narrower positive result the previous cycle recorded, in
    favor of closing the gap ``_TaggedInt`` demonstrates for every
    recognized shape uniformly.
    """
    inner = GraphQLInputObjectType(
        "Inner",
        {"n": GraphQLInputField(GraphQLFloat, out_name=_CollidingKey("n"))},
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={_CollidingKey("n"): 1.0}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


def test_a_colliding_default_key_still_merges_when_declared_as_a_plain_str() -> None:
    """Positive control: the realistic case. A schema almost never declares
    ``out_name`` as anything but a plain ``str``, and a plain ``str`` key
    still matches by real content, so the narrowing above affects only a
    deliberately adversarial subclass, not an ordinary schema.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"n": GraphQLInputField(GraphQLFloat, out_name="n")}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"n": 1.0})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {n: 1.0}})"
    printed = print_ast(
        run(Selection(omitted, explicit), "Query", schema=schema).selection_set
    )
    assert printed.count("f(") == 1


def test_a_stored_default_with_two_fields_in_reversed_order_still_merges() -> None:
    """Positive control: order-independence over more than one key. A stored
    default built with its fields in one order must still match an explicit
    literal that writes them in the opposite order.
    """
    inner = GraphQLInputObjectType(
        "Inner",
        {
            "a": GraphQLInputField(GraphQLFloat),
            "b": GraphQLInputField(GraphQLFloat),
        },
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"b": 2.0, "a": 1.0})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {a: 1.0, b: 2.0}})"
    printed = print_ast(
        run(Selection(omitted, explicit), "Query", schema=schema).selection_set
    )
    assert printed.count("f(") == 1


# CR-20260915T005931Z-5c65bfb-397d732a-F01: ``_exact_match``'s fallback for
# any value that is not a recognized shape decided sameness with ``a == b``,
# trusting the operand's own possibly-overridden equality exactly like the
# ``str``-subclass gap the previous cycle closed. A custom scalar's
# ``parse_value``/``parse_literal`` can return any type, including one whose
# ``__eq__`` always reports two different payloads equal, which lets a
# non-invertible scalar's self-consistency round trip inside
# ``_canonical_default`` wrongly pass and merge a stored default with an
# unrelated explicit literal.


class _Token:
    """An opaque custom-scalar value: not a builtin, so no base ``__eq__``
    can be trusted for it. Its equality ignores its own text, so two
    instances holding different text still compare equal to each other,
    exactly the lie a colliding ``str`` subclass told for a builtin type.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Token)

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return f"_Token({self.text!r})"


def _broken_token_type() -> GraphQLScalarType:
    """A ``Token`` scalar whose ``serialize`` ignores the value it is given
    and always names the literal ``"wire"``. Its round trip is therefore not
    invertible: printing a stored default's value can name a different
    payload than the default itself holds, which is exactly the case the
    self-consistency check in ``_canonical_default`` exists to catch.
    """
    return GraphQLScalarType(
        name="Token", serialize=lambda _value: "wire", parse_value=_Token
    )


def _token_default_schema() -> GraphQLSchema:
    """An ``Outer.inner`` default holding ``Token("stored")`` under ``tok``,
    against a field whose declared type is the non-invertible ``Token``
    scalar. Every ``dict``-shaped check the fields share is otherwise the
    same as ``_colliding_key_schema``; only the compared value's type is a
    custom scalar's own output rather than a mapping key.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"tok": GraphQLInputField(_broken_token_type())}
    )
    return input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"tok": _Token("stored")}
                )
            },
        )
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_of_a_non_invertible_custom_scalar_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's ``Token`` and the explicit literal's ``Token``
    hold different text, but the type's own ``__eq__`` claims every instance
    is equal, and the scalar's own ``serialize`` cannot be trusted to print
    the default's real payload. Trusting either, as the pre-fix fallback
    equality did, would merge these two different requests into one.
    """
    schema = _token_default_schema()
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {tok: "wire"}})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_a_non_invertible_custom_scalar_default_reaches_a_resolver_unchanged() -> None:
    """Grounds the mismatch in an actual resolver call: both requests execute
    successfully, but the omitted form delivers the stored default's own
    ``Token`` and the explicit form delivers a ``Token`` built from the
    literal, holding different real text even though the two compare equal.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    inner = GraphQLInputObjectType(
        "Inner", {"tok": GraphQLInputField(_broken_token_type())}
    )
    outer = GraphQLInputObjectType(
        "Outer",
        {"inner": GraphQLInputField(inner, default_value={"tok": _Token("stored")})},
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(schema, '{ f(input: {inner: {tok: "wire"}}) }')

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    omitted_token = received[0]["inner"]["tok"]
    explicit_token = received[1]["inner"]["tok"]
    assert omitted_token == explicit_token
    assert omitted_token.text == "stored"
    assert explicit_token.text == "wire"


class _CollidingCount(int):
    """An ``int`` subclass whose equality ignores its own value, so two
    instances holding different numbers still compare equal to each other.
    Sweeps the same fallback gap for a numeric subclass, not only ``str``.
    """

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _CollidingCount)

    def __hash__(self) -> int:
        return 0


def test_a_stored_default_of_a_non_invertible_numeric_scalar_does_not_merge() -> None:
    """The same non-invertible-scalar gap, for a numeric subclass rather than
    an arbitrary object: the fallback must not trust ``int.__eq__`` overridden
    by a caller-controlled subclass any more than it trusts an object's own.
    """
    count_type = GraphQLScalarType(
        name="Count", serialize=lambda _value: 99, parse_value=_CollidingCount
    )
    inner = GraphQLInputObjectType("Inner", {"count": GraphQLInputField(count_type)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"count": _CollidingCount(1)}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {count: 99}})"
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


def test_a_faithful_custom_scalar_tuple_default_still_merges() -> None:
    """Positive control: a well-behaved custom scalar whose Python value is a
    plain ``tuple`` of recognized-safe elements. ``_exact_match`` never calls
    the tuple's own ``__eq__``; it recurses into ``1`` and ``2`` structurally,
    so a genuinely faithful round trip still proves the default unchanged and
    the merge still succeeds, showing the fix is not merely a lockout.
    """
    point_type = GraphQLScalarType(
        name="Point",
        serialize=lambda value: f"{value[0]},{value[1]}",
        parse_value=lambda value: tuple(int(part) for part in value.split(",")),
    )
    inner = GraphQLInputObjectType("Inner", {"point": GraphQLInputField(point_type)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"point": (1, 2)})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {point: "1,2"}})'
    printed = print_ast(
        run(Selection(omitted, explicit), "Query", schema=schema).selection_set
    )
    assert printed.count("f(") == 1


# CR-20260915T105334Z-5c65bfb-e703b70a-F01: recognizing a shape by
# ``isinstance`` let a subclass through too, and a subclass defeats exact
# matching without any ``__eq__`` override at all: an immutable-builtin
# subclass can carry its own extra state that the base ``__eq__`` never
# inspects, and a container-builtin subclass can override the very
# operations (``len``, iteration, ``items``) a structural check reads
# through. Plain ``float`` also needs its own correction: ``0.0 == -0.0``
# even though the two carry a different, resolver-visible sign.


class _TaggedInt(int):
    """An ``int`` subclass carrying its own extra state that ``int.__eq__``
    never inspects. No ``__eq__`` is overridden here: the base payload
    genuinely matches for two instances holding a different ``tag``, which
    is exactly the gap an ``isinstance`` check, rather than an exact-type
    check, leaves open.
    """

    tag: str

    def __new__(cls, value: int, tag: str) -> _TaggedInt:
        self = super().__new__(cls, value)
        self.tag = tag
        return self


def _tagged_int_type() -> GraphQLScalarType:
    """A ``Tagged`` scalar whose ``serialize`` prints only the base ``int``
    payload, dropping the attached ``tag``, so a stored default and an
    unrelated literal sharing a payload but holding different tags print
    identically.
    """
    return GraphQLScalarType(
        name="Tagged",
        serialize=lambda value: int(value),
        parse_value=lambda value: _TaggedInt(int(value), "wire"),
    )


def _tagged_int_default_schema() -> GraphQLSchema:
    inner = GraphQLInputObjectType(
        "Inner", {"tag": GraphQLInputField(_tagged_int_type())}
    )
    return input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"tag": _TaggedInt(1, "stored")}
                )
            },
        )
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_of_a_same_payload_tagged_subclass_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's ``_TaggedInt`` and the literal-coerced
    ``_TaggedInt`` share the base payload ``1``, so ``int.__eq__`` calls
    them equal with no override involved, but they hold different ``tag``
    attributes ``int.__eq__`` never looks at. Recognizing the subclass as
    though it were a plain ``int``, as an ``isinstance`` check would, merges
    these two different requests into one.
    """
    schema = _tagged_int_default_schema()
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {tag: 1}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_a_same_payload_tagged_subclass_default_reaches_a_resolver_unchanged() -> None:
    """Grounds the mismatch in an actual resolver call: both requests execute
    successfully, but the omitted form delivers the stored default's own
    ``tag`` and the explicit form delivers the literal-coerced ``tag``, even
    though the two ``_TaggedInt`` values compare equal as plain ``int``.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    inner = GraphQLInputObjectType(
        "Inner", {"tag": GraphQLInputField(_tagged_int_type())}
    )
    outer = GraphQLInputObjectType(
        "Outer",
        {
            "inner": GraphQLInputField(
                inner, default_value={"tag": _TaggedInt(1, "stored")}
            )
        },
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(schema, "{ f(input: {inner: {tag: 1}}) }")

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    omitted_tag = received[0]["inner"]["tag"]
    explicit_tag = received[1]["inner"]["tag"]
    assert omitted_tag == explicit_tag
    assert omitted_tag.tag == "stored"
    assert explicit_tag.tag == "wire"


class _TaggedStr(str):
    """A ``str`` subclass carrying its own extra state that ``str.__eq__``
    never inspects, the same gap ``_TaggedInt`` demonstrates for ``int``.
    Sweeps the same shape for ``str``, closing it for every recognized
    scalar shape uniformly rather than leaving ``str`` specially trusted.
    """

    tag: str

    def __new__(cls, value: str, tag: str) -> _TaggedStr:
        self = super().__new__(cls, value)
        self.tag = tag
        return self


def _tagged_str_type() -> GraphQLScalarType:
    """A ``TaggedString`` scalar whose ``serialize`` prints only the base
    ``str`` payload, dropping the attached ``tag``, so a stored default and
    an unrelated literal sharing text but holding different tags print
    identically.
    """
    return GraphQLScalarType(
        name="TaggedString",
        serialize=lambda value: str(value),
        parse_value=lambda value: _TaggedStr(str(value), "wire"),
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_of_a_same_payload_tagged_string_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's ``_TaggedStr`` and the literal-coerced
    ``_TaggedStr`` share the base text ``"n"``, so ``str.__eq__`` calls
    them equal with no override involved, but they hold different ``tag``
    attributes ``str.__eq__`` never looks at.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"tag": GraphQLInputField(_tagged_str_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"tag": _TaggedStr("n", "stored")}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {tag: "n"}})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


def test_a_stored_default_of_a_sign_collapsing_scalar_does_not_merge() -> None:
    """A ``SignedZero`` scalar whose ``serialize`` always names the literal
    ``"0.0"`` regardless of the true sign of the float it is given, the same
    non-invertible construction as ``_broken_token_type``. ``float.__eq__``
    alone calls ``-0.0`` and ``0.0`` equal, so without also comparing the
    sign of zero this stored default's round trip would wrongly validate
    against a stored payload whose real sign the printed text never named.
    """
    scalar = GraphQLScalarType(
        name="SignedZero", serialize=lambda _value: "0.0", parse_value=float
    )
    inner = GraphQLInputObjectType("Inner", {"z": GraphQLInputField(scalar)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"inner": GraphQLInputField(inner, default_value={"z": -0.0})},
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {z: "0.0"}})'
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


def test_a_sign_collapsing_scalar_default_reaches_a_resolver_unchanged() -> None:
    """Grounds the signed-zero mismatch in an actual resolver call: the
    omitted form delivers the stored default's real ``-0.0`` and the
    explicit form delivers the literal's real ``0.0``, distinguishable only
    by the sign ``math.copysign`` exposes.
    """
    received: list[dict[str, Any]] = []

    def resolve_f(_root: None, _info: GraphQLResolveInfo, **kwargs: Any) -> str:
        received.append(kwargs["input"])
        return "ok"

    scalar = GraphQLScalarType(
        name="SignedZero", serialize=lambda _value: "0.0", parse_value=float
    )
    inner = GraphQLInputObjectType("Inner", {"z": GraphQLInputField(scalar)})
    outer = GraphQLInputObjectType(
        "Outer", {"inner": GraphQLInputField(inner, default_value={"z": -0.0})}
    )
    query = GraphQLObjectType(
        "Query",
        {
            "f": GraphQLField(
                GraphQLString,
                args={"input": GraphQLArgument(outer)},
                resolve=resolve_f,
            )
        },
    )
    schema = GraphQLSchema(query=query)

    omitted_result = graphql_sync(schema, "{ f(input: {}) }")
    explicit_result = graphql_sync(schema, '{ f(input: {inner: {z: "0.0"}}) }')

    assert omitted_result.errors is None
    assert explicit_result.errors is None
    omitted_z = received[0]["inner"]["z"]
    explicit_z = received[1]["inner"]["z"]
    assert omitted_z == explicit_z
    assert math.copysign(1.0, omitted_z) == -1.0
    assert math.copysign(1.0, explicit_z) == 1.0


class _LyingTuple(tuple):  # type: ignore[type-arg]
    """A ``tuple`` subclass whose iteration and length always report a
    fixed, fabricated view, no matter what real content its own tuple
    storage holds. A check that reads structure through ``len``/iteration
    rather than through ``tuple``'s own base implementation sees this
    fabricated view agree for any two instances, regardless of their real,
    resolver-visible content.
    """

    def __iter__(self) -> Any:
        return iter((0, 0))

    def __len__(self) -> int:
        return 2


def _lying_pair_type() -> GraphQLScalarType:
    """A ``Pair`` scalar whose ``serialize`` always names the literal
    ``"0,0"`` regardless of the value given it, the same non-invertible
    construction as ``_broken_token_type``, so a stored default's real
    content is never actually printed.
    """
    return GraphQLScalarType(
        name="Pair",
        serialize=lambda _value: "0,0",
        parse_value=lambda value: _LyingTuple(int(part) for part in value.split(",")),
    )


def test_a_stored_default_of_a_hostile_tuple_subclass_does_not_merge() -> None:
    """The stored default's real tuple content is ``(9, 9)`` and the
    explicit literal's real content is ``(0, 0)``, but ``_LyingTuple``'s own
    iteration reports the same fabricated view for both regardless of real
    content, and the scalar's own ``serialize`` cannot be trusted to print
    the default's real payload. Trusting either, as a structural check that
    iterates the operand itself would, merges these two different requests
    into one.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"pair": GraphQLInputField(_lying_pair_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"pair": _LyingTuple((9, 9))}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {pair: "0,0"}})'
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


class _LyingMapping(dict):  # type: ignore[type-arg]
    """A ``dict`` subclass whose ``items`` and length always report a fixed,
    fabricated view, no matter what real key/value pairs its own storage
    holds. Sweeps the same fabricated-view gap for a mapping subclass, not
    only a tuple.
    """

    def items(self) -> Any:
        return {"secret": 0}.items()

    def __len__(self) -> int:
        return 1


def _lying_secret_type() -> GraphQLScalarType:
    """A ``Secret`` scalar whose ``serialize`` always names the literal
    ``"0"`` regardless of the value given it, so a stored default's real
    content is never actually printed.
    """
    return GraphQLScalarType(
        name="Secret",
        serialize=lambda _value: "0",
        parse_value=lambda value: _LyingMapping({"secret": int(value)}),
    )


def test_a_stored_default_of_a_hostile_mapping_subclass_does_not_merge() -> None:
    """The stored default's real content is ``{"secret": 9}`` and the
    explicit literal's real content is ``{"secret": 0}``, but
    ``_LyingMapping``'s own ``items`` reports the same fabricated view for
    both regardless of real content. Trusting it, as a structural check
    that reads ``items`` on the operand itself would, merges these two
    different requests into one.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"box": GraphQLInputField(_lying_secret_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"box": _LyingMapping({"secret": 9})}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {box: "0"}})'
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


# CR-20260915T123407Z-5c65bfb-ccc2c2ff-F01: the fix above gave each
# recognized shape its own independent ``type(a) is <builtin>`` gate, but
# the regression suite only exercised a subset of those gates directly
# (``str``, ``int``, the signed-zero ``float`` case, ``tuple``, ``dict``).
# ``float``'s ordinary tagged-subclass case, ``bytes``, ``list``, and a
# non-``dict`` ``Mapping`` had no dedicated test, so a change that regressed
# only one of those omitted gates back to ``isinstance`` would pass every
# test in this file. ``bool`` needs no such test: Python does not allow
# subclassing it, the same guarantee ``None`` already has.


class _TaggedFloat(float):
    """A ``float`` subclass carrying its own extra state that
    ``float.__eq__`` never inspects, the same gap ``_TaggedInt`` demonstrates
    for ``int``. This is a different gap from the signed-zero correction:
    it exists even for two ordinary, same-sign floats.
    """

    tag: str

    def __new__(cls, value: float, tag: str) -> _TaggedFloat:
        self = super().__new__(cls, value)
        self.tag = tag
        return self


def _tagged_float_type() -> GraphQLScalarType:
    """A ``TaggedFloat`` scalar whose ``serialize`` prints only the base
    ``float`` payload, dropping the attached ``tag``, so a stored default and
    an unrelated literal sharing a payload but holding different tags print
    identically.
    """
    return GraphQLScalarType(
        name="TaggedFloat",
        serialize=lambda value: float(value),
        parse_value=lambda value: _TaggedFloat(float(value), "wire"),
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_of_a_same_payload_tagged_float_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's ``_TaggedFloat`` and the literal-coerced
    ``_TaggedFloat`` share the base payload ``1.5``, so ``float.__eq__``
    calls them equal with no override involved, but they hold different
    ``tag`` attributes ``float.__eq__`` never looks at.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"tag": GraphQLInputField(_tagged_float_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"tag": _TaggedFloat(1.5, "stored")}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = "f(input: {inner: {tag: 1.5}})"
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


class _TaggedBytes(bytes):
    """A ``bytes`` subclass carrying its own extra state that
    ``bytes.__eq__`` never inspects, the same gap ``_TaggedInt`` demonstrates
    for ``int``. Sweeps the last unswept immutable scalar builtin.
    """

    tag: str

    def __new__(cls, value: bytes, tag: str) -> _TaggedBytes:
        self = super().__new__(cls, value)
        self.tag = tag
        return self


def _tagged_bytes_type() -> GraphQLScalarType:
    """A ``TaggedBytes`` scalar whose ``serialize`` prints only the base
    ``bytes`` payload, as hex, dropping the attached ``tag``, so a stored
    default and an unrelated literal sharing a payload but holding different
    tags print identically.
    """
    return GraphQLScalarType(
        name="TaggedBytes",
        serialize=lambda value: bytes(value).hex(),
        parse_value=lambda value: _TaggedBytes(bytes.fromhex(value), "wire"),
    )


@pytest.mark.parametrize("python_first", [True, False])
def test_a_stored_default_of_a_same_payload_tagged_bytes_does_not_merge(
    python_first: bool,
) -> None:
    """The stored default's ``_TaggedBytes`` and the literal-coerced
    ``_TaggedBytes`` share the base payload ``b"\\x01"``, so
    ``bytes.__eq__`` calls them equal with no override involved, but they
    hold different ``tag`` attributes ``bytes.__eq__`` never looks at.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"tag": GraphQLInputField(_tagged_bytes_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"tag": _TaggedBytes(b"\x01", "stored")}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {tag: "01"}})'
    order = (omitted, explicit) if python_first else (explicit, omitted)
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(*order), "Query", schema=schema)


class _LyingList(list):  # type: ignore[type-arg]
    """A ``list`` subclass whose iteration and length always report a fixed,
    fabricated view, no matter what real content its own list storage holds,
    the same gap ``_LyingTuple`` demonstrates for ``tuple``.
    """

    def __iter__(self) -> Any:
        return iter((0, 0))

    def __len__(self) -> int:
        return 2


def _lying_series_type() -> GraphQLScalarType:
    """A ``Series`` scalar whose ``serialize`` always names the literal
    ``"0,0"`` regardless of the value given it, the same non-invertible
    construction as ``_lying_pair_type``, so a stored default's real content
    is never actually printed.
    """
    return GraphQLScalarType(
        name="Series",
        serialize=lambda _value: "0,0",
        parse_value=lambda value: _LyingList(int(part) for part in value.split(",")),
    )


def test_a_stored_default_of_a_hostile_list_subclass_does_not_merge() -> None:
    """The stored default's real list content is ``[9, 9]`` and the explicit
    literal's real content is ``[0, 0]``, but ``_LyingList``'s own iteration
    reports the same fabricated view for both regardless of real content.
    Trusting it, as a structural check that iterates the operand itself
    would, merges these two different requests into one.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"series": GraphQLInputField(_lying_series_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"series": _LyingList([9, 9])}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {series: "0,0"}})'
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


class _OpaqueMapping(Mapping):  # type: ignore[type-arg]
    """A ``Mapping`` implementation that is not a ``dict``. The pre-fix code
    recognized any ``isinstance(a, Mapping)``, so this would have been read
    structurally through ``items``/``__len__``/``__iter__``. Every one of
    those raises here, so a passing test proves this shape is excluded
    outright now, not read through a real or fabricated view: the only test
    left it can pass is identity, which two independently constructed
    instances never share, whether or not their real content agrees.
    """

    def __getitem__(self, key: str) -> int:
        raise AssertionError("must not be read structurally")

    def __iter__(self) -> Any:
        raise AssertionError("must not be read structurally")

    def __len__(self) -> int:
        raise AssertionError("must not be read structurally")

    def items(self) -> Any:
        raise AssertionError("must not be read structurally")


def _opaque_box_type() -> GraphQLScalarType:
    """A ``Box`` scalar that always produces a fresh ``_OpaqueMapping``,
    ignoring its argument entirely, so the stored default and the explicit
    literal produce two instances with the same, genuinely matching real
    content and still do not merge.
    """
    return GraphQLScalarType(
        name="Box",
        serialize=lambda _value: "0",
        parse_value=lambda _value: _OpaqueMapping(),
    )


def test_a_stored_default_of_a_non_dict_mapping_does_not_merge() -> None:
    """Unlike every hostile-subclass test above, the two ``_OpaqueMapping``
    instances here have genuinely matching real content: neither lies about
    its structure, because neither is ever asked. A ``Mapping`` that is not
    a ``dict`` is identity-only now, so even faithful, non-hostile content
    does not merge, and no ``AssertionError`` from the raising methods above
    propagates, confirming this function never reads it structurally.
    """
    inner = GraphQLInputObjectType(
        "Inner", {"box": GraphQLInputField(_opaque_box_type())}
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {
                "inner": GraphQLInputField(
                    inner, default_value={"box": _OpaqueMapping()}
                )
            },
        )
    )
    omitted = Field("f", args={"input": {}})
    explicit = 'f(input: {inner: {box: "0"}})'
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(Selection(omitted, explicit), "Query", schema=schema)


def test_an_omitted_nullable_field_inside_a_stored_default_is_absent() -> None:
    schema = nested_default_schema({})
    selection = Selection(Field("f", args={"input": {}}), "f(input: {inner: {}})")
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("f(") == 1


def test_a_list_default_item_with_no_canonical_form_stops_the_walk() -> None:
    tag = GraphQLInputObjectType("Tag", {"name": GraphQLInputField(GraphQLString)})
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"tags": GraphQLInputField(GraphQLList(tag), default_value=[5])},
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(
                Field("f", args={"input": {}}),
                'f(input: {tags: [{name: "a"}]})',
            ),
            "Query",
            schema=schema,
        )


# CR-20260912T010049Z-5c65bfb-3d6727ac-F01: two declared fields cannot store
# under the same key, or a stored value could be attributed to either one.


def test_a_duplicate_out_name_default_has_no_canonical_form() -> None:
    """Two fields sharing one storage key make the default irreversible."""
    inner = GraphQLInputObjectType(
        "Inner",
        {
            "a": GraphQLInputField(GraphQLInt, out_name="x"),
            "b": GraphQLInputField(GraphQLString, out_name="x"),
        },
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer", {"inner": GraphQLInputField(inner, default_value={"x": 1})}
        )
    )
    assert validate_schema(schema) == []
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(
                Field("f", args={"input": {}}),
                'f(input: {inner: {a: 1, b: "1"}})',
            ),
            "Query",
            schema=schema,
        )


# CR-20260912T010049Z-5c65bfb-3d6727ac-F02: a stored default has a canonical
# form only when coercing it round-trips to the exact stored value.


def test_an_id_default_stored_as_an_int_does_not_merge_with_a_string_literal() -> None:
    """Execution delivers ``int`` for the default and ``str`` for the literal."""
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer", {"id": GraphQLInputField(GraphQLID, default_value=1)}
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), 'f(input: {id: "1"})'),
            "Query",
            schema=schema,
        )


def test_a_bare_stored_default_at_a_list_has_no_canonical_form() -> None:
    """A default that skipped coercion delivers the bare value, not a list."""
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"tags": GraphQLInputField(GraphQLList(GraphQLString), default_value="a")},
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), 'f(input: {tags: ["a"]})'),
            "Query",
            schema=schema,
        )


def test_an_uncoercible_stored_default_has_no_canonical_form_rather_than_raising() -> (
    None
):
    """The type cannot serialize the stored value; that is refusal, not a crash."""
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer", {"n": GraphQLInputField(GraphQLInt, default_value="bad")}
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(Field("f", args={"input": {}}), "f(input: {n: 1})"),
            "Query",
            schema=schema,
        )


# CR-20260912T001159Z-5c65bfb-404c14d0-F02: a one-of object that breaks its
# own type-level rule is invalid, so it is compared as written.


def one_of_schema() -> GraphQLSchema:
    choice = GraphQLInputObjectType(
        "Choice",
        {"a": GraphQLInputField(GraphQLInt), "b": GraphQLInputField(GraphQLInt)},
        is_one_of=True,
    )
    query = GraphQLObjectType(
        "Query",
        {"pick": GraphQLField(GraphQLString, args={"choice": GraphQLArgument(choice)})},
    )
    return GraphQLSchema(query=query)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_null_one_of_field_is_compared_as_written(python_first: bool) -> None:
    """Validation reports the null, so the written literal has to survive."""
    schema = one_of_schema()
    python = Field("pick", args={"choice": {"a": None}})
    raw = "pick(choice: {a: null})"
    order = (python, raw) if python_first else (raw, python)
    with pytest.raises(SelectionError, match="share the response key 'pick'"):
        run(Selection(*order), "Query", schema=schema)


@pytest.mark.parametrize("python_first", [True, False])
def test_a_one_of_object_with_two_fields_is_compared_as_written(
    python_first: bool,
) -> None:
    schema = one_of_schema()
    python = Field("pick", args={"choice": {"a": 1, "b": 2}})
    raw = "pick(choice: {a: 1, b: 2})"
    order = (python, raw) if python_first else (raw, python)
    with pytest.raises(SelectionError, match="share the response key 'pick'"):
        run(Selection(*order), "Query", schema=schema)


def test_an_empty_one_of_object_is_compared_as_written() -> None:
    schema = one_of_schema()
    with pytest.raises(SelectionError, match="share the response key 'pick'"):
        run(
            Selection(Field("pick", args={"choice": {}}), "pick(choice: {})"),
            "Query",
            schema=schema,
        )


def test_a_valid_one_of_object_still_merges_across_forms() -> None:
    schema = one_of_schema()
    selection = Selection(
        Field("pick", args={"choice": {"a": 1}}),
        "pick(choice: {a: 1})",
    )
    printed = print_ast(run(selection, "Query", schema=schema).selection_set)
    assert printed.count("pick(") == 1


def test_two_different_valid_one_of_objects_still_conflict() -> None:
    schema = one_of_schema()
    with pytest.raises(SelectionError, match="share the response key 'pick'"):
        run(
            Selection("pick(choice: {a: 1})", "pick(choice: {b: 1})"),
            "Query",
            schema=schema,
        )


def test_a_one_of_default_that_breaks_its_own_rule_has_no_canonical_form() -> None:
    choice = GraphQLInputObjectType(
        "Choice",
        {"a": GraphQLInputField(GraphQLInt), "b": GraphQLInputField(GraphQLInt)},
        is_one_of=True,
    )
    schema = input_arg_schema(
        GraphQLInputObjectType(
            "Outer",
            {"choice": GraphQLInputField(choice, default_value={"a": 1, "b": 2})},
        )
    )
    with pytest.raises(SelectionError, match="share the response key 'f'"):
        run(
            Selection(
                Field("f", args={"input": {}}),
                "f(input: {choice: {a: 1, b: 2}})",
            ),
            "Query",
            schema=schema,
        )
