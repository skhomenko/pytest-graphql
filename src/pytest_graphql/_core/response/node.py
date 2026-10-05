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

from pytest_graphql._core.errors import GraphQLFieldError, GraphQLTestError
from pytest_graphql._core.naming import NameMap

if TYPE_CHECKING:
    from pytest_graphql._core.response.materialize import FieldPlan, Materializer

MISSING: Any = object()
_REPR_FIELD_CAP = 20
_PATH_SEGMENT = re.compile(r"[^.\[\]]+|\[\d+\]")


class Node(Mapping[str, Any]):
    """One object from a response, such as a `User`, read like a record.

    A `Node` is a read-only mapping. Read a field as an attribute
    (`user.name`) or as a key (`user["name"]`). Both the exact schema name and
    the snake_case form work, so `user.joined_at` reads the field `joinedAt`.
    Reading a field that the response does not hold raises `GraphQLFieldError`.
    The error lists the fields that are there and suggests a close match. It also
    reminds you that the field may exist in the schema and still be missing from
    your selection.

    Values are read lazily. A field that holds an object gives a `Node`, and a
    field that holds a list of objects gives a `NodeList`. A custom scalar gives
    the value its registered `parse` function returns, or the raw JSON value
    when it has none. A key the selection did not ask for is read as raw JSON.

    Iteration and `len()` use the server's own keys. `dict(node)` gives those
    keys with the values wrapped as above. `node == other` is true for another
    node or a `dict` with exactly the same data. For a partial comparison, use a
    matcher such as `gql.expect.User(name="Ada")`. A `Node` cannot be used as a
    dictionary key.

    `repr()` shows the type name and the field names. It never shows a value, so
    a secret in the data cannot leak into a test report.

    You do not create a `Node`. The client builds them from responses.

    Examples:
        ```python {.exec}
        user = gql.query("user", id="u1")

        assert user.name == "Ada Lovelace"
        assert user["name"] == "Ada Lovelace"
        assert user.team.name == "Core"
        assert user.joined_at == user["joinedAt"]
        assert "name" in user
        ```
    """

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
        """The name of the object's type, or `None` when it is not known.

        For a field whose schema type is an object type, the name is always
        known. For an interface or a union, it is known only when the response
        has `__typename`. The client adds `__typename` to every selection it
        generates. In a selection you write yourself, ask for it by name.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id"])
            assert user.__typename__ == "User"

            node = gql.query("node", id="u1", fields=["id"])
            assert node.__typename__ is None

            node = gql.query("node", id="u1", fields=["id", "__typename"])
            assert node.__typename__ == "User"
            ```
        """
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
        """Read a field by its schema name or its snake_case name.

        Args:
            key: The field name.

        Returns:
            The field value. A nested object is a `Node`, and a list of objects is a
            `NodeList`.

        Raises:
            GraphQLFieldError: When the key is not a string, the response has no
                such field, or the snake_case name matches two fields. The error
                is also a `KeyError` and an `AttributeError`.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1")
            assert user["name"] == "Ada Lovelace"
            assert user["joined_at"] == user["joinedAt"]
            ```
        """
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
        """Read a field as an attribute. It works like `node[name]`.

        A name that starts with an underscore is never read as a field. Use
        `node["_name"]` for a field whose name starts with one.

        Args:
            name: The field name.

        Returns:
            The field value.

        Raises:
            GraphQLFieldError: When the response has no such field. The error is
                also an `AttributeError`, so `getattr(node, "x", default)` and
                `hasattr(node, "x")` work as usual.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1")
            assert user.name == "Ada Lovelace"
            assert getattr(user, "nickname", "none") == "none"
            ```
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys exactly as the server returned them.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id", "name"])
            assert list(user) == ["id", "name"]
            ```
        """
        return iter(self._raw)

    def __len__(self) -> int:
        """Count the keys the server returned for this object.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id", "name"])
            assert len(user) == 2
            ```
        """
        return len(self._raw)

    def __contains__(self, key: object) -> bool:
        """Tell whether the object has a field, by either spelling of its name.

        Args:
            key: The field name. Anything that is not a string gives `False`.

        Returns:
            `True` when the response has the field.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id", "joinedAt"])
            assert "joinedAt" in user
            assert "joined_at" in user
            assert "name" not in user
            ```
        """
        if not isinstance(key, str):
            return False
        names = self._name_map()
        return names.get(key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        """Read a field, or return `default` when the response does not have it.

        A snake_case name that matches two fields is still an error, because
        returning a default would hide that.

        Args:
            key: The field name, in either spelling.
            default: The value to return for a missing field.

        Returns:
            The field value, or `default`.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u1", fields=["id"])
            assert user.get("id") == "u1"
            assert user.get("name") is None
            assert user.get("name", "unknown") == "unknown"
            ```
        """
        if key in self or self._name_map().is_ambiguous(key):
            return self[key]  # an ambiguous key raises rather than defaulting
        return default

    def at(self, path: str, default: Any = MISSING) -> Any:
        """Read a nested value by a dotted path.

        A segment is a field name or a list index. Write an index as a number
        (`friends.0.name`) or in brackets (`friends[0].name`). Each name works
        in its exact schema spelling and in snake_case.

        Args:
            path: The path to read.
            default: What to return when a segment does not exist. Without it,
                a missing segment raises.

        Returns:
            The value at the path.

        Raises:
            GraphQLFieldError: When a segment does not exist and no `default`
                is given.

        Examples:
            ```python {.exec}
            user = gql.query("user", id="u2")
            assert user.at("manager.name") == "Ada Lovelace"
            assert user.at("friends[0].name") == "Ada Lovelace"
            assert user.at("manager.nickname", default=None) is None
            ```
        """
        return at_path(self, path, default)

    def to_dict(self) -> dict[str, Any]:
        """Return the object as a plain `dict` of the data the server returned.

        The keys are the server's response keys. Nested values are plain JSON
        values, and custom scalars are not decoded. The copy is independent, so
        changing it does not change the response.

        Examples:
            ```python {.exec}
            fields = ["id", {"team": ["name"]}]
            user = gql.query("user", id="u1", fields=fields)
            assert user.to_dict() == {"id": "u1", "team": {"name": "Core"}}
            ```
        """
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
    """A list of `Node` values, with helpers to filter and read them.

    A field that holds a list of objects gives a `NodeList`. It is a normal
    `list`, so `len()`, indexing, slicing and iteration work. A list of lists
    keeps the nesting, and the inner lists are `NodeList` too.

    Examples:
        ```python {.exec}
        users = gql.query("users", fields=["id", "name"])

        assert len(users) == 3
        assert users[0].name == "Ada Lovelace"
        assert users.pluck("name") == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
        assert users.one(id="u2").name == "Grace Hopper"
        ```
    """

    def where(self, *, strict: bool = False, **filters: Any) -> NodeList:
        """Return the elements that match every filter.

        Each keyword is a field name and what its value must be. The value is
        a plain value or a matcher such as `matches()` or `gt()`. A field name
        works in either spelling. The match is partial: an element matches when
        each named field matches, and its other fields are ignored. An element
        that is not a `Node` never matches.

        The result is a new `NodeList`, so `len(users.where(...))` is the number
        of matches.

        Args:
            strict: Also require that the element has no field besides the
                named ones. Because this is a keyword of `where`, you cannot
                filter on a field that is itself named `strict`.
            **filters: The field filters.

        Returns:
            A new list of the elements that match.

        Examples:
            ```python {.exec}
            from pytest_graphql import matches

            users = gql.query("users", fields=["id", "name"])
            assert users.where(name=matches("^A")).pluck("id") == ["u1", "u3"]
            exact = users.where(id="u1", name="Ada Lovelace", strict=True)
            assert exact.ids() == ["u1"]
            assert users.where(id="u1", strict=True) == []
            ```
        """
        from pytest_graphql._core.matching.objects import field_filter

        wanted = field_filter(filters, strict=strict)
        return NodeList(
            element
            for element in self
            if isinstance(element, Node) and wanted.accepts(element)
        )

    def one(self, *, strict: bool = False, **filters: Any) -> Any:
        """Return the one element that matches the filters.

        The filters work as in `where()`.

        Args:
            strict: Also require that the element has no field besides the
                named ones.
            **filters: The field filters.

        Returns:
            The matching element.

        Raises:
            GraphQLTestError: When zero or several elements match. The message
                gives the number found, the size of the list and the names of
                the filters. It does not print any response value.

        Examples:
            ```python {.exec}
            users = gql.query("users", fields=["id", "name"])
            assert users.one(name="Grace Hopper").id == "u2"
            ```
        """
        found = self.where(strict=strict, **filters)
        if len(found) != 1:
            named = f" Filters: {', '.join(filters)}." if filters else ""
            raise GraphQLTestError(
                f"one() needs exactly 1 matching element, found {len(found)} "
                f"of {len(self)}.{named}"
            )
        return found[0]

    def pluck(self, path: str, default: Any = MISSING) -> list[Any]:
        """Read the same path from every element and return the values as a list.

        The path works as in `Node.at()`.

        Args:
            path: The dotted path to read from each element.
            default: What to use for an element that lacks the path. Without it,
                a missing path raises.

        Returns:
            One value for each element, in order.

        Raises:
            GraphQLFieldError: When an element lacks the path and no `default`
                is given.

        Examples:
            ```python {.exec}
            fields = ["name", {"manager": ["name"]}]
            users = gql.query("users", fields=fields)
            names = ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
            assert users.pluck("name") == names
            managers = users.pluck("manager.name", default=None)
            assert managers == [None, "Ada Lovelace", "Ada Lovelace"]
            ```
        """
        return [at_path(element, path, default) for element in self]

    def ids(self) -> list[Any]:
        """Return the `id` of every element. It is `pluck("id")`.

        Examples:
            ```python {.exec}
            users = gql.query("users", fields=["id"])
            assert users.ids() == ["u1", "u2", "u3"]
            ```
        """
        return self.pluck("id")

    def at(self, path: str, default: Any = MISSING) -> Any:
        """Read a value from the list by a path that starts with an index.

        Write the index as a number (`0.name`) or in brackets (`[0].name`).

        Args:
            path: The path to read.
            default: What to return when a segment does not exist. Without it,
                a missing segment raises.

        Returns:
            The value at the path.

        Raises:
            GraphQLFieldError: When a segment does not exist and no `default`
                is given.

        Examples:
            ```python {.exec}
            users = gql.query("users", fields=["id", "name"])
            assert users.at("0.name") == "Ada Lovelace"
            assert users.at("[1].name") == "Grace Hopper"
            assert users.at("[9].name", default=None) is None
            ```
        """
        return at_path(self, path, default)


def resolve_key(node: Node, name: object) -> str | None:
    """The response key ``name`` names on ``node``, or ``None`` when it has none.

    Exact and snake_case spellings both resolve, like ``Node.__getitem__``. A
    snake spelling that two keys share raises ``GraphQLFieldError``, as
    reading it would.
    """
    if not isinstance(name, str):
        return None
    names = node._name_map()
    exact = names.get(name)
    if exact is not None:
        return exact
    if names.is_ambiguous(name):
        raise GraphQLFieldError.ambiguous(
            node.__typename__ or node._named.name, name, names.ambiguous_names(name)
        )
    return None


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
