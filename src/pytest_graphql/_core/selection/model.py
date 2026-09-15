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
    """One field in a selection, with arguments, an alias or a type condition.

    ``args`` holds Python values. They are never written into document text:
    each one is hoisted into a generated variable during normalization, which
    is what keeps a user value out of the document (C7).

    ``fields`` distinguishes two things that look alike. ``None`` means the
    user listed no sub-selection, which is an error for a field that needs
    one, because an explicit selection adds nothing on its own (B12).
    ``AUTO`` asks for the generated selection of that field's type.
    """

    name: str
    _: KW_ONLY
    args: Mapping[str, Any] | None = None
    alias: str | None = None
    fields: SelectionInput | None = None
    on: str | None = None
    directives: Mapping[str, Mapping[str, Any]] | None = field(default=None)

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
        """The key this field occupies in its selection set and in the data."""
        return self.alias or self.name


@dataclass(frozen=True)
class InlineFragment:
    """``... on TypeName { ... }``. Produced by ``Selection.of``."""

    on: str
    fields: SelectionInput = AUTO


class Selection:
    """A reusable, named bundle of any selection input form.

    Defining a selection once is the intended answer to schema churn, so a
    ``Selection`` is immutable and every operator returns a new one.
    """

    __slots__ = ("_parts", "_removals")

    def __init__(self, *parts: SelectionItem) -> None:
        self._parts: tuple[SelectionItem, ...] = parts
        self._removals: tuple[str, ...] = ()

    @classmethod
    def of(cls, type_name: str, *parts: SelectionItem) -> Selection:
        """A selection that applies only to ``type_name``.

        The name is resolved against the schema when the document is built:
        an unknown name, or one that is not a possible type of the parent,
        raises there. With no parts the fragment takes the generated
        selection for that type, because an inline fragment cannot be empty.
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
        return self._parts

    @property
    def removals(self) -> tuple[str, ...]:
        return self._removals

    def __add__(self, other: Selection) -> Selection:
        """Union two selections. Nesting keeps each operand's removals its own."""
        if not isinstance(other, Selection):
            return NotImplemented
        return Selection(self, other)

    def __sub__(self, other: str) -> Selection:
        """Remove a response key, or a dotted path, from this selection.

        The removal is recorded against this whole selection, so the operand
        is nested rather than rewritten. Removing something that is not there
        raises when the document is built, so a renamed field cannot silently
        stop being removed.
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
