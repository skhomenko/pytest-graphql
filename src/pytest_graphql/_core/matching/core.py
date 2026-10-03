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
    """Base of every matcher. Compare one with ``==`` or ask it to ``evaluate``."""

    __slots__ = ()

    @abstractmethod
    def _check(self, actual: Any, walk: Walk) -> bool:
        """Compare ``actual``, recording through ``walk``. True when it matches."""

    @abstractmethod
    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        """A short rendering of what this matcher expects, values through ``show``."""

    def evaluate(self, actual: Any) -> MatchResult:
        """Compare ``actual`` and keep every difference, with exact counts."""
        walk = Walk(record=True)
        ok = self._check(actual, walk)
        return walk.result(self, ok)

    def explain(self, actual: Any, options: RenderOptions | None = None) -> list[str]:
        """The diff lines for ``actual``, empty when it matches (SPEC 7.5)."""
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
