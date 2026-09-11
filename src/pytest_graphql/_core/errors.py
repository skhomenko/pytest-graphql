"""The public exception hierarchy (SPEC 8.1) and its message quality (SPEC 8.3).

``GraphQLTestError`` is the root every other exception in this package
descends from. SPEC 8.1's hierarchy diagram calls this class ``GraphQLError``;
`docs/reference/DESIGN_DECISIONS.md` section 1 renames the root to
``GraphQLTestError`` and keeps every leaf class's specification name, to avoid
a same-named, unrelated clash with ``graphql.GraphQLError`` from graphql-core,
a required dependency. Every ``GraphQLClientError`` message states three
things: what was wrong, what was expected, and what to do about it.
Suggestions come from ``difflib.get_close_matches``, the standard library
fuzzy matcher, per SPEC 8.3: no third-party fuzzy matching dependency.

Several leaf classes below the ``GraphQLTransportError`` and
``GraphQLExecutionError`` branches are left as plain markers. Their real
constructors belong to the milestone that raises them: M5a for the transport
errors, M5c for execution and partial-data errors (some of that milestone's
code is transcribed verbatim from published text, so this module must not
guess at a shape that text will also define), and M8 for ``WaitTimeoutError``
and ``ExpectedErrorNotRaised``. Defining them here only fixes their place in
the hierarchy.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence


def _best_match(name: str, candidates: Sequence[str]) -> str | None:
    matches = difflib.get_close_matches(name, candidates, n=1)
    return matches[0] if matches else None


def _and_join(words: Sequence[str]) -> str:
    """Join two or more words as English prose: "a and b", "a, b and c"."""
    ordered = sorted(words)
    if len(ordered) == 1:
        return ordered[0]
    return f"{', '.join(ordered[:-1])} and {ordered[-1]}"


def _pluralize(word: str) -> str:
    if len(word) > 1 and word.endswith("y") and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


class GraphQLTestError(Exception):
    """Base of every exception this package raises. Catch this to catch all."""


class GraphQLClientError(GraphQLTestError):
    """Raised before any network call."""


class OperationNotFoundError(GraphQLClientError):
    """No operation of the given kind has the given name."""

    def __init__(
        self, *, kind: str, name: str, available: Sequence[str], total_count: int
    ) -> None:
        self.kind = kind
        self.name = name
        self.available = tuple(available)
        self.total_count = total_count
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"no {self.kind} named {self.name!r}."]
        matches = difflib.get_close_matches(self.name, self.available)
        if matches:
            lines.append(f"  Did you mean: {', '.join(matches)}?")
        plural = _pluralize(self.kind)
        lines.append(
            f"  This schema has {self.total_count} {plural}. "
            "Run with --gql-show-schema-stats to list them."
        )
        return "\n".join(lines)


class ArgumentError(GraphQLClientError):
    """An operation has no argument, or an input object has no field, by that name."""

    def __init__(
        self,
        *,
        kind: str,
        operation_name: str,
        bad_name: str,
        signature: str,
        candidates: Sequence[str],
        input_type_name: str | None = None,
        input_fields: Sequence[str] | None = None,
    ) -> None:
        self.kind = kind
        self.operation_name = operation_name
        self.bad_name = bad_name
        self.signature = signature
        self.candidates = tuple(candidates)
        self.input_type_name = input_type_name
        self.input_fields = tuple(input_fields) if input_fields is not None else None
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [
            f"{self.kind} {self.operation_name!r} has no argument {self.bad_name!r}.",
            f"  Signature: {self.signature}",
        ]
        if self.input_type_name is not None and self.input_fields is not None:
            lines.append(
                f"  {self.input_type_name} fields: {', '.join(self.input_fields)}"
            )
        match = _best_match(self.bad_name, self.candidates)
        if match:
            lines.append(f"  Did you mean {match!r}?")
        return "\n".join(lines)


class SelectionError(GraphQLClientError):
    """An explicit selection is invalid, or the assembled document fails validation."""

    @classmethod
    def unknown_field(
        cls, type_name: str, bad_name: str, available: Sequence[str]
    ) -> SelectionError:
        lines = [f"no field {bad_name!r} on {type_name}."]
        lines.append(f"  Available fields: {', '.join(available)}")
        match = _best_match(bad_name, available)
        if match:
            lines.append(f"  Did you mean {match!r}?")
        return cls("\n".join(lines))

    @classmethod
    def from_validation(cls, message: str) -> SelectionError:
        """Wrap a graphql-core document-validation message, verbatim."""
        return cls(message)


class SelectionTooLargeError(GraphQLClientError):
    """Auto-selection produced more fields than the configured limit."""

    _SUGGESTIONS = (
        "  Reduce it by one of:\n"
        "    max_depth=2\n"
        '    per_type_depth_cap={"User": 1}\n'
        '    fields=["id", "name"]'
    )

    def __init__(self, *, type_name: str, field_count: int, limit: int) -> None:
        self.type_name = type_name
        self.field_count = field_count
        self.limit = limit
        first = (
            f"auto-selection of {type_name!r} produced {field_count:,} fields "
            f"(limit {limit})."
        )
        super().__init__(f"{first}\n{self._SUGGESTIONS}")


class SchemaError(GraphQLClientError):
    """A referenced type, or a required schema shape, does not exist."""

    @classmethod
    def unknown_type(cls, name: str, available: Sequence[str]) -> SchemaError:
        lines = [f"no type named {name!r}."]
        match = _best_match(name, available)
        if match:
            lines.append(f"  Did you mean {match!r}?")
        return cls("\n".join(lines))

    @classmethod
    def not_a_possible_type(cls, name: str, parent_name: str) -> SchemaError:
        return cls(f"{name!r} is not a possible type of {parent_name!r}.")


class ScalarNotRegisteredError(GraphQLClientError):
    """A custom scalar has no fake generator registered for it."""

    def __init__(self, scalar_name: str) -> None:
        self.scalar_name = scalar_name
        super().__init__(
            f"no fake generator registered for scalar {scalar_name!r}.\n"
            f"  Register one with ScalarRegistry.register({scalar_name!r}, "
            "fake=...) or exclude the field."
        )


class GraphQLTransportError(GraphQLTestError):
    """Raised by the network layer. Its leaves belong to M5a."""


class GraphQLConnectionError(GraphQLTransportError):
    pass


class GraphQLTimeoutError(GraphQLTransportError):
    pass


class GraphQLHTTPStatusError(GraphQLTransportError):
    pass


class GraphQLExecutionError(GraphQLTestError):
    """The server returned an ``errors`` array. Its leaves belong to M5c."""


class GraphQLPartialDataError(GraphQLExecutionError):
    pass


class GraphQLFieldError(GraphQLTestError):
    """A response field was accessed that does not exist, or is ambiguous."""

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @classmethod
    def unknown_field(
        cls, type_name: str, bad_name: str, available: Sequence[str]
    ) -> GraphQLFieldError:
        lines = [f"no field {bad_name!r} on {type_name}."]
        lines.append(f"  Available in this response: {', '.join(available)}")
        match = _best_match(bad_name, available)
        if match:
            lines.append(f"  Did you mean {match!r}?")
        lines.append(
            "  Note: the field may exist on the type but not be in this selection set."
        )
        return cls("\n".join(lines))

    @classmethod
    def ambiguous(
        cls, type_name: str, snake_key: str, exact_names: Sequence[str]
    ) -> GraphQLFieldError:
        """``exact_names`` names every colliding spelling, two or more."""
        return cls(
            f"{snake_key!r} is ambiguous on {type_name}: it matches "
            f"{_and_join(exact_names)}.\n"
            "  Use the exact field name to disambiguate."
        )


class WaitTimeoutError(GraphQLTestError):
    """A ``wait_until`` poll exhausted its deadline. Its shape belongs to M8."""


class ExpectedErrorNotRaised(GraphQLTestError):  # noqa: N818 -- name fixed by SPEC 8.1
    """An ``expect_error`` block completed without raising. Its shape belongs to M8."""
