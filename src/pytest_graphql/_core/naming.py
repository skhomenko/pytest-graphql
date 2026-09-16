"""Argument and field naming (B10). One module owns every conversion.

Resolution is always by lookup, never by regenerating a name: ``NameMap``
builds a lookup table once, from the exact schema names it is given, and
every later resolution reads that table. ``to_camel`` exists for error
messages only and is never used to resolve a name.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphql import GraphQLField

_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def to_snake(name: str) -> str:
    """Convert a schema name to its snake_case form.

    ``firstName`` -> ``first_name``, ``userID`` -> ``user_id``, ``HTTPStatus``
    -> ``http_status``.
    """
    return _BOUNDARY.sub("_", name).lower()


def to_camel(name: str) -> str:
    """Convert a snake_case name to camelCase, for error messages only."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def field_signature(name: str, definition: GraphQLField) -> str:
    """Render a field or operation as it appears in an error message (SPEC 8.3).

    ``createUser(input: CreateUserInput!): User!``. Shared by field-argument
    errors (``normalize.py``) and operation-argument errors (``operation.py``)
    so the two never drift into two spellings of the same thing.
    """
    arguments = ", ".join(
        f"{argument_name}: {argument.type}"
        for argument_name, argument in definition.args.items()
    )
    return f"{name}({arguments}): {definition.type}"


@dataclass(frozen=True)
class NameMap:
    """A lookup from both a schema name and its snake form to that same name.

    Built once per argument list, input object, or response object. A key
    matching no field resolves to ``None``; the caller decides what error
    that means in its own context. A snake key that two or more distinct
    schema names collapse onto is ambiguous: it resolves to ``None`` even
    though it is present, and ``ambiguous_names`` names every spelling that
    collided, not only the first two. Three or more legal GraphQL field names
    (for example ``userId``, ``userID`` and ``user_Id``) can share one snake
    form, so the collision set is never truncated to a pair.
    """

    exact: Mapping[str, str]
    snake: Mapping[str, str]
    collisions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build(cls, names: Iterable[str]) -> NameMap:
        exact: dict[str, str] = {}
        snake: dict[str, str] = {}
        colliding: dict[str, set[str]] = {}
        for name in names:
            exact[name] = name
            key = to_snake(name)
            existing = snake.get(key)
            if existing is None:
                snake[key] = name
            elif existing != name:
                colliding.setdefault(key, {existing}).add(name)
        collisions = {key: tuple(sorted(group)) for key, group in colliding.items()}
        return cls(exact=exact, snake=snake, collisions=collisions)

    def is_ambiguous(self, key: str) -> bool:
        return key in self.collisions and key not in self.exact

    def ambiguous_names(self, key: str) -> tuple[str, ...]:
        """Return every exact spelling that collided on ``key``, sorted."""
        return self.collisions[key]

    def get(self, key: str) -> str | None:
        """Resolve ``key`` to the exact schema name it names, or ``None``."""
        exact_hit = self.exact.get(key)
        if exact_hit is not None:
            snake_hit = self.snake.get(key)
            if snake_hit is not None and snake_hit != exact_hit:
                warnings.warn(
                    f"{key!r} is both an exact schema name and the snake_case "
                    f"form of {snake_hit!r}; the exact name wins.",
                    stacklevel=2,
                )
            return exact_hit
        if self.is_ambiguous(key):
            return None
        return self.snake.get(key)
