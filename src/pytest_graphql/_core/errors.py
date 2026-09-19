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

``GraphQLTransportError`` and its leaves, plus ``GraphQLRequestError``, carry
the real shape M5a's transport layer raises (C3, C13). The leaves below
``GraphQLExecutionError`` are still left as plain markers: their constructors
belong to M5c, some of it transcribed verbatim from published text, so this
module must not guess at a shape that text will also define. ``WaitTimeoutError``
and ``ExpectedErrorNotRaised`` are M8's. Defining every leaf here only fixes
its place in the hierarchy ahead of the milestone that gives it a body.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Deferred to break the import cycle: diagnostics.py imports
    # DiagnosticRenderError from this module, so this module cannot import
    # diagnostics.py back at runtime. Every transport and request exception
    # below only needs the name for its ``request`` parameter's type.
    from pytest_graphql._core.diagnostics import DiagnosticSnapshot


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

    @classmethod
    def _from_message(
        cls, message: str, *, kind: str, operation_name: str, bad_name: str
    ) -> ArgumentError:
        """Build an instance that carries a message this class did not render.

        The "no such argument" shape above is the only one ``_render`` knows,
        because it is the only one SPEC 8.3 gives an exact example for.
        ``missing_argument`` and ``invalid_value`` below are real
        ``ArgumentError``s (SPEC 8.2 routes both there) with their own
        message shapes, so they bypass ``__init__``'s required "no such
        argument" fields rather than stretching that constructor, or this
        message format, to cover a case it was not written for.
        """
        error = cls.__new__(cls)
        error.kind = kind
        error.operation_name = operation_name
        error.bad_name = bad_name
        error.signature = ""
        error.candidates = ()
        error.input_type_name = None
        error.input_fields = None
        GraphQLClientError.__init__(error, message)
        return error

    @classmethod
    def missing_argument(
        cls, *, kind: str, operation_name: str, arg_name: str, signature: str
    ) -> ArgumentError:
        """SPEC 8.2: a required argument the caller never supplied."""
        message = (
            f"{kind} {operation_name!r} is missing required argument {arg_name!r}.\n"
            f"  Signature: {signature}"
        )
        error = cls._from_message(
            message, kind=kind, operation_name=operation_name, bad_name=arg_name
        )
        error.signature = signature
        return error

    @classmethod
    def reserved_name_collision(
        cls, *, kind: str, operation_name: str, arg_name: str, signature: str
    ) -> ArgumentError:
        """B23: a required argument only a reserved per-call option can name.

        Every keyword in the per-call option table is always read as that
        option, so a schema argument sharing its name can never be reached
        through a plain keyword. ``variables={...}`` is the one route left.
        """
        message = (
            f"{kind} {operation_name!r} has a required argument {arg_name!r} "
            "that collides with a reserved per-call option name.\n"
            f"  Pass it through variables={{{arg_name!r}: ...}} instead.\n"
            f"  Signature: {signature}"
        )
        error = cls._from_message(
            message, kind=kind, operation_name=operation_name, bad_name=arg_name
        )
        error.signature = signature
        return error

    @classmethod
    def duplicate_argument(
        cls,
        *,
        kind: str,
        operation_name: str,
        resolved_name: str,
        first_key: str,
        second_key: str,
        signature: str,
    ) -> ArgumentError:
        """Two different supplied keys resolved to the same schema argument.

        ``first_key`` and ``second_key`` are the exact and snake spelling of
        one argument, or an explicit ``variables={...}`` entry alongside a
        plain keyword for the same name: whichever pair a caller actually
        supplied together. Silently keeping one and dropping the other would
        be a value the caller wrote and never sees again.
        """
        message = (
            f"{kind} {operation_name!r} received argument {resolved_name!r} twice, "
            f"as both {first_key!r} and {second_key!r}.\n"
            f"  Signature: {signature}"
        )
        error = cls._from_message(
            message, kind=kind, operation_name=operation_name, bad_name=resolved_name
        )
        error.signature = signature
        return error

    @classmethod
    def invalid_value(
        cls, *, kind: str, operation_name: str, arg_name: str, detail: str
    ) -> ArgumentError:
        """B14: a supplied value fails to coerce against its declared type."""
        message = (
            f"{kind} {operation_name!r} received an invalid value for argument "
            f"{arg_name!r}.\n"
            f"  {detail}"
        )
        return cls._from_message(
            message, kind=kind, operation_name=operation_name, bad_name=arg_name
        )


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

    @classmethod
    def on_leaf_type(cls, *, field_name: str, type_name: str) -> SelectionError:
        """An explicit selection was given for a field with no fields to select."""
        return cls(
            f"{field_name!r} returns {type_name}, a scalar or enum, which has "
            "no fields to select.\n"
            "  Leave fields unset, or pass fields=AUTO."
        )

    @classmethod
    def no_selectable_fields(cls, type_name: str) -> SelectionError:
        """SPEC 5.4 rule 9: an empty selection set is not a valid document."""
        return cls(
            f"auto-selection of {type_name!r} produced no fields.\n"
            "  Every field was removed by the policy, needs an argument, or "
            "sits past the depth limit.\n"
            "  Widen it by one of:\n"
            "    max_depth=4\n"
            "    include_deprecated=True\n"
            "    exclude=[]\n"
            '    fields=["id"]'
        )


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


