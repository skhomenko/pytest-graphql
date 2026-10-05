"""``contains`` and ``unordered`` (DESIGN_DECISIONS section 5, "Matching").

Both solve the same problem: pair each expected item with a distinct element
so that every pair matches. That is maximum bipartite matching, which is exact
and polynomial. Greedy first-fit is not exact: an item that fits several
elements could take the one element another item needs. ``contains`` asks that
every item is paired and allows extra elements. ``unordered`` asks the same
and also requires equal lengths, so every element is paired too.

Matching is over distinct elements, never over distinct values, so duplicates
on either side need duplicates on the other.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pytest_graphql._core.errors import GraphQLTestError
from pytest_graphql._core.matching.bipartite import maximum_matching
from pytest_graphql._core.matching.core import (
    DESCRIBE_DEPTH,
    SILENT,
    Detail,
    Matcher,
    Show,
    Walk,
    check_value,
    describe_expected,
    plural,
)

#: Pairs of an item and an element that one match may compare. The pair table
#: is built before matching, so this bounds both its time and its memory.
MAX_MATCH_PAIRS = 1_000_000
#: Unmatched items or elements one failure spells out before it says "more".
DETAIL_CAP = 10


class _Pairing(Matcher):
    __slots__ = ("_items",)

    name: ClassVar[str]
    equal_length: ClassVar[bool]

    def __init__(self, items: tuple[Any, ...]) -> None:
        self._items = items

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return f"{self.name}({plural(len(self._items), 'item')})"

    def _check(self, actual: Any, walk: Walk) -> bool:
        if not isinstance(actual, (list, tuple)):
            return walk.leaf(False, actual, self)
        items, count = self._items, len(actual)
        # The cap comes first: every later exit depends on sizes, and a call
        # that is over the limit must raise whether or not it could also fail
        # fast.
        pairs = len(items) * count
        if pairs > MAX_MATCH_PAIRS:
            raise GraphQLTestError(
                f"{self.name}() would compare {pairs} pairs of items and "
                f"elements, more than the limit of {MAX_MATCH_PAIRS}.\n"
                "  Narrow the list first, for example with NodeList.where()."
            )
        if self.equal_length and len(items) != count:
            return walk.leaf(False, actual, self, (_length_detail(len(items), count),))
        if len(items) > count and not walk.record:
            return False
        rows = _pair_table(items, actual)
        if not walk.record and 0 in rows:
            return False
        held = maximum_matching(rows, count)
        if all(right != -1 for right in held):
            return walk.leaf(True, actual, self)
        if not walk.record:
            return False
        return walk.leaf(False, actual, self, self._detail(items, actual, held))

    def _detail(
        self, items: tuple[Any, ...], actual: Any, held: list[int]
    ) -> tuple[Detail, ...]:
        lines: list[Detail] = []
        unmatched = [index for index, right in enumerate(held) if right == -1]
        for index in unmatched[:DETAIL_CAP]:
            lines.append(_item_detail(index, items[index]))
        if len(unmatched) > DETAIL_CAP:
            lines.append(_more_detail(len(unmatched) - DETAIL_CAP))
        if self.equal_length:
            taken = {right for right in held if right != -1}
            spare = [j for j in range(len(actual)) if j not in taken]
            for index in spare[:DETAIL_CAP]:
                lines.append(_element_detail(index, actual[index]))
            if len(spare) > DETAIL_CAP:
                lines.append(_more_detail(len(spare) - DETAIL_CAP))
        return tuple(lines)


class Contains(_Pairing):
    """Every item pairs with a distinct element. Extra elements are allowed."""

    __slots__ = ()
    name = "contains"
    equal_length = False


class Unordered(_Pairing):
    """Items and elements pair one to one, in any order."""

    __slots__ = ()
    name = "unordered"
    equal_length = True


def _pair_table(items: tuple[Any, ...], actual: Any) -> list[int]:
    """One bitmask per item: which elements it matches."""
    rows: list[int] = []
    for item in items:
        mask = 0
        for position, element in enumerate(actual):
            if check_value(item, element, SILENT):
                mask |= 1 << position
        rows.append(mask)
    return rows


def _length_detail(expected: int, got: int) -> Detail:
    return lambda show: f"expected {plural(expected, 'element')}, got {got}"


def _item_detail(index: int, item: Any) -> Detail:
    return lambda show: (
        f"no element left for item {index}: {describe_expected(item, show)}"
    )


def _element_detail(index: int, element: Any) -> Detail:
    return lambda show: f"no item left for element {index}: {show(element)}"


def _more_detail(count: int) -> Detail:
    return lambda show: f"... {count} more"


def contains(*items: Any) -> Contains:
    """Match a list that holds an element for each item, in any order.

    Each item is matched to a different element, and the list may have other
    elements too. An item can be a plain value, a `dict`, another matcher or a
    helper. The matching is exact. An item that fits several elements never
    takes the one element that another item needs. Duplicate items need
    duplicate elements.

    A very large comparison, more than a million pairs of an item and an
    element, raises an error that suggests `NodeList.where()` to narrow the
    list first.

    Args:
        *items: What the list must hold.

    Returns:
        A matcher for a list.

    Examples:
        ```python {.exec}
        from pytest_graphql import contains

        users = gql.query("users")
        assert users == contains(
            gql.expect.User(name="Grace Hopper"),
            {"name": "Ada Lovelace"},
        )
        assert users != contains({"name": "Ada Lovelace"}, {"name": "Ada Lovelace"})
        ```
    """
    return Contains(items)


def unordered(*items: Any) -> Unordered:
    """Match a list that has the same elements as the items, in any order.

    The list and the items must have the same length, and each item is matched
    to a different element. An item can be a plain value, a `dict`, another
    matcher or a helper. Duplicates on either side need duplicates on the other.

    Args:
        *items: What the list must hold, and nothing else.

    Returns:
        A matcher for a list.

    Examples:
        ```python {.exec}
        from pytest_graphql import unordered

        names = gql.query("users").pluck("name")
        assert names == unordered("Alan Turing", "Ada Lovelace", "Grace Hopper")
        assert names != unordered("Alan Turing", "Ada Lovelace")
        ```
    """
    return Unordered(items)
