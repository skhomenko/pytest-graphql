"""The matcher protocol and the comparison walk (SPEC 3.6, DESIGN_DECISIONS 5).

A ``Matcher`` answers one question about an actual value: does it match? It
answers it in one of two modes. ``==`` runs the walk silent and stops at the
first difference, so an ``assert`` costs as little as a plain comparison.
``evaluate()`` runs it recording, and returns a ``MatchResult`` that counts
every compared field and keeps the differences for the diff renderer.

The recording is bounded. Counts stay exact however many fields differ, but
only ``MAX_RECORDED`` differences and as many matched paths are kept, so a
mismatch over a very large response cannot grow a structure without limit.

Nothing here prints a value. A ``Matcher``'s ``repr`` names fields and types
only, like ``Node``, because a test may put a credential in an expected value
and a failed assertion prints ``repr``. Showing a value is the renderer's job,
and it goes through the diagnostics scrub.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pytest_graphql._core.response.node import MISSING

if TYPE_CHECKING:
    from pytest_graphql._core.matching.render import RenderOptions

#: How many differences, and how many matched paths, one evaluation keeps.
MAX_RECORDED = 200
#: How much of an expected structure a description spells out.
DESCRIBE_DEPTH = 2
DESCRIBE_ENTRIES = 5

Path = tuple[str | int, ...]
Show = Callable[[Any], str]
Detail = Callable[[Show], str]


@dataclass(frozen=True)
class Said:
    """Library-built text that a renderer shows as it is, after the scrub.

    A plain ``str`` in a mismatch is a *value*, shown quoted like ``repr``.
    ``Said`` is for words such as ``<missing>``, which are not values.
    """

    text: str


def placeholder_show(value: Any) -> str:
    """The ``Show`` behind ``repr()``: a value becomes its type, never its content."""
    if isinstance(value, Said):
        return value.text
    if value is MISSING:
        return "<missing>"
    return f"<{type(value).__name__}>"


@dataclass(frozen=True)
class Mismatch:
    """One place where the actual value differs from what was expected."""

    path: Path
    actual: Any = field(repr=False)
    expected: Any = field(repr=False)
    detail: tuple[Detail, ...] = field(default=(), repr=False)

    def detail_text(self, show: Show = placeholder_show) -> list[str]:
        return [line(show) for line in self.detail]


@dataclass(frozen=True)
class MatchResult:
    """What one recording walk found. Counts are exact; the lists are bounded."""

    ok: bool
    root: Any = field(repr=False)
    compared: int
    differ: int
    ignored: int
    mismatches: tuple[Mismatch, ...]
    matched: tuple[Path, ...]
    dropped_mismatches: int = 0
    dropped_matched: int = 0


class Walk:
    """The state of one comparison. Silent when ``record`` is false."""

    __slots__ = (
        "compared",
        "differ",
        "dropped_matched",
        "dropped_mismatches",
        "ignored",
        "matched",
        "mismatches",
        "path",
        "record",
    )

    def __init__(self, record: bool = True) -> None:
        self.record = record
        self.path: list[str | int] = []
        self.compared = 0
        self.differ = 0
        self.ignored = 0
        self.mismatches: list[Mismatch] = []
        self.matched: list[Path] = []
        self.dropped_mismatches = 0
        self.dropped_matched = 0

    def push(self, segment: str | int) -> None:
        if self.record:
            self.path.append(segment)

    def pop(self) -> None:
        if self.record:
            self.path.pop()

    def leaf(
        self,
        ok: bool,
        actual: Any,
        expected: Any,
        detail: tuple[Detail, ...] = (),
    ) -> bool:
        """Record one compared field. Returns ``ok`` so callers can chain it."""
        if not self.record:
            return ok
        self.compared += 1
        if ok:
            if len(self.matched) < MAX_RECORDED:
                self.matched.append(tuple(self.path))
            else:
                self.dropped_matched += 1
        else:
            self.differ += 1
            if len(self.mismatches) < MAX_RECORDED:
                self.mismatches.append(
                    Mismatch(tuple(self.path), actual, expected, detail)
                )
            else:
                self.dropped_mismatches += 1
        return ok

    def result(self, root: Any, ok: bool) -> MatchResult:
        return MatchResult(
            ok=ok,
            root=root,
            compared=self.compared,
            differ=self.differ,
            ignored=self.ignored,
            mismatches=tuple(self.mismatches),
            matched=tuple(self.matched),
            dropped_mismatches=self.dropped_mismatches,
            dropped_matched=self.dropped_matched,
        )


#: A silent walk keeps no state, so one instance serves every pair test.
SILENT = Walk(record=False)


class Matcher(ABC):
    """Something a value can be compared with, to say whether it matches.

    You rarely build a `Matcher` directly. You get one from `gql.expect.Type(...)`
    or from a helper such as `contains()`, `gt()` or `matches()`. Helpers can
    also sit inside a plain `dict` or `list` that you compare with a response.

    Use a matcher in one of three ways:

    - `value == matcher` is `True` or `False`. It stops at the first
      difference, so it costs no more than a normal comparison. Under pytest,
      a failed `assert` prints a short list of the fields that differ.
    - `matcher.evaluate(value)` compares again and keeps every difference.
    - `matcher.explain(value)` returns the lines of that list as text.

    A matcher is partial. It checks the fields it names and ignores the rest.
    `repr()` of a matcher shows type and field names only, never the expected
    values, so a secret in an expected value does not appear in a test report.
    Matchers cannot be used as dictionary keys or set members.

    Examples:
        ```python {.exec}
        from pytest_graphql import Matcher, contains

        user = gql.query("user", id="u1")
        matcher = gql.expect.User(name="Ada Lovelace")
        assert isinstance(matcher, Matcher)
        assert user == matcher
        assert gql.query("users") == contains(matcher)
        ```
    """

    __slots__ = ()

    @abstractmethod
    def _check(self, actual: Any, walk: Walk) -> bool:
        """Compare ``actual``, recording through ``walk``. True when it matches."""

    @abstractmethod
    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        """Describe what this matcher expects, in a short text.

        Every expected value and every field name goes through `show`, so the
        caller decides what text a value may become. `repr(matcher)` is this
        method with a `show` that prints only the name of each value's type.

        Args:
            show: A function that returns the text for one value.
            depth: How many levels of nested expectations to spell out. Deeper
                levels are summarized by count.

        Returns:
            The description.

        Examples:
            ```python {.exec}
            matcher = gql.expect.User(name="Ada")
            assert matcher.describe(lambda value: "?") == "User(?=?)"
            assert repr(matcher) == "User(name=<str>)"
            ```
        """

    def evaluate(self, actual: Any) -> MatchResult:
        """Compare `actual` with this matcher and keep every difference.

        Unlike `==`, this does not stop at the first difference. The counts in
        the result are exact. The lists of differences and of matched paths
        keep at most 200 entries each, so a huge response cannot use unbounded
        memory.

        Args:
            actual: The value to compare, such as a `Node` or a `NodeList`.

        Returns:
            A result with these attributes: `ok` (the verdict), `compared` (how
            many fields were compared), `differ` (how many of them differ),
            `ignored` (how many fields of the compared objects the matcher did
            not name), `mismatches` (the differences) and `matched` (the paths
            that matched).

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id", "name"])

            result = gql.expect.User(name="Ada").evaluate(user)
            assert not result.ok
            assert (result.compared, result.differ, result.ignored) == (1, 1, 1)
            assert result.mismatches[0].path == ("name",)
            ```
        """
        walk = Walk(record=True)
        ok = self._check(actual, walk)
        return walk.result(self, ok)

    def explain(self, actual: Any, options: RenderOptions | None = None) -> list[str]:
        """Return the lines that show how `actual` differs from this matcher.

        The first line is a summary. The lines after it name each field that
        differs, with the actual value and the expected one, and then the
        fields that matched. A long list is cut and says how many entries it
        left out. Values are cleaned of control characters and cut to a
        length limit. When the result is empty, the value matches.

        Args:
            actual: The value to compare.
            options: How to print the lines, such as the limits on line count and
                value length. Leave it out for the defaults. Under pytest, the
                failure report passes options that also remove the secrets of the
                request that produced the response.

        Returns:
            The lines, or an empty list when `actual` matches.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id", "name"])

            lines = gql.expect.User(name="Ada").explain(user)
            assert lines[0] == (
                "  User does not match "
                "(1 of 1 compared field differs; 1 response field ignored)"
            )
            assert "'Ada Lovelace'" in lines[1]
            assert gql.expect.User(name="Ada Lovelace").explain(user) == []
            ```
        """
        from pytest_graphql._core.matching.render import render_diff

        return render_diff(self.evaluate(actual), options)

    def __eq__(self, other: object) -> bool:
        return self._check(other, SILENT)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return self.describe(placeholder_show)

    __str__ = __repr__


class LeafMatcher(Matcher):
    """A matcher that decides one value and is one compared field."""

    __slots__ = ()

    @abstractmethod
    def _test(self, actual: Any) -> bool: ...

    def _check(self, actual: Any, walk: Walk) -> bool:
        return walk.leaf(self._test(actual), actual, self)


def values_equal(expected: Any, actual: Any) -> bool:
    """Equality where a JSON boolean never equals a JSON number.

    Python says ``True == 1``. A GraphQL ``Boolean`` and an ``Int`` are
    different values, so a test that expects ``1`` must not pass on ``true``.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (
            isinstance(expected, bool)
            and isinstance(actual, bool)
            and expected == actual
        )
    return bool(expected == actual)


