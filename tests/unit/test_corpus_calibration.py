"""Regression coverage for the calibration corpus.

``tests/schema/corpus/measure.py`` is a script, not a test module: it prints a
human-readable report, and nothing enforced that the report kept matching what
`docs/reference/DESIGN_DECISIONS.md`'s Calibration section claims. This file
is that enforcement. Each assertion below pins one number the design document
cites, against the same synthetic SDL fixtures the document was written from.
A change to a fixture or to the selection engine that moves one of these
numbers must also update the design document, not just this test.
"""

from __future__ import annotations

from typing import cast

from graphql import (
    FieldNode,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLUnionType,
    InlineFragmentNode,
    build_schema,
)

from pytest_graphql._core.selection.builder import SelectionBuilder
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.schema.corpus.measure import (
    connection_depth_cap_confirmed,
    deepest_query_field_depth,
    deprecated_field_census,
    detect_connections,
    largest_default_policy_build,
    load_corpus,
    measure_query_fields,
    nested_connections,
    page_size_argument_survey,
    probe_type_depth,
    union_and_interface_widths,
)

CORPUS = load_corpus()


def test_corpus_has_the_two_synthetic_fixtures() -> None:
    assert set(CORPUS) == {"catalog", "feed"}


def test_catalog_union_exceeds_max_union_members() -> None:
    widths = union_and_interface_widths(CORPUS["catalog"])
    assert widths["SearchResult"] == 12
    assert widths["Node"] == 3


def test_catalog_search_union_collapses_the_two_members_past_the_cap() -> None:
    schema = CORPUS["catalog"]
    union_type = schema.type_map["SearchResult"]
    assert isinstance(union_type, GraphQLUnionType)
    built = SelectionBuilder(schema).build(union_type, SelectionPolicy())
    fragments = {
        fragment.type_condition.name.value: fragment
        for fragment in built.selection_set.selections
        if isinstance(fragment, InlineFragmentNode)
    }
    # SearchResult lists 12 members; max_union_members=10 collapses the last
    # two, Tag8 and Tag9, to __typename plus id.
    for name in ("Tag8", "Tag9"):
        field_names = [
            selection.name.value
            for selection in fragments[name].selection_set.selections
            if isinstance(selection, FieldNode)
        ]
        assert field_names == ["__typename", "id"]


def test_catalog_nested_connection_is_confirmed_by_the_depth_cap() -> None:
    schema = CORPUS["catalog"]
    survey = detect_connections(schema)
    nested = nested_connections(schema, survey)
    assert [(n.outer, n.field_name, n.inner) for n in nested] == [
        ("CategoryConnection", "products", "ProductConnection")
    ]
    confirmed = connection_depth_cap_confirmed(schema, SelectionBuilder(schema), nested)
    assert confirmed == nested


def test_catalog_page_size_survey_finds_a_declared_default() -> None:
    recognized, with_default = page_size_argument_survey(CORPUS["catalog"])
    assert recognized == 3
    assert with_default == 1


def test_catalog_required_argument_field_is_excluded_from_auto_selection() -> None:
    measurements = {m.query_field: m for m in measure_query_fields(CORPUS["catalog"])}
    product = measurements["product"]
    assert product.status == "ok"
    # priceInRegion(currency: String!) has an unsupplied required argument and
    # must not appear in the built document; asserting a field count on its
    # own would not catch a wrong field silently taking its slot.
    product_type = cast(GraphQLObjectType, CORPUS["catalog"].type_map["Product"])
    built = SelectionBuilder(CORPUS["catalog"]).build(product_type, SelectionPolicy())
    field_names = {
        selection.name.value
        for selection in built.selection_set.selections
        if isinstance(selection, FieldNode)
    }
    assert "priceInRegion" not in field_names


def test_feed_connections_use_a_non_first_page_argument() -> None:
    recognized, with_default = page_size_argument_survey(CORPUS["feed"])
    assert (recognized, with_default) == (0, 0)


def test_feed_nested_connections_stay_unconfirmed_at_both_depths() -> None:
    schema = CORPUS["feed"]
    survey = detect_connections(schema)
    nested = nested_connections(schema, survey)
    assert len(nested) == 2
    confirmed = connection_depth_cap_confirmed(schema, SelectionBuilder(schema), nested)
    assert confirmed == []


def test_feed_heuristic_mismatches_are_detected() -> None:
    survey = detect_connections(CORPUS["feed"])
    assert survey.named_but_wrong_shape == ["LegacyThreadConnection"]
    assert survey.shaped_but_unnamed == ["AttachmentGroup"]


def test_feed_root_with_only_unexpandable_connections_has_nothing_selectable() -> None:
    measurements = {m.query_field: m for m in measure_query_fields(CORPUS["feed"])}
    assert measurements["feedStats"].status == "no_selectable_fields"
    assert measurements["posts"].status == "ok"


def test_deprecated_fields_are_present_in_both_fixtures() -> None:
    catalog_deprecated, catalog_total = deprecated_field_census(CORPUS["catalog"])
    feed_deprecated, feed_total = deprecated_field_census(CORPUS["feed"])
    assert (catalog_deprecated, catalog_total) == (2, 42)
    assert (feed_deprecated, feed_total) == (1, 31)


