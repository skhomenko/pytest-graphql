"""Maximum bipartite matching (DESIGN_DECISIONS section 5, "Matching").

``contains`` and ``unordered`` are only correct if an item that fits several
elements never takes the one element another item needs. Greedy first-fit
fails that, so the property test compares the real algorithm with a brute
force search on small graphs, and fixed cases pin the shapes greedy gets wrong.
"""

from __future__ import annotations

import itertools
import time

from hypothesis import given, settings
from hypothesis import strategies as st

from pytest_graphql._core.matching.bipartite import maximum_matching


def rows_of(adjacency: list[list[int]]) -> list[int]:
    return [sum(1 << right for right in row) for row in adjacency]


def size_of(match: list[int]) -> int:
    return sum(1 for right in match if right != -1)


def brute_force_size(adjacency: list[list[int]]) -> int:
    """The largest matching, found by trying every assignment."""
    best = 0
    options = [[*row, -1] for row in adjacency]
    for choice in itertools.product(*options):
        used = [right for right in choice if right != -1]
        if len(used) == len(set(used)):
            best = max(best, len(used))
    return best


def test_greedy_first_fit_would_fail_here() -> None:
    # Item 0 fits elements 0 and 1, item 1 fits only element 0. First-fit
    # gives item 0 element 0 and strands item 1.
    match = maximum_matching(rows_of([[0, 1], [0]]), 2)
    assert size_of(match) == 2
    assert match == [1, 0]


def test_a_longer_augmenting_path_is_found() -> None:
    adjacency = [[0, 1], [1, 2], [2, 3], [0]]
    match = maximum_matching(rows_of(adjacency), 4)
    assert size_of(match) == 4
    assert sorted(match) == [0, 1, 2, 3]


def test_an_item_with_no_candidate_stays_unmatched() -> None:
    match = maximum_matching(rows_of([[0], [], [0]]), 1)
    assert size_of(match) == 1
    assert match[1] == -1


def test_empty_inputs() -> None:
    assert maximum_matching([], 0) == []
    assert maximum_matching([], 5) == []
    assert maximum_matching([0, 0], 0) == [-1, -1]


def test_duplicate_rows_use_distinct_elements() -> None:
    # Three identical items, three identical elements: all three match, and
    # no element is used twice.
    match = maximum_matching(rows_of([[0, 1, 2]] * 3), 3)
    assert sorted(match) == [0, 1, 2]


def test_more_items_than_elements_matches_at_most_the_elements() -> None:
    match = maximum_matching(rows_of([[0]] * 4), 1)
    assert size_of(match) == 1


def test_a_large_dense_graph_is_polynomial_and_does_not_recurse() -> None:
    # 400 items against 400 elements, every pair compatible except a shifted
    # diagonal. A backtracking search would not finish. A recursive DFS would
    # exceed the interpreter's frame limit on the long chain below.
    size = 400
    dense = [[j for j in range(size) if j != i] for i in range(size)]
    started = time.monotonic()
    assert size_of(maximum_matching(rows_of(dense), size)) == size
    assert time.monotonic() - started < 5.0

    chain = [[i, i + 1] for i in range(1999)] + [[0]]
    assert size_of(maximum_matching(rows_of(chain), 2000)) == 2000


@settings(max_examples=300, deadline=None)
@given(
    st.integers(min_value=0, max_value=5).flatmap(
        lambda right_count: st.tuples(
            st.just(right_count),
            st.lists(
                st.lists(
                    st.integers(min_value=0, max_value=max(right_count - 1, 0)),
                    max_size=right_count,
                    unique=True,
                )
                if right_count
                else st.just([]),
                max_size=5,
            ),
        )
    )
)
def test_matching_is_maximum_and_valid_against_brute_force(
    case: tuple[int, list[list[int]]],
) -> None:
    right_count, adjacency = case
    match = maximum_matching(rows_of(adjacency), right_count)

    assert len(match) == len(adjacency)
    used = [right for right in match if right != -1]
    assert len(used) == len(set(used)), "an element was used twice"
    for left, right in enumerate(match):
        if right != -1:
            assert right in adjacency[left], "matched along a missing edge"
    assert len(used) == brute_force_size(adjacency)
