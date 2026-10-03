"""Object matching: ``expect.Type(**fields)``, plain dicts, and ``where`` filters.

An object matcher is partial. Only the fields it names are compared, and every
other field of the response is ignored and counted. A named field that the
response does not carry fails, except under ``absent()``, which asserts exactly
that. ``strict`` additionally requires the response to carry no other field.

The type name is checked only when the response object carries one
(``__typename``), so a matcher stays usable against an explicit selection that
left ``__typename`` out (B12).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pytest_graphql._core.matching.core import (
    DESCRIBE_DEPTH,
    DESCRIBE_ENTRIES,
    SILENT,
    Matcher,
    Said,
    Show,
    Walk,
    check_value,
    describe_expected,
    plural,
)
from pytest_graphql._core.matching.values import Absent
from pytest_graphql._core.response.node import MISSING, Node, resolve_key

#: ``(label, key, expected)``: the name as written, the name to look up, and
#: the expected value, matcher, dict or list.
Fields = tuple[tuple[str, str, Any], ...]


def lookup(actual: Mapping[Any, Any], key: Any) -> Any:
    """The key ``actual`` holds for ``key``, or ``None`` when it has none."""
    if isinstance(actual, Node):
        return resolve_key(actual, key)
    return key if key in actual else None


def check_fields(
    fields: Fields,
    actual: Any,
    walk: Walk,
    *,
    expected: Any,
    strict: bool = False,
    accepted_types: frozenset[str] | None = None,
    type_name: str = "",
) -> bool:
    """Compare the named fields of ``actual``. The rule for every object match."""
    if not isinstance(actual, Mapping):
        return walk.leaf(False, actual, expected)
    if accepted_types is not None and isinstance(actual, Node):
        runtime = actual.__typename__
        if runtime is not None and runtime not in accepted_types:
            walk.push("__typename")
            walk.leaf(False, runtime, type_name)
            walk.pop()
            return False
    ok = True
    carried: set[Any] = set()
    for label, key, want in fields:
        found = lookup(actual, key)
        walk.push(label)
        if isinstance(want, Absent):
            if found is None:
                good = walk.leaf(True, MISSING, want)
            else:
                carried.add(found)
                good = walk.leaf(False, actual[found], want)
        elif found is None:
            good = walk.leaf(False, MISSING, want)
        else:
            carried.add(found)
            good = check_value(want, actual[found], walk)
        walk.pop()
        if not good:
            ok = False
            if not walk.record:
                return False
    extra = len(actual) - len(carried)
    if walk.record:
        walk.ignored += extra
    if strict and extra:
        return walk.leaf(
            False, Said(plural(extra, "other field")), Said("no other fields")
        )
    return ok


class ObjectMatcher(Matcher):
    """The matcher ``gql.expect.Type(**fields)`` builds."""

    __slots__ = ("_accepted", "_fields", "_strict", "_type_name")

    def __init__(
        self,
        type_name: str,
        fields: Fields,
        *,
        accepted: frozenset[str] | None = None,
        strict: bool = False,
    ) -> None:
        self._type_name = type_name
        self._fields = fields
        self._accepted = accepted
        self._strict = strict

    @property
    def type_name(self) -> str:
        return self._type_name

    def _check(self, actual: Any, walk: Walk) -> bool:
        return check_fields(
            self._fields,
            actual,
            walk,
            expected=self,
            strict=self._strict,
            accepted_types=self._accepted,
            type_name=self._type_name,
        )

    def accepts(self, actual: Any) -> bool:
        """Whether ``actual`` matches, without recording anything."""
        return self._check(actual, SILENT)

    def field_names(self) -> list[str]:
        return [label for label, _, _ in self._fields]

    def describe(self, show: Show, depth: int = DESCRIBE_DEPTH) -> str:
        head = self._type_name or "object"
        labels = [show(Said(label)) for label, _, _ in self._fields]
        if depth <= 0:
            shown = labels[:DESCRIBE_ENTRIES]
        else:
            shown = [
                f"{show(Said(label))}={describe_expected(want, show, depth - 1)}"
                for label, _, want in self._fields[:DESCRIBE_ENTRIES]
            ]
        text = ", ".join(shown)
        if len(self._fields) > DESCRIBE_ENTRIES:
            text += f", ... {len(self._fields) - DESCRIBE_ENTRIES} more"
        return f"{head}({text})"


def field_filter(filters: Mapping[str, Any], *, strict: bool = False) -> ObjectMatcher:
    """The matcher ``NodeList.where(**filters)`` uses: no type, no schema check."""
    fields = tuple((name, name, want) for name, want in filters.items())
    return ObjectMatcher("", fields, strict=strict)
