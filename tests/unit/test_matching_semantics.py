"""Matching semantics (SPEC 3.6, DESIGN_DECISIONS section 5, "Matching").

Every test builds its response through ``build_response``, so the actual
values are real ``Node`` and ``NodeList`` objects and the reflected
``Node == Matcher`` path is the one a user's ``assert`` takes.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

import pytest

from pytest_graphql._core.errors import GraphQLTestError
from pytest_graphql._core.matching import (
    ExpectNamespace,
    Matcher,
    absent,
    any_length,
    any_value,
    contains,
    gt,
    gte,
    length,
    lt,
    lte,
    matches,
    one_of,
    sequences,
    unordered,
)
from pytest_graphql._core.response import Node, NodeList
from tests.unit.matching_support import (
    SCHEMA,
    SECRET,
    order,
    orders_nodes,
    respond,
    user,
    user_node,
    users_nodes,
)

expect = ExpectNamespace(SCHEMA)


def lone_user_with(fields: str, data: dict[str, Any]) -> Node:
    return respond("{ user { " + fields + " } }", {"user": data}).data.user


# -- objects -----------------------------------------------------------------


def test_an_object_matcher_is_partial_and_ignores_other_fields() -> None:
    node = user_node()
    matcher = expect.User(first_name="John")
    assert node == matcher
    assert matcher == node
    assert (node != matcher) is False


def test_a_differing_field_fails() -> None:
    assert user_node() != expect.User(first_name="Jhon")


def test_every_named_field_must_match() -> None:
    assert user_node() != expect.User(first_name="John", last_name="Smith")


def test_a_matcher_field_missing_from_the_response_fails() -> None:
    node = lone_user_with("id", {"id": "u1"})
    assert node != expect.User(first_name="John")
    assert node == expect.User(id="u1")


def test_absent_passes_when_the_field_is_not_in_the_response() -> None:
    node = lone_user_with("id", {"id": "u1"})
    assert node == expect.User(first_name=absent())


def test_absent_fails_when_the_field_is_in_the_response() -> None:
    assert user_node() != expect.User(first_name=absent())
    assert user_node() != expect.User(email=absent())  # present as null


def test_absent_takes_no_argument() -> None:
    with pytest.raises(TypeError):
        absent("first_name")  # type: ignore[call-arg]


def test_absent_outside_a_field_fails_for_any_value() -> None:
    assert absent() != "x"
    assert absent() != None  # noqa: E711 -- a present null is still present


def test_snake_and_exact_field_names_both_match() -> None:
    node = user_node()
    assert node == expect.User(first_name="John")
    assert node == expect.User(firstName="John")


def test_null_is_a_value_that_can_be_matched() -> None:
    assert user_node(email=None) == expect.User(email=None)
    assert user_node() != expect.User(email=None)


def test_the_type_name_is_checked_when_the_response_carries_one() -> None:
    node = user_node()
    assert node == expect.User(id="u1")
    assert node != expect.Order(id="u1")


def test_the_type_name_is_not_checked_when_the_response_has_none() -> None:
    # An abstract type with no ``__typename`` selected has no runtime name.
    hits = respond(
        "{ hits { ... on User { id } ... on Team { id } } }",
        {"hits": [{"id": "1"}]},
    ).data.hits
    assert hits[0].__typename__ is None
    assert hits[0] == expect.Order(id="1")
    assert hits[0] == expect.User(id="1")


def test_an_interface_matcher_accepts_each_implementing_type() -> None:
    assert user_node() == expect.Entity(id="u1")
    assert orders_nodes(order())[0] == expect.Entity(id="o1")
    assert orders_nodes(order())[0] != expect.Team(id="o1")


def test_a_union_matcher_accepts_its_members_only() -> None:
    hits = respond(
        "{ hits { __typename ... on User { id } ... on Team { id } } }",
        {
            "hits": [
                {"__typename": "User", "id": "1"},
                {"__typename": "Team", "id": "2"},
            ]
        },
    ).data.hits
    assert hits[0] == expect.Hit()
    assert hits[1] == expect.Hit()
    assert hits[0] == expect.Hit(__typename="User")
    assert hits[1] != expect.Hit(__typename="User")
    assert orders_nodes(order())[0] != expect.Hit()


def test_matchers_nest() -> None:
    node = user_node()
    assert node == expect.User(settings=expect.Settings(theme="dark"))
    assert node != expect.User(settings=expect.Settings(theme="light"))


def test_a_plain_dict_inside_a_matcher_is_a_partial_match_without_validation() -> None:
    node = user_node()
    assert node == expect.User(settings={"theme": "dark"})
    assert node != expect.User(settings={"theme": "light"})
    assert node != expect.User(settings={"no_such_field": 1})  # no error, no match


def test_lists_are_positional_with_equal_length() -> None:
    node = user_node(orders=[order(total=100), order(id="o2", total=200)])
    assert node == expect.User(orders=[expect.Order(total=100), {"total": 200}])
    assert node != expect.User(orders=[expect.Order(total=200), {"total": 100}])
    assert node != expect.User(orders=[expect.Order(total=100)])
    assert node != expect.User(orders=[])


def test_plain_scalar_lists_compare_by_position() -> None:
    assert user_node(tags=["a", "b"]) == expect.User(tags=["a", "b"])
    assert user_node(tags=["a", "b"]) != expect.User(tags=["b", "a"])


def test_nesting_is_unlimited_and_mixes_dicts_lists_and_matchers() -> None:
    node = user_node(orders=[order(), order(id="o2", total=7)])
    matcher = expect.User(
        settings={"theme": matches(r"^d")},
        orders=[{"total": gt(50)}, expect.Order(total=lte(7))],
        matrix=[[1], [2, 3]],
    )
    assert node == matcher


def test_a_matcher_equals_a_plain_dict_actual() -> None:
    assert expect.Settings(theme="dark") == {"theme": "dark", "locale": "en"}
    assert expect.Settings(theme="dark") != {"theme": "light"}
    assert expect.Settings(theme="dark") == {"theme": "dark"}
    assert [expect.Settings(theme="dark")] == [{"theme": "dark"}]


def test_node_still_compares_exactly_to_a_dict() -> None:
    node = user_node()
    assert node != {"firstName": "John"}
    assert node == node.to_dict()
    assert Node.__eq__(node, expect.User(id="u1")) is NotImplemented


def test_a_matcher_is_unhashable_and_does_not_show_values() -> None:
    matcher = expect.User(password=SECRET, first_name="John")
    with pytest.raises(TypeError):
        hash(matcher)
    assert SECRET not in repr(matcher)
    assert SECRET not in str(matcher)
    assert "John" not in repr(matcher)
    assert "first_name" in repr(matcher)


def test_a_boolean_never_equals_a_number() -> None:
    assert contains(1) != [True]
    assert contains(True) != [1]
    assert contains(True) == [True]
    assert one_of(1) != True  # noqa: E712
    assert one_of(True) == True  # noqa: E712


# -- value helpers -----------------------------------------------------------


def test_length_and_any_length_constrain_lists_only() -> None:
    node = user_node(orders=[order(), order(id="o2")])
    assert node == expect.User(orders=length(2))
    assert node != expect.User(orders=length(3))
    assert node == expect.User(orders=any_length())
    assert node != expect.User(first_name=any_length())  # a string is not a list
    assert length(0) == []
    assert any_length() == []


@pytest.mark.parametrize("bad", [-1, 1.5, True, "2"])
def test_length_rejects_a_bad_count(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        length(bad)


def test_any_value_needs_the_field_but_not_its_value() -> None:
    assert user_node(email=None) == expect.User(email=any_value())
    assert lone_user_with("id", {"id": "u1"}) != expect.User(email=any_value())


def test_matches_uses_a_regex_search_on_strings_only() -> None:
    assert matches(r"^u_\d+$") == "u_12"
    assert matches(r"^u_\d+$") != "xu_12"
    assert matches("o") == "John"  # search, not fullmatch
    assert matches(re.compile("j", re.IGNORECASE)) == "John"
    assert matches(r"\d") != 12
    assert matches(r"\d") != None  # noqa: E711


def test_matches_rejects_a_bad_pattern_at_construction() -> None:
    with pytest.raises(re.error):
        matches("(")
    with pytest.raises(TypeError):
        matches(5)  # type: ignore[arg-type]


def test_comparison_helpers() -> None:
    assert gt(0) == 1
    assert gt(1) != 1
    assert gte(1) == 1
    assert lt(2) == 1
    assert lt(1) != 1
    assert lte(1) == 1
    assert gt(1.5) == 2
    assert gt(0) != float("nan")


def test_comparisons_cover_dates_and_iso_strings() -> None:
    early = datetime.date(2024, 1, 1)
    assert gt(early) == datetime.date(2024, 6, 1)
    assert lt(early) != datetime.date(2024, 6, 1)
    assert gte("2024-01-01") == "2024-06-01"
    assert lt("2024-01-01") != "2024-06-01"


def test_comparison_never_raises_on_incomparable_values() -> None:
    assert gt(0) != "1"
    assert gt(0) != None  # noqa: E711
    assert gt(0) != True  # noqa: E712 -- a boolean is not a number
    assert gt(datetime.date(2024, 1, 1)) != "2024-06-01"
    assert gt(0) != [1]
    assert gt(0) != {"a": 1}


def test_one_of_tests_membership_by_equality() -> None:
    assert one_of("A", "B") == "B"
    assert one_of("A", "B") != "C"
    assert one_of([1], {"a": 1}) == [1]  # unhashable values are fine
    assert one_of(None) == None  # noqa: E711


def test_one_of_needs_a_value() -> None:
    with pytest.raises(ValueError):
        one_of()


def test_helpers_are_matchers_that_compose() -> None:
    node = user_node(orders=[order(total=150), order(id="o2", total=90)])
    matcher = expect.User(orders=contains(expect.Order(total=gt(100))))
    assert node == matcher
    assert all(
        isinstance(item, Matcher)
        for item in (gt(1), any_value(), absent(), length(1), contains(), matches("a"))
    )


# -- contains and unordered --------------------------------------------------


def test_contains_allows_extras_and_any_order() -> None:
    assert contains(3, 1) == [1, 2, 3]
    assert contains(4) != [1, 2, 3]


def test_contains_with_no_items_is_vacuous_and_unordered_needs_an_empty_list() -> None:
    assert contains() == []
    assert contains() == [1]
    assert unordered() == []
    assert unordered() != [1]


def test_contains_and_unordered_need_a_list() -> None:
    assert contains(1) != 1
    assert contains(1) != None  # noqa: E711
    assert unordered(1) != "1"
    assert contains(1) != {"a": 1}


def test_an_empty_list_contains_nothing() -> None:
    assert contains(1) != []
    assert unordered(1) != []


def test_duplicate_items_need_duplicate_elements() -> None:
    assert contains(1, 1) != [1]
    assert contains(1, 1) == [1, 1]
    assert contains(1, 1) == [1, 2, 1]
    assert unordered(1, 1) != [1, 2]


def test_duplicate_elements_are_matched_one_to_one() -> None:
    assert unordered(1, 1, 2) == [1, 2, 1]
    assert unordered(1, 2) != [1, 1, 2]  # equal multiset needs equal lengths
    assert unordered(1, 2, 2) != [1, 1, 2]


def test_an_item_that_fits_several_elements_does_not_take_the_one_another_needs() -> (
    None
):
    # gt(0) fits both elements. First-fit gives it the 1, and the literal 1
    # then has nothing left.
    assert contains(gt(0), 1) == [1, 5]
    assert contains(gt(0), 1) == [5, 1]
    assert unordered(gt(0), 1) == [1, 5]
    assert unordered(gt(0), 1) == [5, 1]


def test_a_longer_chain_of_overlaps_still_resolves() -> None:
    items = (one_of(1, 2), one_of(2, 3), one_of(3, 4), 1)
    assert unordered(*items) == [4, 3, 2, 1]
    assert contains(*items) == [9, 1, 2, 3, 4]


def test_a_real_shortage_still_fails() -> None:
    assert contains(gt(0), gt(0), gt(0)) != [1, 2, -5]
    assert unordered(one_of(1, 2), one_of(1, 2), one_of(1, 2)) != [1, 2, 3]


def test_unordered_requires_equal_lengths() -> None:
    assert unordered(1) != [1, 2]
    assert unordered(1, 2) != [1]
    assert unordered(1, 2) == [2, 1]
    # Matching every item is not enough when an element is left over.
    assert unordered(gt(0)) != [1, 2]
    assert contains(gt(0)) == [1, 2]


def test_both_helpers_work_on_a_node_list() -> None:
    nodes = orders_nodes(order(id="a", total=1), order(id="b", total=2))
    assert isinstance(nodes, NodeList)
    assert nodes == contains(expect.Order(id="b"))
    assert nodes == unordered(expect.Order(id="b"), expect.Order(id="a"))
    assert nodes != unordered(expect.Order(id="b"))
    assert nodes != contains(expect.Order(id="c"))


def test_a_plain_dict_item_is_a_partial_match() -> None:
    nodes = orders_nodes(order(id="a", total=1), order(id="b", total=2))
    assert nodes == contains({"total": 2})
    assert nodes != contains({"total": 3})


def test_the_evaluation_names_the_unmatched_items() -> None:
    result = contains(1, 9, 8).evaluate([1, 2])
    assert not result.ok
    assert result.differ == 1
    (mismatch,) = result.mismatches
    assert mismatch.path == ()
    assert len(mismatch.detail) == 2  # items 1 and 2 have no element left


def test_a_length_failure_is_reported_as_a_length_failure() -> None:
    result = unordered(1, 2).evaluate([1, 2, 3])
    (mismatch,) = result.mismatches
    assert any("2" in line and "3" in line for line in mismatch.detail_text())


def test_the_pair_cap_refuses_instead_of_growing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sequences, "MAX_MATCH_PAIRS", 9)
    assert contains(1, 2, 3) == [1, 2, 3]  # 9 pairs, at the cap
    with pytest.raises(GraphQLTestError, match="10 pairs"):
        assert contains(1, 2) == [1, 2, 3, 4, 5]  # 10 pairs


# -- where, one, count -------------------------------------------------------


def make_users() -> NodeList:
    return users_nodes(
        user(id="1", firstName="John", status="ACTIVE"),
        user(id="2", firstName="Jane", status="ACTIVE"),
        user(id="3", firstName="John", status="BANNED"),
    )


def test_where_filters_and_returns_a_node_list() -> None:
    users = make_users()
    found = users.where(first_name="John")
    assert isinstance(found, NodeList)
    assert found is not users
    assert found.ids() == ["1", "3"]


def test_where_ands_its_filters_and_accepts_matchers() -> None:
    users = make_users()
    assert users.where(first_name="John", status="BANNED").ids() == ["3"]
    assert users.where(first_name=matches("^J"), id=one_of("1", "2")).ids() == [
        "1",
        "2",
    ]
    assert users.where(settings={"theme": "dark"}).ids() == ["1", "2", "3"]


def test_where_with_no_filter_keeps_every_element() -> None:
    users = make_users()
    assert users.where().ids() == ["1", "2", "3"]


def test_where_on_no_match_is_an_empty_node_list() -> None:
    found = make_users().where(first_name="Nobody")
    assert isinstance(found, NodeList)
    assert len(found) == 0


def test_count_applies_after_filters() -> None:
    users = make_users()
    assert len(users) == 3
    assert len(users.where(first_name="John")) == 2
    assert len(users.where(first_name="John").where(status="ACTIVE")) == 1


def test_where_is_partial_by_default() -> None:
    nodes = orders_nodes(order(id="a"))
    assert len(nodes.where(id="a")) == 1


def test_strict_where_needs_exactly_the_named_fields() -> None:
    nodes = orders_nodes(order(id="a"))
    assert len(nodes.where(strict=True, id="a")) == 0  # other fields are carried
    every = {
        "__typename": "Order",
        "id": "a",
        "total": 100,
        "status": "PAID",
        "note": None,
    }
    assert len(nodes.where(strict=True, **every)) == 1
    assert len(nodes.where(strict=True, **{**every, "total": 1})) == 0


def test_strict_where_resolves_names_like_the_node_does() -> None:
    nodes = users_nodes(user())
    fields = {
        "__typename": "User",
        "id": "u1",
        "first_name": "John",
        "lastName": "Doe",
        "email": "john@example.test",
        "status": "ACTIVE",
        "password": None,
        "settings": {"theme": "dark"},
        "orders": length(1),
        "tags": ["a", "b"],
        "matrix": [[1], [2, 3]],
    }
    assert len(nodes.where(strict=True, **fields)) == 1


def test_strict_where_with_absent_does_not_count_the_absent_field() -> None:
    nodes = respond("{ orders { id } }", {"orders": [{"id": "a"}]}).data.orders
    assert len(nodes.where(strict=True, id="a", note=absent())) == 1
    assert len(nodes.where(strict=True, id="a", total=absent(), note=absent())) == 1
    assert len(nodes.where(strict=True, note=absent())) == 0  # id is carried


def test_where_with_absent_filters_by_missing_fields() -> None:
    nodes = respond(
        "{ orders { id } }", {"orders": [{"id": "a"}, {"id": "b"}]}
    ).data.orders
    assert nodes.where(note=absent()).ids() == ["a", "b"]
    assert nodes.where(id=absent()).ids() == []


def test_a_non_node_element_never_matches_a_filter() -> None:
    nested = respond(
        "{ user { matrix } }", {"user": {"matrix": [[1], [2]]}}
    ).data.user.matrix
    assert isinstance(nested, list)
    assert list(NodeList(nested).where(a=1)) == []


def test_one_returns_the_only_match() -> None:
    users = make_users()
    only = users.one(first_name="Jane")
    assert isinstance(only, Node)
    assert only.id == "2"


def test_one_without_filters_needs_a_single_element_list() -> None:
    assert orders_nodes(order(id="a")).one().id == "a"
    with pytest.raises(GraphQLTestError, match="2 of 2"):
        orders_nodes(order(id="a"), order(id="b")).one()


def test_one_names_the_count_when_nothing_matches() -> None:
    with pytest.raises(GraphQLTestError) as caught:
        make_users().one(first_name="Nobody")
    assert "0 of 3" in str(caught.value)


def test_one_names_the_count_when_several_match() -> None:
    with pytest.raises(GraphQLTestError) as caught:
        make_users().one(first_name="John")
    assert "2 of 3" in str(caught.value)


def test_one_does_not_echo_response_values() -> None:
    users = users_nodes(user(firstName=SECRET), user(firstName=SECRET))
    with pytest.raises(GraphQLTestError) as caught:
        users.one(first_name=SECRET)
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


def test_filters_honour_the_strict_keyword_only() -> None:
    # ``strict`` is the option, so a field of that name is reached through a
    # matcher on the element instead.
    assert len(make_users().where(strict=False, first_name="Jane")) == 1


def test_where_has_no_first_helper_and_no_approx() -> None:
    assert not hasattr(NodeList, "first")
    import pytest_graphql._core.matching as matching

    assert not hasattr(matching, "approx")


def test_where_and_one_skip_a_mapping_that_is_not_a_node() -> None:
    mixed = NodeList([{"id": "x"}, 7, *orders_nodes(order(id="a"))])
    assert [item.id for item in mixed.where(id="a")] == ["a"]
    assert len(mixed.where()) == 1
    assert mixed.one(id="a").id == "a"
    with pytest.raises(GraphQLTestError, match="0 of"):
        NodeList([{"id": "x"}]).one(id="x")
    assert len(NodeList([{"id": "x"}]).where(strict=True, id="x")) == 0


def test_the_pair_cap_applies_before_any_size_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sequences, "MAX_MATCH_PAIRS", 1)
    with pytest.raises(GraphQLTestError, match="2 pairs"):
        assert contains(1, 2) == [1]  # more items than elements
    with pytest.raises(GraphQLTestError, match="2 pairs"):
        assert unordered(1, 2) == [1]  # unequal lengths
    with pytest.raises(GraphQLTestError, match="2 pairs"):
        contains(1, 2).evaluate([1])
    assert contains(1) == [1]  # one pair, at the cap
