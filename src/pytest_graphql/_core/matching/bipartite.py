"""Maximum bipartite matching for ``contains`` and ``unordered``.

An item that fits several elements must never take the one element another
item needs, so first-fit is wrong and a backtracking search is exponential.
This is the augmenting-path algorithm (Kuhn), which is exact and runs in
O(items x edges). Each item's candidate elements are one integer bitmask, so a
row costs one machine word per 64 elements and "which candidates are still
free" is a single mask operation.

The search is an explicit stack. A response list can hold thousands of
elements, and a recursive search would reach the interpreter's frame limit on
a long chain of overlapping items.
"""

from __future__ import annotations

from collections.abc import Sequence


def maximum_matching(rows: Sequence[int], right_count: int) -> list[int]:
    """Match each left vertex to at most one right vertex, as many as possible.

    ``rows[i]`` is a bitmask: bit ``j`` is set when left vertex ``i`` may take
    right vertex ``j``. The result gives, for each left vertex, the right
    vertex it holds, or ``-1`` when it holds none. No right vertex is used
    twice, and no larger matching exists.
    """
    match_left = [-1] * len(rows)
    match_right = [-1] * right_count
    for start in range(len(rows)):
        _augment(start, rows, match_left, match_right)
    return match_left


def _augment(
    start: int,
    rows: Sequence[int],
    match_left: list[int],
    match_right: list[int],
) -> bool:
    """Find a path that frees a right vertex for ``start``; flip it if found."""
    visited = 0
    # Each frame is a left vertex and the candidates it has not tried yet.
    # ``chosen[k]`` is the right vertex the k-th frame is currently trying.
    stack: list[tuple[int, int]] = [(start, rows[start])]
    chosen: list[int] = []
    while stack:
        left, candidates = stack[-1]
        candidates &= ~visited
        if not candidates:
            stack.pop()
            if chosen:
                chosen.pop()
            continue
        lowest = candidates & -candidates
        right = lowest.bit_length() - 1
        visited |= lowest
        stack[-1] = (left, candidates & ~lowest)
        chosen.append(right)
        holder = match_right[right]
        if holder == -1:
            for (frame_left, _), frame_right in zip(stack, chosen, strict=True):
                match_left[frame_left] = frame_right
                match_right[frame_right] = frame_left
            return True
        stack.append((holder, rows[holder]))
    return False
