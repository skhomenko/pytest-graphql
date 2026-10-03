"""The value helpers of SPEC 3.6, minus ``approx`` (PLAN D4).

Each helper is a ``Matcher`` that decides one value and counts as one compared
field. None of them raises on a value of the wrong kind: a regex asked about a
number, or ``gt`` asked about a string, simply does not match, so a failed
assertion shows the diff and not a ``TypeError`` from inside the helper.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping
from typing import Any

from pytest_graphql._core.matching.core import (
    DESCRIBE_DEPTH,
    LeafMatcher,
    Show,
    describe_expected,
    plural,
    values_equal,
)


class AnyValue(LeafMatcher):
    """The field must be present. Its value, ``null`` included, is ignored."""

    __slots__ = ()

    def _test(self, actual: Any) -> bool:
        return True

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return "any_value()"


class Absent(LeafMatcher):
    """The field must not be in the response.

    A field is judged missing before any value is read, so this matcher only
    reaches ``_test`` when a value exists, and then it fails.
    """

    __slots__ = ()

    def _test(self, actual: Any) -> bool:
        return False

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return "absent()"


class AnyLength(LeafMatcher):
    """The value must be a list, of any length."""

    __slots__ = ()

    def _test(self, actual: Any) -> bool:
        return isinstance(actual, (list, tuple))

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return "any_length()"


class Length(LeafMatcher):
    """The value must be a list of exactly ``count`` elements."""

    __slots__ = ("_count",)

    def __init__(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"length() needs an int, got {type(count).__name__}.")
        if count < 0:
            raise ValueError("length() needs a count of zero or more.")
        self._count = count

    def _test(self, actual: Any) -> bool:
        return isinstance(actual, (list, tuple)) and len(actual) == self._count

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return f"length({self._count})"


class Matches(LeafMatcher):
    """The value must be a string that the regular expression finds.

    It is a search, not a full match, so anchors are the author's to write:
    ``matches(r"^u_\\d+$")``.
    """

    __slots__ = ("_regex",)

    def __init__(self, pattern: str | re.Pattern[str]) -> None:
        if isinstance(pattern, re.Pattern):
            self._regex = pattern
        elif isinstance(pattern, str):
            self._regex = re.compile(pattern)
        else:
            raise TypeError(
                f"matches() needs a str or a compiled pattern, got "
                f"{type(pattern).__name__}."
            )

    def _test(self, actual: Any) -> bool:
        return isinstance(actual, str) and self._regex.search(actual) is not None

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return f"matches({show(self._regex.pattern)})"


_COMPARISONS: dict[str, Callable[[Any, Any], Any]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def _comparable(value: Any) -> bool:
    """A scalar that has an order. Not a boolean, ``null`` or a container."""
    return not (
        value is None
        or isinstance(value, (bool, Mapping, list, tuple, set, frozenset, bytes))
    )


class Comparison(LeafMatcher):
    """``actual OP bound`` for numbers, dates and ISO date strings.

    A boolean is not a number here. A value that cannot be ordered against the
    bound, such as a string against an int, does not match and does not raise.
    """

    __slots__ = ("_bound", "_name", "_op")

    def __init__(self, name: str, bound: Any) -> None:
        if not _comparable(bound):
            raise TypeError(
                f"{name}() needs a number, a date or a string to compare with, "
                f"got {type(bound).__name__}."
            )
        self._name = name
        self._op = _COMPARISONS[name]
        self._bound = bound

    def _test(self, actual: Any) -> bool:
        if not _comparable(actual):
            return False
        try:
            return bool(self._op(actual, self._bound))
        except TypeError:
            return False

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        return f"{self._name}({show(self._bound)})"


class OneOf(LeafMatcher):
    """The value must equal one of the given values."""

    __slots__ = ("_values",)

    def __init__(self, values: tuple[Any, ...]) -> None:
        if not values:
            raise ValueError("one_of() needs at least one value.")
        self._values = values

    def _test(self, actual: Any) -> bool:
        return any(values_equal(value, actual) for value in self._values)

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        if depth <= 0:
            return f"one_of({plural(len(self._values), 'value')})"
        return (
            "one_of(" + describe_expected(list(self._values), show, depth)[1:-1] + ")"
        )


def any_value() -> AnyValue:
    """The field must be present; its value is ignored."""
    return AnyValue()


def absent() -> Absent:
    """The field must not be in the response. Takes no argument.

    It is a value placed on a field: ``gql.expect.User(deleted_at=absent())``.
    """
    return Absent()


def any_length() -> AnyLength:
    """The value must be a list, of any length."""
    return AnyLength()


def length(count: int) -> Length:
    """The value must be a list of exactly ``count`` elements."""
    return Length(count)


def matches(pattern: str | re.Pattern[str]) -> Matches:
    """The value must be a string in which ``pattern`` is found."""
    return Matches(pattern)


def gt(bound: Any) -> Comparison:
    """The value must be greater than ``bound``."""
    return Comparison("gt", bound)


def gte(bound: Any) -> Comparison:
    """The value must be greater than or equal to ``bound``."""
    return Comparison("gte", bound)


def lt(bound: Any) -> Comparison:
    """The value must be less than ``bound``."""
    return Comparison("lt", bound)


def lte(bound: Any) -> Comparison:
    """The value must be less than or equal to ``bound``."""
    return Comparison("lte", bound)


def one_of(*values: Any) -> OneOf:
    """The value must equal one of ``values``."""
    return OneOf(values)