class DiagnosticRenderError(GraphQLClientError):
    """``RequestInfo.__repr__``/``as_curl()`` found no safe rendering
    (DESIGN_DECISIONS.md section 7, "Boundary").

    Raised instead of returning text that would expose a redacted value, or a
    corrupted ``as_curl()`` request body, when a qualifying secret collides
    with fixed or generated rendering syntax that no per-leaf substitution
    can remove: syntax a format cannot omit (a class name, a shell
    delimiter) cannot be replaced, and an already-valid JSON document (the
    ``--data`` body) cannot be rewritten without breaking its structure.

    Neither constructor argument is composed here: the fixed, descriptive
    prose in :meth:`default_message` and the fixed ``renderer`` label are both
    compile-time text, and a qualifying secret can equal a substring of
    either one exactly as it can equal a renderer's own wrapper syntax
    (DESIGN_DECISIONS.md section 7, "Value scrub for free-form text"). The
    caller that already holds the qualifying set validates both ``renderer``
    and the composed ``message`` against it first and passes an empty string
    in place of whichever one collides, so this constructor never needs --
    and is never trusted -- to repeat either check itself.
    """

    def __init__(self, renderer: str, message: str) -> None:
        self.renderer = renderer
        super().__init__(message)

    @staticmethod
    def default_message(renderer: str) -> str:
        """The descriptive message a caller uses when it is safe to.

        A ``@staticmethod`` rather than inline construction so the one call
        site that raises this exception can validate this exact text against
        the qualifying set *before* deciding whether to pass it or the empty
        fallback to ``__init__``.
        """
        return (
            f"{renderer} could not produce a safe representation of this "
            "request: a redacted value collides with fixed or generated "
            "rendering syntax.\n"
            "  Use a different value for the colliding secret, or call "
            "redacted() and inspect the snapshot's fields directly instead."
        )


class GraphQLTransportError(GraphQLTestError):
    """Raised by the network layer (C3, C13).

    Every instance carries ``request``, the redacted ``DiagnosticSnapshot``
    of the request that failed, never the live ``RequestInfo``
    (DESIGN_DECISIONS.md section 7, "Boundary"). ``body_excerpt`` is the
    capped, already scrubbed-and-escaped response text C3 requires for an
    unparsable response; it is empty for a failure that never received a
    body, such as a connection or timeout error.
    """

    def __init__(
        self,
        message: str,
        *,
        request: DiagnosticSnapshot,
        body_excerpt: str = "",
    ) -> None:
        self.request = request
        self.body_excerpt = body_excerpt
        super().__init__(message)


class GraphQLConnectionError(GraphQLTransportError):
    """The connection could not be established, after every retry attempt."""


class GraphQLTimeoutError(GraphQLTransportError):
    """A read or write timeout. Never retried (SPEC 5.6)."""


class GraphQLHTTPStatusError(GraphQLTransportError):
    """A non-2xx response carrying no valid GraphQL envelope (C3)."""

    def __init__(
        self,
        message: str,
        *,
        request: DiagnosticSnapshot,
        status_code: int,
        body_excerpt: str = "",
    ) -> None:
        self.status_code = status_code
        super().__init__(message, request=request, body_excerpt=body_excerpt)


class GraphQLRequestError(GraphQLTestError):
    """The server rejected the request before execution (C3).

    A valid GraphQL envelope with no ``data`` entry at all, at any status,
    means the server never started executing the request. This is distinct
    from ``GraphQLExecutionError``, which means execution ran: an envelope
    carrying a ``data`` entry, even ``null``, went through execution before
    it failed. ``errors`` holds the envelope's structured error objects,
    already scrubbed and escaped; ``request`` is the redacted snapshot, per
    the same boundary rule every transport exception follows.
    """

    def __init__(
        self,
        message: str,
        *,
        request: DiagnosticSnapshot,
        status_code: int,
        media_type: str,
        errors: tuple[Mapping[str, Any], ...],
    ) -> None:
        self.request = request
        self.status_code = status_code
        self.media_type = media_type
        self.errors = errors
        super().__init__(message)


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