def test_deepest_query_field_depth_matches_design_doc() -> None:
    assert deepest_query_field_depth(CORPUS["catalog"]) == 5
    assert deepest_query_field_depth(CORPUS["feed"]) == 6


def test_catalog_search_is_the_largest_default_policy_build_in_the_corpus() -> None:
    result = largest_default_policy_build(CORPUS)
    assert result is not None
    name, search = result
    assert name == "catalog"
    assert search.query_field == "search"
    assert (search.field_count, search.ast_depth) == (62, 6)


def test_probe_type_depth_walks_into_a_union_returned_by_a_query_field() -> None:
    """``deepest_query_field_depth`` must not silently skip a Query field
    whose return type is a union, the way it did before this test existed:
    the helper's isinstance check excluded ``GraphQLUnionType`` and returned
    ``None`` for a schema whose only Query field returns one. A purpose-built
    schema, rather than the two named corpus fixtures, isolates the contract
    from whatever ``catalog.graphql``/``feed.graphql`` happen to contain.
    """
    schema = build_schema(
        """
        type Leaf { id: ID! }
        type Wrapper { child: Leaf! }
        union Result = Leaf | Wrapper
        type Query { thing: Result! }
        """
    )
    result_type = schema.type_map["Result"]
    assert isinstance(result_type, GraphQLUnionType)
    # Leaf contributes depth 0 (no composite fields); Wrapper contributes
    # depth 1 via its `child: Leaf!` field. The union itself costs nothing.
    assert probe_type_depth(schema, result_type) == 1
    assert deepest_query_field_depth(schema) == 1


def test_probe_type_depth_stops_at_a_recursive_union_instead_of_the_cap() -> None:
    """``Wrapper.child: Result!`` returns the same union that is already the
    walk's root, so it is a cycle on the union itself, not a step to a new
    member. ``SelectionBuilder.run`` puts the root's own name in
    ``ancestors`` before expanding it (``src/pytest_graphql/_core/selection/
    builder.py``), so ``Wrapper.child`` hits ``"Result" in ancestors`` and
    the engine emits nothing further for that field; the deepest acyclic
    path below ``Result`` is therefore depth 0, from either member.

    An earlier fix checked only each selected member's own name against
    ``ancestors``, never the union's, so ``Wrapper.child: Result`` looked
    like a step to a fresh member (``Leaf``) reached through a different
    branch, and the walk returned a nonzero depth that did not match the
    engine's own cycle detection. Checking at every one of the caps below
    guards against the walk instead recursing until the cap.
    """
    schema = build_schema(
        """
        type Leaf { id: ID! }
        type Wrapper { child: Result! }
        union Result = Leaf | Wrapper
        type Query { thing: Result! }
        """
    )
    result_type = schema.type_map["Result"]
    assert isinstance(result_type, GraphQLUnionType)
    for cap in (2, 4, 20):
        assert probe_type_depth(schema, result_type, cap=cap) == 0


def test_probe_type_depth_does_not_let_a_repeated_union_reach_a_deeper_sibling() -> (
    None
):
    """A union reached again through one member must not let the walk keep
    going by switching to a sibling member it has not personally visited:
    once the union itself is an ancestor, the whole union is a cycle for any
    field that returns it again, the same way ``SelectionBuilder`` treats a
    repeat of the abstract type's own name, not just a repeat of whichever
    member happens to be reached first.

    ``A.again: Result`` re-enters ``Result``, which is already an ancestor
    from the root, so that field contributes nothing. ``B.tail: Tail``
    reaches a fresh type and its own ``leaf: Leaf`` field adds one more
    level, so the deepest acyclic path is ``B.tail.leaf`` at depth 2. Before
    this fix, the walk let ``A.again`` step past ``Result`` into the
    still-unvisited ``B`` member and count ``B``'s subtree again, reporting
    depth 3 instead of the 2 the engine's own selection would produce.
    """
    schema = build_schema(
        """
        type A { again: Result! }
        type B { tail: Tail! }
        type Tail { leaf: Leaf! }
        type Leaf { id: ID! }
        union Result = A | B
        type Query { thing: Result! }
        """
    )
    result_type = schema.type_map["Result"]
    assert isinstance(result_type, GraphQLUnionType)
    for cap in (4, 20):
        assert probe_type_depth(schema, result_type, cap=cap) == 2


def test_probe_type_depth_walks_an_interface_implementation_only_field() -> None:
    """A composite field declared only on an interface's implementation, not
    on the interface itself, must still be reachable: the walk has to expand
    every possible type of the interface, the way ``SelectionBuilder``
    expands ``schema.get_possible_types`` for an interface exactly as it does
    for a union, not skip straight past an interface root because the
    interface's own field list has nothing composite on it.

    Before this fix, an interface root (or an interface reached mid-walk)
    was treated as a plain object and probed via ``t.fields`` directly, so
    ``Wrapper``'s implementation-only ``child: Leaf`` field, which the
    interface itself does not declare, was never seen and the probe reported
    depth 0 for a schema whose selector build actually reaches depth 1.
    """
    schema = build_schema(
        """
        interface Result { id: ID! }
        type Leaf { id: ID! }
        type Wrapper implements Result { id: ID!, child: Leaf! }
        type Query { thing: Result! }
        """
    )
    result_type = schema.type_map["Result"]
    assert isinstance(result_type, GraphQLInterfaceType)
    assert probe_type_depth(schema, result_type) == 1
