"""Matching (M6): ``Matcher``, the ``expect`` namespace, the helpers and the diff.

``contains`` and ``unordered`` pair items with elements by maximum bipartite
matching. ``NodeList.where`` and ``one`` reuse the same object matching, so a
filter and an assertion can never disagree about what a field match means.
This package imports no pytest.
"""

from __future__ import annotations

from pytest_graphql._core.matching.core import Matcher, MatchResult, Mismatch
from pytest_graphql._core.matching.expect import ExpectNamespace, TypeExpectation
from pytest_graphql._core.matching.objects import ObjectMatcher
from pytest_graphql._core.matching.render import RenderOptions, render_diff
from pytest_graphql._core.matching.sequences import contains, unordered
from pytest_graphql._core.matching.values import (
    absent,
    any_length,
    any_value,
    gt,
    gte,
    length,
    lt,
    lte,
    matches,
    one_of,
)

__all__ = [
    "ExpectNamespace",
    "MatchResult",
    "Matcher",
    "Mismatch",
    "ObjectMatcher",
    "RenderOptions",
    "TypeExpectation",
    "absent",
    "any_length",
    "any_value",
    "contains",
    "gt",
    "gte",
    "length",
    "lt",
    "lte",
    "matches",
    "one_of",
    "render_diff",
    "unordered",
]
