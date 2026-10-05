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
the real shape M5a's transport layer raises (C3, C13).
``GraphQLExecutionError`` and its leaf carry the response that failed,
``ExpectedErrorNotRaised`` and ``WaitTimeoutError`` carry what M8's error
assertions and polling found. Every one of them is built from a response, so
each checks its complete message against that response's request before it is
raised (DESIGN_DECISIONS.md section 7, "Boundary").
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Deferred to break the import cycle: diagnostics.py imports
    # DiagnosticRenderError from this module, so this module cannot import
    # diagnostics.py back at runtime. Every transport and request exception
    # below only needs the name for its ``request`` parameter's type. The
    # response types import this module in turn.
    from pytest_graphql._core.diagnostics import DiagnosticSnapshot
    from pytest_graphql._core.response.envelope import GraphQLResponse
    from pytest_graphql._core.response.types import GraphQLErrorInfo


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

    #: Set only when even an empty message would let the ``repr`` show a
    #: request's secret (``_check_exception_text`` in diagnostics.py).
    _shown_repr: str | None = None

    def __repr__(self) -> str:
        # The message is the library's finished, already scrubbed rendering
        # (DESIGN_DECISIONS.md section 7). The default ``repr`` re-escapes
        # it, doubling every backslash and quote after the last scrub ran,
        # and that can spell a credential the scrub had removed. So the
        # message is shown as it is, never transformed again. An exception
        # built from a request checked this complete text, and its other
        # standard renderings, before it was raised.
        if self._shown_repr is not None:
            return self._shown_repr
        return f"{type(self).__name__}({self})"


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

    @classmethod
    def not_a_query(cls, *, kind: str, name: str) -> ArgumentError:
        """``wait_until`` was given an operation that is not a query.

        A poll repeats its call, so a mutation would repeat its side effect
        on every attempt. The schema decided ``kind``, never the name.
        """
        message = (
            f"wait_until polls queries only, and {name!r} is a {kind}.\n"
            "  Polling would repeat its side effect on every attempt.\n"
            f"  Poll a query that reads the result, or call {kind}({name!r}) once."
        )
        return cls._from_message(message, kind=kind, operation_name=name, bad_name=name)

    @classmethod
    def unknown_option(
        cls, *, callable_name: str, bad_name: str, candidates: Sequence[str]
    ) -> ArgumentError:
        """A configuration keyword a public entry point does not accept.

        The call grammar routes a name the caller wrote and the library does
        not know to this class, whether the name was meant as a schema
        argument or as a configuration option. Accepting it silently would
        leave a client running on the default the caller believed they had
        replaced.
        """
        names = ", ".join(sorted(candidates))
        message = (
            f"{callable_name} has no configuration option {bad_name!r}.\n"
            f"  Options: {names}"
        )
        match = _best_match(bad_name, tuple(candidates))
        if match:
            message += f"\n  Did you mean {match!r}?"
        error = cls._from_message(
            message, kind="call", operation_name=callable_name, bad_name=bad_name
        )
        error.candidates = tuple(candidates)
        return error


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
    def ambiguous_field(
        cls, type_name: str, bad_name: str, spellings: Sequence[str]
    ) -> SelectionError:
        """A snake spelling that two or more schema fields share."""
        return cls(
            f"{bad_name!r} is ambiguous on {type_name}: it matches "
            f"{', '.join(spellings)}.\n"
            "  Use the exact field name."
        )

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
    """`gql.fake` needed a value for a custom scalar that has no generator.

    The factory fills a field of a custom scalar type by calling the `fake`
    function of that scalar's `ScalarSpec`. This error means no spec is
    registered for the scalar. The message gives the code that registers one,
    and names the field to override instead. A field you give a value for in
    the call, or one that the factory leaves out, never raises it.

    Args:
        scalar_name: The name of the scalar.
        path: Where the factory needed the value: the input type, then the
            field names, with a list index as an integer. Leave it out when the
            place is not known.

    Examples:
        ```python {.exec}
        from graphql import build_schema

        from pytest_graphql import ScalarNotRegisteredError, build_client

        schema = build_schema(
            "scalar Money type Query { ping: Boolean } input Pay { amount: Money! }"
        )
        client = build_client(
            url="http://localhost:8000/graphql",
            transport=gql.transport,
            schema=schema,
        )
        try:
            client.fake.Pay()
        except ScalarNotRegisteredError as error:
            assert error.scalar_name == "Money"
            assert error.location == "Pay.amount"
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(self, scalar_name: str, *, path: Sequence[str | int] = ()) -> None:
        self.scalar_name = scalar_name
        """The name of the scalar that has no generator.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarNotRegisteredError

            error = ScalarNotRegisteredError("Money", path=("Pay", "lines", 0, "price"))
            assert error.scalar_name == "Money"
            ```
        """
        self.path = tuple(path)
        """Where the factory needed the value, as names and list indexes.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarNotRegisteredError

            error = ScalarNotRegisteredError("Money", path=("Pay", "lines", 0, "price"))
            assert error.path == ("Pay", "lines", 0, "price")
            ```
        """
        self.location = _render_path(self.path) if self.path else None
        """The path as text, such as `Pay.amount` or `Pay.lines[0].price`, or `None`.

        Examples:
            ```python {.exec}
            from pytest_graphql import ScalarNotRegisteredError

            error = ScalarNotRegisteredError("Money", path=("Pay", "lines", 0, "price"))
            assert error.location == "Pay.lines[0].price"
            assert ScalarNotRegisteredError("Money").location is None
            ```
        """
        spec = (
            f'    registry.register(ScalarSpec(name="{scalar_name}", '
            "serialize=str, fake=lambda rng: ...))"
        )
        if self.location is None:
            lines = [
                "no fake generator is registered for the custom scalar "
                f"{scalar_name!r}.",
                "  A ScalarSpec for it must provide a fake generator. Register one:",
                spec,
                "  Or give the field a value in the call, which skips generation.",
            ]
        else:
            lines = [
                "no fake generator is registered for the custom scalar "
                f"{scalar_name!r}, needed at {self.location}.",
                "  A ScalarSpec for it must provide a fake generator. Register one:",
                spec,
                "  Or give the field a value, which skips generation:",
                f"    gql.fake.{self.path[0]}({self.path[1]}=...)"
                if len(self.path) > 1
                else f"    gql.fake.{self.path[0]}(...)",
            ]
        super().__init__("\n".join(lines))


def _render_path(path: Sequence[str | int]) -> str:
    """``Type.field[0].sub``: names joined by dots, an index in brackets."""
    text = ""
    for segment in path:
        text += (
            f"[{segment}]"
            if isinstance(segment, int)
            else (f".{segment}" if text else segment)
        )
    return text


class DiagnosticRenderError(GraphQLClientError):
    """A request cannot be shown as text without exposing a secret.

    `repr()` and `as_curl()` of a `RequestInfo` replace secret values with
    markers. They raise this error when a secret is equal to text that the
    output must contain, such as a class name or a field name, so that no
    replacement could remove the secret without breaking the output. The error
    is raised instead of returning text that shows the secret.

    This is rare, because a real secret is long and random. The fix is in the
    message: use a different value for the colliding secret, or call
    `redacted()` on the request and read the fields of the snapshot.

    Args:
        renderer: The name of the output that was refused, such as `"repr()"`.
        message: The error text. `default_message()` builds the standard one.

    Examples:
        ```python {.exec}
        from pytest_graphql import DiagnosticRenderError, RequestInfo

        request = RequestInfo(
            operation=None,
            kind="query",
            document="{ ping }",
            variables={},
            headers={"Authorization": "RequestInfo"},
            url="http://localhost:8000/graphql",
        )
        try:
            repr(request)
        except DiagnosticRenderError as error:
            assert error.renderer == "repr()"
            assert request.redacted().kind == "query"
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(self, renderer: str, message: str) -> None:
        self.renderer = renderer
        """The name of the output that was refused, such as `"repr()"`.

        Examples:
            ```python {.exec}
            from pytest_graphql import DiagnosticRenderError

            error = DiagnosticRenderError("as_curl()", "not safe to show")
            assert error.renderer == "as_curl()"
            ```
        """
        super().__init__(message)

    @staticmethod
    def default_message(renderer: str) -> str:
        """Return the standard message for a refused output.

        Args:
            renderer: The name of the output that was refused.

        Returns:
            The message, which says what happened and what to do about it.

        Examples:
            ```python {.exec}
            from pytest_graphql import DiagnosticRenderError

            text = DiagnosticRenderError.default_message("repr()")
            assert text.startswith("repr() could not produce a safe representation")
            ```
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
        request._guard.check_exception(self)


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
        request._guard.check_exception(self)


class GraphQLExecutionError(GraphQLTestError):
    """The server answered with errors and no usable data.

    `query()` and `mutation()` raise this when the response has an `errors`
    list and no data, for example when a required field fails, so that `data`
    is `null`. It is also raised for a server that breaks the protocol, by
    sending neither errors nor data.

    The message holds the response's `repr()`: the status, the number of errors
    and the redacted request. It never holds a server value, so a secret that a
    server echoes back does not enter your test report. Read the errors from
    `errors`.

    Set `raise_on_error=False` on the client or on a call, and use `raw=True`,
    to get the response back instead of this error.

    Args:
        summary: The first line of the message.
        response: The response that failed. The library always passes it.
            Without it, `response` is `None` and `errors` is empty.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLExecutionError

        try:
            gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
        except GraphQLExecutionError as error:
            assert error.response.data_state == "null"
            assert error.errors[0].path == ("updateUser",)
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(
        self, summary: str, *, response: GraphQLResponse[Any] | None = None
    ) -> None:
        self.response = response
        """The response that failed, or `None` when the error was built without one.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLExecutionError

            try:
                gql.mutation("updateUser", id="missing", fields=["id"])
            except GraphQLExecutionError as error:
                assert error.response.data_state == "null"
                assert error.response.errors == error.errors
            else:
                raise AssertionError("expected an error")
            ```
        """
        if response is None:
            super().__init__(summary)
            return
        super().__init__(f"{summary}\n  {_shown(response)}")
        response.request._guard.check_exception(self)

    @property
    def errors(self) -> tuple[GraphQLErrorInfo, ...]:
        """The errors the server returned, from `response`.

        Each entry has `message`, `path`, `locations`, `extensions` and `code`.
        The tuple is empty when the error has no response.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLExecutionError

            try:
                gql.mutation("updateUser", id="missing", name="Ada", fields=["id"])
            except GraphQLExecutionError as error:
                assert len(error.errors) == 1
                assert error.errors[0] is error.response.errors[0]
            ```
        """
        return () if self.response is None else self.response.errors


class GraphQLPartialDataError(GraphQLExecutionError):
    """The server returned data and errors together.

    GraphQL allows a field to fail while the rest of the operation succeeds. The
    response then has both `data` and `errors`. By default the client raises this
    error for it, so that an error is never ignored by accident. It is a
    `GraphQLExecutionError`, so one `except` clause covers both.

    The data is still available in `response.data`. To accept partial data,
    set `raise_on_partial=False` on the client or on a call. To accept every
    error, set `raise_on_error=False`.

    Examples:
        ```python {.exec}
        from dataclasses import replace

        from pytest_graphql import GraphQLPartialDataError, build_client


        class FailsOneField:
            # Adds an error to every response of another transport.

            def __init__(self, inner):
                self.inner = inner

            def send(self, request, *, timeout):
                raw = self.inner.send(request, timeout=timeout)
                error = {"message": "no access", "path": ["user", "balance"]}
                return replace(raw, errors=(error,))

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=FailsOneField(gql.transport),
            schema=gql.schema,
        )
        try:
            client.query("user", id="u1", fields=["id", "balance"])
        except GraphQLPartialDataError as error:
            assert error.response.has_data
            assert error.response.data.user.id == "u1"
        else:
            raise AssertionError("expected an error")
        ```
    """


class GraphQLFieldError(GraphQLTestError, AttributeError, KeyError):
    """You read a field that the response does not have, or a name that is unclear.

    A `Node` raises this when you read a field that is not in the response. The
    message lists the fields that are there, suggests the closest name, and
    reminds you that the field may be in the schema but missing from your
    selection. It is also raised for a snake_case name that matches two
    different fields. Use the exact field name then.

    It is both an `AttributeError` and a `KeyError`. That keeps `hasattr()`,
    `getattr(node, "x", default)` and `node.get("x")` working in the usual
    way.

    Args:
        message: The error text.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLFieldError

        user = gql.query("user", id="u1", fields=["id"])
        try:
            user.name
        except GraphQLFieldError as error:
            assert isinstance(error, AttributeError)
            assert isinstance(error, KeyError)
        else:
            raise AssertionError("expected an error")
        assert hasattr(user, "name") is False
        ```
    """

    __str__ = Exception.__str__

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @classmethod
    def unknown_field(
        cls, type_name: str, bad_name: str, available: Sequence[str]
    ) -> GraphQLFieldError:
        """Build the error for a field that is not in the response.

        The library calls this when you read a missing field. You rarely need
        it yourself.

        Args:
            type_name: The name of the type that was read.
            bad_name: The name that was read.
            available: The field names the response has. The message lists them and
                suggests the closest one.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLFieldError

            error = GraphQLFieldError.unknown_field("User", "nme", ["id", "name"])
            assert "Did you mean 'name'?" in str(error)
            ```
        """
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
        """Build the error for a snake_case name that matches two fields.

        The library calls this when you read such a name. You rarely need it
        yourself.

        Args:
            type_name: The name of the type that was read.
            snake_key: The snake_case name that was read.
            exact_names: Every exact field name that has this snake_case form. Give
                two or more.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLFieldError

            error = GraphQLFieldError.ambiguous("User", "user_id", ["userId", "UserId"])
            assert "ambiguous" in str(error)
            ```
        """
        return cls(
            f"{snake_key!r} is ambiguous on {type_name}: it matches "
            f"{_and_join(exact_names)}.\n"
            "  Use the exact field name to disambiguate."
        )


class ResponseShapeError(GraphQLTestError):
    """The server sent a value that does not fit the type in the schema.

    The client checks every response against the schema. This error means that
    a value has the wrong shape, for example a number where the schema says
    `String`, a list where it says an object, or a `__typename` that is not a
    possible type. The server is broken or the schema is out of date.

    The message names the place in the response and the types. It never repeats
    the server's value.

    Args:
        message: The error text.
        path: Where in the response the value is: field names, with a list index
            as an integer.

    Examples:
        ```python {.exec}
        from dataclasses import replace

        from pytest_graphql import ResponseShapeError, build_client


        class NumberForName:
            # Returns a number where the schema promises a string.

            def __init__(self, inner):
                self.inner = inner

            def send(self, request, *, timeout):
                raw = self.inner.send(request, timeout=timeout)
                return replace(raw, data={"user": {"id": "u1", "name": 123}})

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=NumberForName(gql.transport),
            schema=gql.schema,
        )
        try:
            client.query("user", id="u1", fields=["id", "name"])
        except ResponseShapeError as error:
            assert error.path == ("user", "name")
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(self, message: str, *, path: tuple[str | int, ...]) -> None:
        self.path = path
        """Where in the response the bad value is, as names and list indexes.

        Examples:
            ```python {.exec}
            from pytest_graphql import ResponseShapeError

            error = ResponseShapeError("bad value", path=("user", "friends", 0, "name"))
            assert error.path == ("user", "friends", 0, "name")
            ```
        """
        super().__init__(message)


def _shown(value: object) -> str:
    """``repr(value)``, or the withheld notice when the renderer refused.

    The error that carries the text must still exist, so a refusal never
    replaces it (``GraphQLExecutionError``).
    """
    from pytest_graphql._core.diagnostics import WITHHELD_TEXT

    try:
        return repr(value)
    except DiagnosticRenderError:
        return WITHHELD_TEXT


def _guarded_line(
    snapshots: Sequence[DiagnosticSnapshot], line: str, fallback: str
) -> str:
    """``line``, or ``fallback`` when it or its ``repr`` shows a secret.

    One line is replaced rather than the whole message, so a single hostile
    entry cannot hide the rest of a failure report. The finished message is
    still checked as a whole by ``check_exception``, which stays the backstop.
    """
    for snapshot in snapshots:
        if snapshot._guard.contains(line) or snapshot._guard.contains(repr(line)):
            return fallback
    return line


def _indented(text: str, prefix: str) -> str:
    """``text`` with every continuation line pushed under ``prefix``.

    A server can put a newline in a message, and a continuation that starts
    at the list margin would read as another entry of the report.
    """
    return text.replace("\n", "\n" + " " * len(prefix))


def _check_against(
    error: GraphQLTestError, snapshots: Sequence[DiagnosticSnapshot]
) -> None:
    for snapshot in snapshots:
        snapshot._guard.check_exception(error)


class ExpectedErrorNotRaised(GraphQLTestError):  # noqa: N818 -- name fixed by SPEC 8.1
    """An `expect_error` block did not end in the error that it expected.

    `GraphQLClient.expect_error()` checks that the code in its `with` block
    fails with a GraphQL error. This error says that the check failed, in one of
    two ways:

    - The block ended without a `GraphQLExecutionError`. `errors` and
      `unmatched` are empty. A client that does not raise, because of
      `raise_on_error=False` or `raise_on_partial=False`, never satisfies the
      block.
    - The block raised one, but a filter matched none of its errors. `unmatched`
      names the filters that failed, and `errors` holds every error the server
      returned.

    The message lists the errors that the server returned, up to the limit set by
    `max_recorded_errors`, and says how many it left out.

    Args:
        message: The error text.
        response: The last response that the block received, or the failed one.
            `None` when the block made no GraphQL call.
        errors: Every error the server returned. Empty when no error was raised.
        unmatched: The names of the filters that matched no error.
        calls: How many responses the block received.

    Examples:
        ```python {.exec}
        from pytest_graphql import ExpectedErrorNotRaised

        try:
            with gql.expect_error(code="FORBIDDEN"):
                gql.query("user", id="u1")
        except ExpectedErrorNotRaised as error:
            assert error.calls == 1
            assert error.errors == ()
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(
        self,
        message: str,
        *,
        response: GraphQLResponse[Any] | None,
        errors: Sequence[GraphQLErrorInfo] = (),
        unmatched: Sequence[str] = (),
        calls: int = 0,
    ) -> None:
        self.response = response
        """The last response the block received, or `None` when it made no call.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            try:
                with gql.expect_error(code="FORBIDDEN"):
                    gql.mutation("updateUser", id="missing", fields=["id"])
            except ExpectedErrorNotRaised as error:
                assert error.response.data_state == "null"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.errors = tuple(errors)
        """Every error the server returned. Empty when the block did not fail.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            try:
                with gql.expect_error(code="FORBIDDEN"):
                    gql.mutation("updateUser", id="missing", fields=["id"])
            except ExpectedErrorNotRaised as error:
                assert len(error.errors) == 1
                assert error.errors[0].path == ("updateUser",)
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.unmatched = tuple(unmatched)
        """The names of the filters that matched no error, such as `"code"`.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            try:
                with gql.expect_error(code="FORBIDDEN"):
                    gql.mutation("updateUser", id="missing", fields=["id"])
            except ExpectedErrorNotRaised as error:
                assert error.unmatched == ("code",)
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.calls = calls
        """How many responses the block received.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            try:
                with gql.expect_error(code="FORBIDDEN"):
                    gql.mutation("updateUser", id="missing", fields=["id"])
            except ExpectedErrorNotRaised as error:
                assert error.calls == 1
            else:
                raise AssertionError("expected an error")
            ```
        """
        super().__init__(message)
        if response is not None:
            response.request._guard.check_exception(self)

    @classmethod
    def no_error(
        cls, *, response: GraphQLResponse[Any] | None, calls: int
    ) -> ExpectedErrorNotRaised:
        """Build the error for a block that ended without an execution error.

        The library calls this from `expect_error()`. You rarely need it yourself.

        Args:
            response: The last response the block received, or `None`.
            calls: How many responses the block received.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            error = ExpectedErrorNotRaised.no_error(response=None, calls=0)
            assert error.calls == 0
            assert "The block made no GraphQL call." in str(error)
            ```
        """
        lines = [
            "expect_error: the block did not raise GraphQLExecutionError.",
            "  Expected: a response with errors that the client raises.",
        ]
        if response is None:
            lines.append("  The block made no GraphQL call.")
        else:
            lines.append(
                f"  Last response (of {calls} call(s) in the block): {_shown(response)}"
            )
            if response.errors:
                lines.append(
                    f"  The last response carried {len(response.errors)} error(s) "
                    "and the client did not raise: raise_on_error or "
                    "raise_on_partial let it through."
                )
        lines += [
            "  Fix: make the call fail the way the test expects, or remove "
            "expect_error.",
            "  A call with raise_on_error=False never raises, so read "
            "response.errors instead.",
        ]
        return cls("\n".join(lines), response=response, calls=calls)

    @classmethod
    def unmatched_filters(
        cls,
        *,
        response: GraphQLResponse[Any],
        filters: Sequence[tuple[str, str]],
        calls: int,
    ) -> ExpectedErrorNotRaised:
        """Build the error for a raised error that a filter did not match.

        The library calls this from `expect_error()`. You rarely need it yourself.

        Args:
            response: The response that carried the errors.
            filters: Each filter that failed, as its name and the text of the value
                you wrote. The text is checked against the request's secrets, like
                every other line of the message.
            calls: How many responses the block received.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ExpectedErrorNotRaised

            response = gql.mutation(
                "updateUser",
                id="missing",
                fields=["id"],
                raise_on_error=False,
                raw=True,
            )
            error = ExpectedErrorNotRaised.unmatched_filters(
                response=response, filters=[("code", "'FORBIDDEN'")], calls=1
            )
            assert error.unmatched == ("code",)
            assert len(error.errors) == 1
            ```
        """
        from pytest_graphql._core.diagnostics import WITHHELD_TEXT

        snapshots = (response.request,)
        errors = response.errors
        shown = [(i, e._summary) for i, e in enumerate(errors, start=1) if e._summary]
        listed = ", ".join(f"{name}={text}" for name, text in filters)
        lines = [
            "expect_error: a filter matched none of the errors the server returned.",
            _guarded_line(
                snapshots,
                f"  Unmatched filters: {listed}",
                f"  Unmatched filters: {WITHHELD_TEXT}",
            ),
            f"  The server returned {len(errors)} error(s):",
        ]
        for index, summary in shown:
            prefix = f"    [{index}] "
            lines.append(
                _guarded_line(
                    snapshots,
                    prefix + _indented(summary, prefix),
                    prefix + WITHHELD_TEXT,
                )
            )
        if len(shown) < len(errors):
            lines.append(
                f"    ... and {len(errors) - len(shown)} more not shown "
                f"(max_recorded_errors={len(shown)})."
            )
        lines.append(
            "  Fix: change the filters to match one of these errors, or fix the "
            "operation that returned them."
        )
        return cls(
            "\n".join(lines),
            response=response,
            errors=errors,
            unmatched=[name for name, _ in filters],
            calls=calls,
        )


class WaitTimeoutError(GraphQLTestError):
    """A `wait_until` poll reached its deadline before `until` held.

    `GraphQLClient.wait_until()` repeats a query until a condition holds. This
    error says that time ran out. Its attributes tell you what the last attempts
    saw: how many calls were made, how long the poll ran, the last response, and
    the last exception that `ignore` swallowed.

    The message shows the text of the last swallowed exception only when it can
    check that text against the request of the attempt that raised it. Without
    that request, the text is withheld to protect secrets. The exception is
    still available as `last_exception`.

    Args:
        operation: The name of the query that was polled.
        attempts: How many calls were made.
        elapsed: The seconds from the first attempt until the poll stopped.
        timeout: The limit that the poll was given, in seconds.
        last_response: The most recent response that any attempt received, or
            `None` when no attempt got one.
        last_exception: The most recent exception that `ignore` swallowed, or
            `None`.
        last_exception_request: The redacted request of the attempt that raised
            `last_exception`, or `None` when it failed before a request existed.

    Examples:
        ```python {.exec}
        from pytest_graphql import WaitTimeoutError

        try:
            gql.wait_until(
                "user",
                id="u1",
                until=lambda user: user.name == "Grace Hopper",
                timeout=0.05,
                interval=0.01,
            )
        except WaitTimeoutError as error:
            assert error.operation == "user"
            assert error.attempts >= 1
            assert error.last_response.data.user.name == "Ada Lovelace"
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(
        self,
        *,
        operation: str,
        attempts: int,
        elapsed: float,
        timeout: float,
        last_response: GraphQLResponse[Any] | None,
        last_exception: Exception | None,
        last_exception_request: DiagnosticSnapshot | None = None,
    ) -> None:
        self.operation = operation
        """The name of the query that was polled.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.operation == "user"
            ```
        """
        self.attempts = attempts
        """How many calls were made. At least one call always runs.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.attempts == 3
            ```
        """
        self.elapsed = elapsed
        """The seconds from the first attempt until the poll stopped.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.elapsed == 0.52
            ```
        """
        self.timeout = timeout
        """The limit the poll was given, in seconds.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.timeout == 0.5
            ```
        """
        self.last_response = last_response
        """The most recent response any attempt received, or `None`.

        It includes the response that a swallowed execution error carried.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.last_response.has_data
            ```
        """
        self.last_exception = last_exception
        """The most recent exception that `ignore` swallowed, or `None`.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.last_exception is None
            ```
        """
        self.last_exception_request = last_exception_request
        """The redacted request of the attempt that raised `last_exception`.

        It is `None` when that attempt failed before a request existed.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            error = WaitTimeoutError(
                operation="user",
                attempts=3,
                elapsed=0.52,
                timeout=0.5,
                last_response=gql.execute("{ __typename }"),
                last_exception=None,
            )
            assert error.last_exception_request is None
            ```
        """
        snapshots = self._snapshots()
        super().__init__(self._render(snapshots))
        _check_against(self, snapshots)

    def _exception_snapshots(self) -> tuple[DiagnosticSnapshot, ...]:
        """The request contexts of the attempt that raised the last exception.

        Its own attempt's, and the ones the exception carries. The last
        response is not among them: it can belong to a later attempt, whose
        request can hold a secret the failed attempt's did not.
        """
        found: list[DiagnosticSnapshot] = []
        if self.last_exception_request is not None:
            found.append(self.last_exception_request)
        exception = self.last_exception
        carried = getattr(exception, "request", None)
        if carried is not None:
            found.append(carried)
        response = getattr(exception, "response", None)
        if response is not None:
            found.append(response.request)
        return tuple(found)

    def _snapshots(self) -> tuple[DiagnosticSnapshot, ...]:
        """Every request whose secrets this message must not show."""
        found = list(self._exception_snapshots())
        if self.last_response is not None:
            found.append(self.last_response.request)
        return tuple(found)

    def safe_cause(self) -> Exception | None:
        """Return `last_exception` when it is safe to chain as the cause.

        Chaining prints the exception, and its notes and causes, in the
        traceback. So the library chains it only when it is one of this
        package's own errors, its attempt's request is known, and nothing linked
        to it shows a secret of any request that this error knows. A foreign
        exception is named once, in the message, and is never chained.

        Returns:
            The exception, or `None` when it is not safe to chain.

        Examples:
            ```python {.exec}
            from pytest_graphql import WaitTimeoutError

            try:
                gql.wait_until(
                    "user",
                    id="u1",
                    until=lambda user: False,
                    timeout=0.02,
                    interval=0.01,
                )
            except WaitTimeoutError as error:
                assert error.last_exception is None
                assert error.safe_cause() is None
            ```
        """
        from pytest_graphql._core.diagnostics import exception_chain_is_clean

        exception = self.last_exception
        if not isinstance(exception, GraphQLTestError):
            return None
        if not self._exception_snapshots():
            return None
        if exception_chain_is_clean(exception, self._snapshots()):
            return exception
        return None

    def _render(self, snapshots: Sequence[DiagnosticSnapshot]) -> str:
        from pytest_graphql._core.diagnostics import (
            DEFAULT_MAX_DIAGNOSTIC_BYTES,
            WITHHELD_TEXT,
            escape_control_characters,
            truncate_text,
        )

        if self.last_response is None:
            response = "  Last response: none, no attempt returned one."
        else:
            response = f"  Last response: {_shown(self.last_response)}"
        prefix = "  Last ignored exception: "
        if self.last_exception is None:
            exception = f"{prefix}none."
        elif not self._exception_snapshots():
            exception = (
                f"{prefix}{type(self.last_exception).__name__}: [withheld: no "
                "request context to check this text against, read "
                "WaitTimeoutError.last_exception]"
            )
        else:
            try:
                text = str(self.last_exception)
            except Exception:
                text = "<message unavailable>"
            text, cut = truncate_text(
                escape_control_characters(text), DEFAULT_MAX_DIAGNOSTIC_BYTES
            )
            if cut:
                text += f"... (truncated, {cut} byte(s) cut)"
            exception = _guarded_line(
                snapshots,
                prefix
                + _indented(f"{type(self.last_exception).__name__}: {text}", "    "),
                prefix + WITHHELD_TEXT,
            )
        return "\n".join(
            [
                f"wait_until({self.operation!r}) timed out: {self.attempts} "
                f"attempt(s) in {self.elapsed:.2f} s (limit {self.timeout:g} s).",
                "  Expected: until() to return true before the deadline.",
                response,
                exception,
                "  Fix: raise timeout, check that until() can become true, or "
                "list the exception that hides the real failure in ignore.",
            ]
        )
