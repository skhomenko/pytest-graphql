"""What a user writes: ``AUTO``, ``Field``, ``InlineFragment``, ``Selection``.

SPEC 3.3 gives four interchangeable input forms, and this module is the third
of them plus the reusable bundle that holds any of the others. Nothing here
touches a schema. Every name in a selection is resolved later, against the
schema, in ``normalize.py``, because that is the only place where a name can
be checked and a good error written.

``+`` and ``-`` do not merge or remove anything themselves. They record the
operation and nest the operands, so ``(a + b) - "x"`` removes from the union
while ``(a - "x") + b`` does not. A merge cannot happen before a schema is
available anyway: two fields conflict when they share a response key, and a
raw GraphQL string does not reveal its response keys until it is parsed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from types import MappingProxyType
from typing import Any, Final, Union

from pytest_graphql._core.errors import SelectionError

#: The GraphQL Name grammar, from the specification's Name production.
#: Matched with ``fullmatch``: ``$`` also matches just before a final
#: newline, which would let a trailing line break through.
_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")


def check_graphql_name(value: str, what: str) -> None:
    """Refuse a user string that would be written into the document as a name.

    The engine's rule is that every user-supplied string it writes as a name is
    either resolved against the schema or matched against the Name grammar
    first. A field name and a type condition take the first route and so can
    only ever be a real name. An alias has no schema to resolve against, and
    the GraphQL grammar has no variable position for one, so matching the
    grammar is the only defence available.

    It has to be a defence, not a nicety. A name is written into the document
    as one opaque AST node, so a value carrying braces or a whole selection set
    prints as extra document text while the tree still looks like a single
    field. Validation walks the tree, so it cannot see inside that node: the
    document that would leave the process is then not the document that was
    validated.
    """
    if not _GRAPHQL_NAME.fullmatch(value):
        raise SelectionError(
            f"{what} {value!r} is not a GraphQL name.\n"
            "  A name starts with a letter or an underscore, followed by "
            "letters, digits or underscores.\n"
            "  Rename it, or leave it out."
        )


class _Auto:
    """The type of ``AUTO``. One instance, and it is a sentinel, not a value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AUTO"


#: Generate the selection for this position from the schema and the policy.
AUTO: Final[_Auto] = _Auto()
"""Ask the client to choose the fields for this position.

`AUTO` is the default for `fields=` on `query()` and `mutation()`, so you
rarely write it. Use it to mix fields you wrote with fields that the client
chooses: `AUTO` can be the whole `fields=` value, or the sub-selection of one
field, as in `Field("team", fields=AUTO)` or `{"team": AUTO}`. It cannot be one
item in a list.

The client generates the selection from the schema and the selection settings of
`ClientConfig`. For example, `max_depth` bounds how deep the generated part
goes. Those limits apply to the generated part only, and never to fields you
wrote yourself.

Examples:
    ```python {.exec}
    from pytest_graphql import AUTO

    # The id and name of the user, and the fields the client chooses for the team.
    response = gql.query(
        "user",
        id="u1",
        fields=["id", "name", {"team": AUTO}],
        raw=True,
    )
    assert "team {" in response.request.document
    assert response.data.user.team.name == "Core"
    ```
"""

SelectionItem = Union[
    str,
    "Field",
    "Selection",
    "InlineFragment",
    Mapping[str, "SelectionInput"],
]

SelectionInput = Union[
    _Auto,
    str,
    "Field",
    "Selection",
    "InlineFragment",
    Sequence["SelectionItem"],
    Mapping[str, "SelectionInput"],
]