def check_value(expected: Any, actual: Any, walk: Walk) -> bool:
    """Compare one expected value, matcher, dict or list with ``actual``."""
    if isinstance(expected, Matcher):
        return expected._check(actual, walk)
    if isinstance(expected, Mapping):
        from pytest_graphql._core.matching.objects import check_fields

        fields = tuple((str(key), key, value) for key, value in expected.items())
        return check_fields(fields, actual, walk, expected=expected)
    if isinstance(expected, (list, tuple)):
        return check_sequence(expected, actual, walk)
    return walk.leaf(values_equal(expected, actual), actual, expected)


def check_sequence(expected: Any, actual: Any, walk: Walk) -> bool:
    """A plain list is positional: equal length, each element compared by index."""
    if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
        return walk.leaf(False, actual, expected)
    if not expected:
        return walk.leaf(True, actual, expected)
    ok = True
    for index, (want, got) in enumerate(zip(expected, actual, strict=True)):
        walk.push(index)
        good = check_value(want, got, walk)
        walk.pop()
        if not good:
            ok = False
            if not walk.record:
                return False
    return ok


def describe_expected(expected: Any, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
    """A bounded rendering of an expected matcher, dict, list or value."""
    if isinstance(expected, Matcher):
        return expected.describe(show, depth)
    if isinstance(expected, Mapping):
        if depth <= 0:
            return f"{{{plural(len(expected), 'field')}}}"
        entries = [
            f"{show(str(key))}: {describe_expected(value, show, depth - 1)}"
            for key, value in _head(expected.items())
        ]
        return "{" + _joined(entries, len(expected)) + "}"
    if isinstance(expected, (list, tuple)):
        if depth <= 0:
            return f"[{plural(len(expected), 'item')}]"
        entries = [describe_expected(item, show, depth - 1) for item in _head(expected)]
        return "[" + _joined(entries, len(expected)) + "]"
    return show(expected)


def _head(items: Any) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if len(out) == DESCRIBE_ENTRIES:
            break
        out.append(item)
    return out


def _joined(entries: list[str], total: int) -> str:
    text = ", ".join(entries)
    if total > len(entries):
        text += f", ... {total - len(entries)} more"
    return text


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
