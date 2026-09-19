"""``Node`` and ``NodeList`` (SPEC 3.4, B17).

A ``Node`` is a read-only mapping over one JSON object of a response. Its keys
are the server's own response keys. Values are wrapped lazily, one field at a
time, by the ``Materializer`` that produced the node, so ``raw`` data is never
copied or changed by reading it.

Its ``repr`` names the type and the selected field names only. It never
renders a value, so a secret in response data cannot leave through it.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from graphql import GraphQLNamedType, SelectionSetNode

from pytest_graphql._core.errors import GraphQLFieldError
from pytest_graphql._core.naming import NameMap

if TYPE_CHECKING:
    from pytest_graphql._core.response.materialize import FieldPlan, Materializer

MISSING: Any = object()
_REPR_FIELD_CAP = 20
_PATH_SEGMENT = re.compile(r"[^.\[\]]+|\[\d+\]")


class Node(Mapping[str, Any]):
    __slots__ = ("_cache", "_m", "_named", "_names", "_plan", "_raw", "_selections")

    def __init__(
        self,
        raw: dict[str, Any],
        materializer: Materializer,
        named: GraphQLNamedType,
        selections: tuple[SelectionSetNode, ...],
    ) -> None:
        self._raw = raw
        self._m = materializer
        self._named = named
        self._selections = selections
        self._plan: Mapping[str, FieldPlan] | None = None
        self._names: NameMap | None = None
        self._cache: dict[str, Any] = {}

    # -- introspection ------------------------------------------------------

    @property
    def __typename__(self) -> str | None:
        """The runtime type name, or ``None`` when the response carried none."""
        return self._m.runtime_name(self._raw, self._named)

    def _plan_for(self) -> Mapping[str, FieldPlan]:
        if self._plan is None:
            runtime = self._m.runtime_name(self._raw, self._named)
            self._plan = self._m.object_plan(self._named, self._selections, runtime)
        return self._plan

    def _name_map(self) -> NameMap:
        if self._names is None:
            self._names = NameMap.build(self._raw)
        return self._names

    def _resolve(self, key: str) -> str:
        names = self._name_map()
        exact = names.get(key)
        if exact is not None:
            return exact
        type_name = self.__typename__ or self._named.name
        if names.is_ambiguous(key):
            raise GraphQLFieldError.ambiguous(
                type_name, key, names.ambiguous_names(key)
            )
        raise GraphQLFieldError.unknown_field(type_name, key, list(self._raw))

    # -- mapping ------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        if not isinstance(key, str):
            raise GraphQLFieldError(
                f"a Node key must be a string, got {type(key).__name__}."
            )
        exact = self._resolve(key)
        if exact in self._cache:
            return self._cache[exact]
        field_plan = self._plan_for().get(exact)
        value = self._raw[exact]
        wrapped = (
            value
            if field_plan is None
            else self._m.wrap(value, field_plan.type_, field_plan.selections)
        )
        self._cache[exact] = wrapped
        return wrapped

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        names = self._name_map()
        return names.get(key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        if key in self or self._name_map().is_ambiguous(key):
            return self[key]  # an ambiguous key raises rather than defaulting
        return default

    def at(self, path: str, default: Any = MISSING) -> Any:
        return at_path(self, path, default)

    def to_dict(self) -> dict[str, Any]:
        """The raw object, original keys, as an independent copy."""
        return copy.deepcopy(self._raw)

    # -- comparison and rendering ------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Node):
            return self._raw == other._raw
        if isinstance(other, dict):
            return self._raw == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def _plan_keys(self) -> list[str]:
        """Response keys the selection asked for, whether or not returned."""
        return list(self._plan_for())

    def _selected_keys(self) -> list[str]:
        """Response keys the selection asked for and the server returned."""
        plan = self._plan_for()
        return [key for key in self._raw if key in plan]

    def __repr__(self) -> str:
        selected = self._selected_keys()
        extra = len(self._raw) - len(selected)
        shown = ", ".join(selected[:_REPR_FIELD_CAP])
        if len(selected) > _REPR_FIELD_CAP:
            shown += f", ... {len(selected) - _REPR_FIELD_CAP} more"
        if extra:
            shown += f"{', ' if shown else ''}+{extra} unselected"
        return f"{self.__typename__ or self._named.name}({shown})"

    __str__ = __repr__


class NodeList(list[Any]):
    """A list whose elements are ``Node`` values (or nested lists of them)."""

    def pluck(self, path: str, default: Any = MISSING) -> list[Any]:
        """``path`` read from every element; ``default`` fills a missing one."""
        return [at_path(element, path, default) for element in self]

    def ids(self) -> list[Any]:
        return self.pluck("id")

    def at(self, path: str, default: Any = MISSING) -> Any:
        return at_path(self, path, default)


def at_path(root: Any, path: str, default: Any = MISSING) -> Any:
    """Read a dotted path (``orders.0.total`` or ``orders[0].total``).

    Each segment resolves like ``Node.__getitem__``. A missing segment raises
    ``GraphQLFieldError`` unless ``default`` is given.
    """
    current = root
    for segment in _segments(path):
        try:
            current = _step(current, segment)
        except GraphQLFieldError:
            if default is MISSING:
                raise
            return default
    return current


def _segments(path: str) -> list[str | int]:
    segments: list[str | int] = []
    for token in _PATH_SEGMENT.findall(path):
        if token.startswith("["):
            segments.append(int(token[1:-1]))
        elif token.isdigit():
            segments.append(int(token))
        else:
            segments.append(token)
    return segments


def _step(current: Any, segment: str | int) -> Any:
    if isinstance(current, list):
        if isinstance(segment, int):
            try:
                return current[segment]
            except IndexError:
                raise GraphQLFieldError(
                    f"index {segment} is out of range for a list of {len(current)}."
                ) from None
        raise GraphQLFieldError(f"cannot read {segment!r} from a list; use an index.")
    if isinstance(current, Node):
        return current[str(segment)]
    if isinstance(current, Mapping):
        if segment in current:
            return current[segment]
        raise GraphQLFieldError(f"no key {segment!r} at this path.")
    raise GraphQLFieldError(f"cannot read {segment!r} from a {type(current).__name__}.")