@dataclass(frozen=True)
class Field:
    """One field in a selection, when a plain name is not enough.

    Write a plain string for a field with no arguments. Use `Field` when the
    field takes arguments, needs an alias, or applies only to one type of an
    interface or union.

    Argument values are Python values. They are never written into the query
    text. Each one is sent as a variable, so a value cannot change the shape of
    the query.

    Args:
        name: The field name in the schema. The snake_case form of a
            camelCase name is accepted too.
        args: Argument values for the field, by argument name.
        alias: A name for the field in the response. Use it to ask for the same
            field twice with different arguments. It must be a valid GraphQL
            name.
        fields: What to select inside the field. A list of names, nested
            fields, `AUTO` or a `Selection`. Leave it out for a field with no
            sub-fields. A field that has sub-fields and no `fields` is an error,
            because a selection you write adds nothing on its own.
        on: The name of an object type. The field is then selected only for
            values of that type, as in `... on User { name }`.
        directives: Not supported in this version. A non-empty value raises an
            error. To send a query with directives, use `GraphQLClient.execute()`.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        response = gql.query(
            "user",
            id="u1",
            fields=[
                "name",
                Field("posts", args={"first": 5}, alias="recent", fields=["title"]),
            ],
            raw=True,
        )
        assert "recent: posts(first: $posts_first)" in response.request.document
        assert response.data.user.recent[0].title == "Hello World"
        ```
    """

    name: str
    """The field name, as written in the schema or in snake_case.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        field = Field(
            "posts", args={"first": 5}, alias="recent", fields=["title"], on="User"
        )
        assert field.name == "posts"
        ```
    """
    _: KW_ONLY
    args: Mapping[str, Any] | None = None
    """Argument values by argument name, or `None` for no arguments.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        field = Field(
            "posts", args={"first": 5}, alias="recent", fields=["title"], on="User"
        )
        assert field.args == {"first": 5}
        assert Field("name").args is None
        ```
    """
    alias: str | None = None
    """The name this field has in the response, or `None` to use `name`.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        field = Field(
            "posts", args={"first": 5}, alias="recent", fields=["title"], on="User"
        )
        assert field.alias == "recent"
        assert Field("name").alias is None
        ```
    """
    fields: SelectionInput | None = None
    """What to select inside this field, or `None` when nothing is selected.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        field = Field(
            "posts", args={"first": 5}, alias="recent", fields=["title"], on="User"
        )
        assert field.fields == ["title"]
        assert Field("name").fields is None
        ```
    """
    on: str | None = None
    """An object type that this field is limited to, or `None` for no limit.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        field = Field(
            "posts", args={"first": 5}, alias="recent", fields=["title"], on="User"
        )
        assert field.on == "User"
        assert Field("name").on is None
        ```
    """
    directives: Mapping[str, Mapping[str, Any]] | None = field(default=None)
    """Not supported yet. Setting a value raises an error.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field

        assert Field("name").directives is None
        try:
            Field("name", directives={"include": {"if": True}})
        except Exception as error:
            assert "not supported" in str(error)
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __post_init__(self) -> None:
        if self.directives:
            raise SelectionError(
                f"directives are not supported in v0.1, and {self.name!r} "
                f"uses {', '.join(sorted(self.directives))}.\n"
                "  Send a document with directives through gql.execute(...) "
                "instead."
            )
        if self.alias is not None:
            check_graphql_name(self.alias, "alias")
        if self.args is not None:
            object.__setattr__(self, "args", MappingProxyType(dict(self.args)))

    @property
    def response_key(self) -> str:
        """The key this field has in the response data: the alias, or else the name.

        Examples:
            ```python {.exec}
            from pytest_graphql import Field

            assert Field("posts", alias="recent").response_key == "recent"
            assert Field("posts").response_key == "posts"
            ```
        """
        return self.alias or self.name


@dataclass(frozen=True)
class InlineFragment:
    """``... on TypeName { ... }``. Produced by ``Selection.of``."""

    on: str
    fields: SelectionInput = AUTO


class Selection:
    """A reusable group of fields that you define once and use in many calls.

    The parts can be any of the forms that `fields=` accepts, except `AUTO` on its
    own: a field name, a nested mapping, a `Field`, a raw GraphQL string or
    another `Selection`. `AUTO` can still be the sub-selection of a part, as in
    `Field("team", fields=AUTO)`. When a field changes in your schema, you edit
    the one `Selection` instead of every test that reads it.

    A `Selection` never changes. Combine them with `+`, and remove a field with
    `-`. Each operator returns a new `Selection`.

    Args:
        *parts: The items to select.

    Examples:
        ```python {.exec}
        from pytest_graphql import Field, Selection

        USER_BRIEF = Selection("id", "name")
        USER_WITH_TEAM = USER_BRIEF + Selection({"team": ["name"]})

        user = gql.query("user", id="u2", fields=USER_WITH_TEAM)
        assert user.name == "Grace Hopper"
        assert user.team.name == "Core"
        ```
    """

    __slots__ = ("_parts", "_removals")

    def __init__(self, *parts: SelectionItem) -> None:
        self._parts: tuple[SelectionItem, ...] = parts
        self._removals: tuple[str, ...] = ()

    @classmethod
    def of(cls, type_name: str, *parts: SelectionItem) -> Selection:
        """Make a selection that applies only to values of one type.

        Use it on a field that returns an interface or a union, to choose
        fields that exist on one implementation only. It becomes an inline
        fragment, `... on TypeName { ... }`. The type name is checked against
        the schema when the query is built. An unknown name, or a type that the
        field cannot return, raises an error with a suggestion.

        Args:
            type_name: The name of the object type.
            *parts: The fields to select for that type. With none, the client
                chooses the fields of the type, as `AUTO` does.

        Returns:
            A new selection.

        Raises:
            SchemaError: When `type_name` is not a type of the schema, or is
                not a possible type of the field. This error is raised when the
                query is built, not when this method is called.

        Examples:
            ```python {.exec}
            from pytest_graphql import Selection

            fields = ["id", Selection.of("User", "name")]
            node = gql.query("node", id="u1", fields=fields)
            assert node.name == "Ada Lovelace"
            ```
        """
        inner: SelectionInput = Selection(*parts) if parts else AUTO
        return cls(InlineFragment(on=type_name, fields=inner))

    @classmethod
    def _make(
        cls, parts: tuple[SelectionItem, ...], removals: tuple[str, ...]
    ) -> Selection:
        made = cls(*parts)
        made._removals = removals
        return made

    @property
    def parts(self) -> tuple[SelectionItem, ...]:
        """The items this selection was built from, as they were given.

        Examples:
            ```python {.exec}
            from pytest_graphql import Selection

            assert Selection("id", "name").parts == ("id", "name")
            ```
        """
        return self._parts

    @property
    def removals(self) -> tuple[str, ...]:
        """The response keys or paths that `-` removed from this selection.

        Examples:
            ```python {.exec}
            from pytest_graphql import Selection

            assert (Selection("id", "name") - "id").removals == ("id",)
            assert Selection("id", "name").removals == ()
            ```
        """
        return self._removals

    def __add__(self, other: Selection) -> Selection:
        """Join two selections into one that has the fields of both.

        Two fields that share a response key must be the same request. If they
        have different arguments or aliases, building the query raises an error.
        A removal made with `-` on one side stays on that side.

        Args:
            other: The selection to add.

        Returns:
            A new selection.

        Examples:
            ```python {.exec}
            from pytest_graphql import Selection

            both = Selection("id") + Selection("name")
            assert gql.query("user", id="u1", fields=both).name == "Ada Lovelace"
            ```
        """
        if not isinstance(other, Selection):
            return NotImplemented
        return Selection(self, other)

    def __sub__(self, other: str) -> Selection:
        """Remove a field from this selection.

        Name a response key, such as `"id"`, or a dotted path to a nested field,
        such as `"team.id"`. The removal applies to everything in this
        selection. Removing a field that is not there raises an error when the
        query is built, so a renamed field cannot silently stop being removed.

        Args:
            other: The response key or dotted path to remove.

        Returns:
            A new selection without that field.

        Examples:
            ```python {.exec}
            from pytest_graphql import Selection

            brief = Selection("id", "name", {"team": ["id", "name"]})
            without_ids = brief - "id" - "team.id"
            user = gql.query("user", id="u1", fields=without_ids)
            assert "id" not in user
            assert "id" not in user.team
            assert user.team.name == "Core"
            ```
        """
        if not isinstance(other, str):
            return NotImplemented
        return Selection._make((self,), (other,))

    def __repr__(self) -> str:
        parts = ", ".join(repr(part) for part in self._parts)
        if not self._removals:
            return f"Selection({parts})"
        removals = ", ".join(repr(removal) for removal in self._removals)
        return f"Selection({parts}) - {removals}"
