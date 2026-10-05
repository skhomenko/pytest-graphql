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
    """The base of every exception that `pytest_graphql` raises on purpose.

    Catch it to handle any failure of the library in one place. Every other
    exception of the library is a subclass of it.

    The library still raises a plain `TypeError` or `ValueError` when a call is
    wrong in an ordinary Python way, for example an `expect_error()` count
    below 1. Those are not subclasses of this class.

    It is a different class from `graphql.GraphQLError` of graphql-core, which
    describes an error in a GraphQL document or result.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLClientError, GraphQLTestError

        try:
            gql.query("usr")
        except GraphQLTestError as error:
            assert isinstance(error, GraphQLClientError)
        else:
            raise AssertionError("expected an error")
        ```
    """

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
    """The client could not finish a step that it does itself.

    These steps are checking a call against the schema, loading the schema,
    making a value for `gql.fake` and showing a request as text. The class
    says what the client was doing. It does not say who caused the failure,
    so read the subclass and the message. The message says what was wrong,
    what was expected and what to do about it.

    Most often the call and the schema disagree. A test that only wants a
    result should let this error fail the test. Catch it to check that a
    helper of your own rejects a bad call. A bad name, argument or selection
    is refused before anything is sent: `OperationNotFoundError`,
    `ArgumentError`, `SelectionError` and `SelectionTooLargeError` raise
    before the client sends anything. `SchemaError` can follow a request,
    because the introspection query that loads the schema goes to the server,
    and a server that refuses it is the cause. `DiagnosticRenderError` comes
    after a request was captured.

    Examples:
        ```python {.exec}
        from pytest_graphql import ArgumentError, GraphQLClientError

        try:
            gql.query("user", idd="u1")
        except GraphQLClientError as error:
            assert isinstance(error, ArgumentError)
        else:
            raise AssertionError("expected an error")
        ```
    """


class OperationNotFoundError(GraphQLClientError):
    """The schema has no query or mutation with the name you used.

    `query()`, `mutation()` and `wait_until()` raise this when the name matches
    no operation of that kind. A name matches in its exact spelling and in
    snake_case. The message suggests the closest names and says how many
    operations of that kind the schema has.

    Args:
        kind: `"query"` or `"mutation"`.
        name: The name that was used.
        available: Every name of that kind in the schema.
        total_count: How many names of that kind the schema has.

    Examples:
        ```python {.exec}
        from pytest_graphql import OperationNotFoundError

        try:
            gql.query("usr")
        except OperationNotFoundError as error:
            assert error.kind == "query"
            assert error.name == "usr"
            assert "user" in error.available
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(
        self, *, kind: str, name: str, available: Sequence[str], total_count: int
    ) -> None:
        self.kind = kind
        """The kind that was looked up, `"query"` or `"mutation"`.

        Examples:
            ```python {.exec}
            from pytest_graphql import OperationNotFoundError

            try:
                gql.query("usr")
            except OperationNotFoundError as error:
                assert error.kind == "query"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.name = name
        """The name that matched no operation.

        Examples:
            ```python {.exec}
            from pytest_graphql import OperationNotFoundError

            try:
                gql.query("usr")
            except OperationNotFoundError as error:
                assert error.name == "usr"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.available = tuple(available)
        """Every name of that kind in the schema, as a tuple.

        Examples:
            ```python {.exec}
            from pytest_graphql import OperationNotFoundError

            try:
                gql.query("usr")
            except OperationNotFoundError as error:
                assert "user" in error.available
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.total_count = total_count
        """How many names of that kind the schema has.

        Examples:
            ```python {.exec}
            from pytest_graphql import OperationNotFoundError

            try:
                gql.query("usr")
            except OperationNotFoundError as error:
                assert error.total_count == len(error.available)
            else:
                raise AssertionError("expected an error")
            ```
        """
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
    """An argument of an operation is unknown, missing, repeated or not valid.

    The client raises this before it sends anything, for any of these reasons:

    - The operation has no argument of that name, or an input object has no
      field of that name. The message shows the signature and suggests the
      closest name.
    - A required argument was not given.
    - One argument was given twice, for example as `user_id` and `userId`.
    - A value does not fit the declared type of its argument.
    - `wait_until()` was given a mutation. Only queries can be polled.
    - A call or `build_client()` got a keyword that is neither an argument nor
      a configuration option.

    Args:
        kind: `"query"` or `"mutation"`.
        operation_name: The name of the operation.
        bad_name: The name that is not valid.
        signature: The schema signature of the operation, such as
            `user(id: ID!): User`.
        candidates: The names that are valid in the place of `bad_name`. They
            are the source of the "Did you mean" hint.
        input_type_name: The input type, when `bad_name` was meant as a field
            of an input object. Leave it out otherwise.
        input_fields: The fields of that input type. Leave it out otherwise.

    Examples:
        ```python {.exec}
        from pytest_graphql import ArgumentError

        try:
            gql.query("user", idd="u1")
        except ArgumentError as error:
            assert error.bad_name == "idd"
            assert error.signature == "user(id: ID!): User"
            assert "Did you mean 'id'?" in str(error)
        else:
            raise AssertionError("expected an error")
        ```
    """

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
        """The kind of the operation, `"query"` or `"mutation"`.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", idd="u1")
            except ArgumentError as error:
                assert error.kind == "query"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.operation_name = operation_name
        """The name of the operation.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", idd="u1")
            except ArgumentError as error:
                assert error.operation_name == "user"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.bad_name = bad_name
        """The name that is not valid, or the name of the argument that has a problem.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", idd="u1")
            except ArgumentError as error:
                assert error.bad_name == "idd"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.signature = signature
        """The schema signature of the operation. It is empty when the error does not
        come from one argument name.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", idd="u1")
            except ArgumentError as error:
                assert error.signature == "user(id: ID!): User"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.candidates = tuple(candidates)
        """The valid names that stand in the place of `bad_name`, as a tuple.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", idd="u1")
            except ArgumentError as error:
                assert error.candidates == ("id",)
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.input_type_name = input_type_name
        """The input type, when `bad_name` was meant as a field of an input object.
        Otherwise `None`.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            error = ArgumentError(
                kind="mutation",
                operation_name="createPost",
                bad_name="titel",
                signature="createPost(input: CreatePostInput!): Post",
                candidates=["title", "authorId"],
                input_type_name="CreatePostInput",
                input_fields=["title", "authorId"],
            )
            assert error.input_type_name == "CreatePostInput"
            other = ArgumentError.not_a_query(kind="mutation", name="x")
            assert other.input_type_name is None
            ```
        """
        self.input_fields = tuple(input_fields) if input_fields is not None else None
        """The fields of the input type, as a tuple. `None` when there is no input type.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            error = ArgumentError(
                kind="mutation",
                operation_name="createPost",
                bad_name="titel",
                signature="createPost(input: CreatePostInput!): Post",
                candidates=["title", "authorId"],
                input_type_name="CreatePostInput",
                input_fields=["title", "authorId"],
            )
            assert error.input_fields == ("title", "authorId")
            ```
        """
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
        """Build the error for a required argument that the call did not give.

        The library calls this when a required argument is missing. You rarely
        need it yourself.

        Args:
            kind: `"query"` or `"mutation"`.
            operation_name: The name of the operation.
            arg_name: The name of the missing argument.
            signature: The schema signature of the operation.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            error = ArgumentError.missing_argument(
                kind="query",
                operation_name="user",
                arg_name="id",
                signature="user(id: ID!): User",
            )
            assert "missing required argument 'id'" in str(error)
            ```
        """
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
        """Build the error for a required argument named like a call option.

        Every keyword that a call accepts as an option, such as `raw` or
        `timeout`, is always read as that option. A schema argument with the same
        name cannot be given as a plain keyword. `variables={...}` is the one
        way left. The library calls this when such an argument is required and
        missing. You rarely need it yourself.

        Args:
            kind: `"query"` or `"mutation"`.
            operation_name: The name of the operation.
            arg_name: The name of the argument.
            signature: The schema signature of the operation.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            error = ArgumentError.reserved_name_collision(
                kind="query",
                operation_name="find",
                arg_name="raw",
                signature="find(raw: String!): Item",
            )
            assert "variables=" in str(error)
            ```
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
        """Build the error for one argument that the call gave twice.

        The two keys can be the exact name and the snake_case name of one
        argument, or a `variables={...}` entry next to a plain keyword. The
        client refuses both, because it would have to drop one of the values
        without telling you. The library calls this. You rarely need it
        yourself.

        Args:
            kind: `"query"` or `"mutation"`.
            operation_name: The name of the operation.
            resolved_name: The schema name of the argument.
            first_key: The first key that named it.
            second_key: The second key that named it.
            signature: The schema signature of the operation.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            error = ArgumentError.duplicate_argument(
                kind="mutation",
                operation_name="updateUser",
                resolved_name="userId",
                first_key="user_id",
                second_key="userId",
                signature="updateUser(userId: ID!): User",
            )
            assert "received argument 'userId' twice" in str(error)
            ```
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
        """Build the error for a value that does not fit the type of its argument.

        The library calls this when a value fails the type check. You rarely
        need it yourself.

        Args:
            kind: `"query"` or `"mutation"`.
            operation_name: The name of the operation.
            arg_name: The name of the argument.
            detail: What was wrong with the value.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.query("user", id=object())
            except ArgumentError as error:
                assert "invalid value for argument 'id'" in str(error)
            else:
                raise AssertionError("expected an error")

            error = ArgumentError.invalid_value(
                kind="query",
                operation_name="user",
                arg_name="id",
                detail="$id is not a valid value of type 'ID!'.",
            )
            assert error.bad_name == "id"
            ```
        """
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
        """Build the error for `wait_until()` called on a mutation.

        A poll repeats its call, so a mutation would repeat its side effect on
        every attempt. The library calls this. You rarely need it yourself.

        Args:
            kind: What the operation is, such as `"mutation"`.
            name: The name of the operation.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError

            try:
                gql.wait_until("updateUser", until=bool)
            except ArgumentError as error:
                assert "polls queries only" in str(error)
            else:
                raise AssertionError("expected an error")
            ```
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
        """Build the error for a keyword that is not a configuration option.

        The client refuses an unknown keyword, because accepting it would leave
        the client running with a default that you believed you had replaced.
        The library calls this. You rarely need it yourself.

        Args:
            callable_name: The name of the function that got the keyword.
            bad_name: The keyword that is not known.
            candidates: The options that the function accepts. The message
                lists them and suggests the closest one.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import ArgumentError, build_client

            try:
                build_client(
                    url="http://localhost:8000/graphql",
                    transport=gql.transport,
                    schema=gql.schema,
                    timout=5,
                )
            except ArgumentError as error:
                assert error.bad_name == "timout"
                assert "Did you mean 'timeout'?" in str(error)
            else:
                raise AssertionError("expected an error")
            ```
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
    """A selection is not valid, or the finished document fails the schema check.

    The client raises this before it sends anything, in these cases:

    - `fields` names a field that the type does not have. The message lists the
      valid names and suggests the closest one.
    - `fields` asks for sub-fields of a scalar or an enum.
    - A snake_case field name matches two fields of the schema.
    - Auto-selection leaves no field to ask for.
    - A document that you give to `execute()` fails validation against the
      schema. The message is then the message of graphql-core.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionError

        try:
            gql.query("user", id="u1", fields=["nme"])
        except SelectionError as error:
            assert "no field 'nme' on User" in str(error)
            assert "Did you mean 'name'?" in str(error)
        else:
            raise AssertionError("expected an error")
        ```
    """

    @classmethod
    def unknown_field(
        cls, type_name: str, bad_name: str, available: Sequence[str]
    ) -> SelectionError:
        """Build the error for a field that the type does not have.

        The library calls this when `fields` names such a field. You rarely need
        it yourself.

        Args:
            type_name: The name of the type.
            bad_name: The field name that was written.
            available: The field names of the type. The message lists them and
                suggests the closest one.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionError

            error = SelectionError.unknown_field("User", "nme", ["id", "name"])
            assert "Did you mean 'name'?" in str(error)
            ```
        """
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
        """Build the error for a snake_case name that two or more fields share.

        The library calls this when `fields` uses such a name. You rarely need
        it yourself.

        Args:
            type_name: The name of the type.
            bad_name: The snake_case name that was written.
            spellings: The exact field names that have this snake_case form.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionError

            error = SelectionError.ambiguous_field(
                "User", "user_id", ["userId", "UserId"]
            )
            assert "ambiguous" in str(error)
            ```
        """
        return cls(
            f"{bad_name!r} is ambiguous on {type_name}: it matches "
            f"{', '.join(spellings)}.\n"
            "  Use the exact field name."
        )

    @classmethod
    def from_validation(cls, message: str) -> SelectionError:
        """Build the error for a document that fails validation.

        The text of graphql-core is kept as it is. The library calls this. You
        rarely need it yourself.

        Args:
            message: The validation message.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionError

            try:
                gql.execute("{ nope }")
            except SelectionError as error:
                assert "Cannot query field 'nope'" in str(error)
            else:
                raise AssertionError("expected an error")

            error = SelectionError.from_validation("Cannot query field 'nope'.")
            assert str(error) == "Cannot query field 'nope'."
            ```
        """
        return cls(message)

    @classmethod
    def on_leaf_type(cls, *, field_name: str, type_name: str) -> SelectionError:
        """Build the error for sub-fields that were asked for on a scalar or an enum.

        The library calls this when `fields` writes sub-fields under such a
        field. You rarely need it yourself.

        Args:
            field_name: The name of the field.
            type_name: The name of its scalar or enum type.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionError

            error = SelectionError.on_leaf_type(field_name="name", type_name="String")
            assert "has no fields to select" in str(error)
            ```
        """
        return cls(
            f"{field_name!r} returns {type_name}, a scalar or enum, which has "
            "no fields to select.\n"
            "  Leave fields unset, or pass fields=AUTO."
        )

    @classmethod
    def no_selectable_fields(cls, type_name: str) -> SelectionError:
        """Build the error for an auto-selection that left no field to ask for.

        An empty selection is not a valid document. The message lists the
        settings that widen the selection. The library calls this. You rarely
        need it yourself.

        Args:
            type_name: The name of the type.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionError

            error = SelectionError.no_selectable_fields("User")
            assert "produced no fields" in str(error)
            ```
        """
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
    """Auto-selection would ask for more fields than `max_fields` allows.

    The client builds the selection of a call by walking the schema from the
    return type of the operation. When the walk finds more fields than
    `ClientConfig.max_fields`, which is 2000 by default, it stops and raises
    this error instead of sending a very large document. The message lists the
    ways to make the selection smaller.

    Args:
        type_name: The name of the type that was auto-selected.
        field_count: How many fields the selection would have had.
        limit: The limit that applied.

    Examples:
        ```python {.exec}
        from pytest_graphql import SelectionTooLargeError

        try:
            gql.query("user", id="u1", max_fields=3)
        except SelectionTooLargeError as error:
            assert error.type_name == "User"
            assert error.field_count > error.limit == 3
        else:
            raise AssertionError("expected an error")
        ```
    """

    _SUGGESTIONS = (
        "  Reduce it by one of:\n"
        "    max_depth=2\n"
        '    per_type_depth_cap={"User": 1}\n'
        '    fields=["id", "name"]'
    )

    def __init__(self, *, type_name: str, field_count: int, limit: int) -> None:
        self.type_name = type_name
        """The name of the type that was auto-selected.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionTooLargeError

            try:
                gql.query("user", id="u1", max_fields=3)
            except SelectionTooLargeError as error:
                assert error.type_name == "User"
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.field_count = field_count
        """How many fields the selection would have had.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionTooLargeError

            try:
                gql.query("user", id="u1", max_fields=3)
            except SelectionTooLargeError as error:
                assert error.field_count > error.limit
            else:
                raise AssertionError("expected an error")
            ```
        """
        self.limit = limit
        """The limit that applied, from `max_fields`.

        Examples:
            ```python {.exec}
            from pytest_graphql import SelectionTooLargeError

            try:
                gql.query("user", id="u1", max_fields=3)
            except SelectionTooLargeError as error:
                assert error.limit == 3
            else:
                raise AssertionError("expected an error")
            ```
        """
        first = (
            f"auto-selection of {type_name!r} produced {field_count:,} fields "
            f"(limit {limit})."
        )
        super().__init__(f"{first}\n{self._SUGGESTIONS}")


class SchemaError(GraphQLClientError):
    """A type that you named is not in the schema, or the schema cannot be used.

    The client raises this when:

    - `gql.fake` or `gql.expect` is asked for a type that the schema does not
      have, or that is not the right kind of type for it;
    - a selection names a type in a fragment that is not in the schema, or is
      not a possible type of the field;
    - the schema cannot be loaded, because the introspection query failed, a
      schema file cannot be read, or the schema is not valid.

    Examples:
        ```python {.exec}
        from pytest_graphql import SchemaError

        try:
            gql.fake.Nope()
        except SchemaError as error:
            assert "no type named 'Nope'" in str(error)
        else:
            raise AssertionError("expected an error")
        ```
    """

    @classmethod
    def unknown_type(cls, name: str, available: Sequence[str]) -> SchemaError:
        """Build the error for a type that the schema does not have.

        The library calls this. You rarely need it yourself.

        Args:
            name: The type name that was used.
            available: The type names of the schema. The message suggests the
                closest one.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SchemaError

            error = SchemaError.unknown_type("Usr", ["User", "Team"])
            assert "Did you mean 'User'?" in str(error)
            ```
        """
        lines = [f"no type named {name!r}."]
        match = _best_match(name, available)
        if match:
            lines.append(f"  Did you mean {match!r}?")
        return cls("\n".join(lines))

    @classmethod
    def not_a_possible_type(cls, name: str, parent_name: str) -> SchemaError:
        """Build the error for a fragment type that the field can never return.

        The library calls this. You rarely need it yourself.

        Args:
            name: The type of the fragment.
            parent_name: The type that the fragment is inside.

        Returns:
            The error, ready to raise.

        Examples:
            ```python {.exec}
            from pytest_graphql import SchemaError

            error = SchemaError.not_a_possible_type("Team", "User")
            assert "not a possible type of 'User'" in str(error)
            ```
        """
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
    """The request did not give a usable GraphQL answer.

    This is the base of the errors that come from the network layer. Catch it
    to handle every failure of the connection and of the HTTP answer in one
    place. A test that wants a result should let it fail, because the server is
    not reachable or is not answering as GraphQL.

    The class is raised itself when:

    - the status is 2xx but the body is not a GraphQL response;
    - the body is larger than `max_response_bytes`;
    - the response names a character set that Python does not know;
    - the client cannot build the request, for example because the URL is
      not valid.

    Its subclasses are `GraphQLConnectionError`, `GraphQLTimeoutError` and
    `GraphQLHTTPStatusError`.

    The error holds a copy of the request with every secret removed. It never
    holds the live request, so a credential cannot reach your test report
    through it.

    Args:
        message: The error text.
        request: The request that failed, as a `DiagnosticSnapshot`.
        body_excerpt: The start of the response body. It is cut to a short
            length and has secrets removed. It is empty when no body arrived.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLTestError, GraphQLTransportError

        request = gql.query("users", raw=True).request
        error = GraphQLTransportError("not a GraphQL response", request=request)
        assert isinstance(error, GraphQLTestError)
        assert error.request.kind == "query"
        ```
    """

    def __init__(
        self,
        message: str,
        *,
        request: DiagnosticSnapshot,
        body_excerpt: str = "",
    ) -> None:
        self.request = request
        """The request that failed, with every secret removed.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLTransportError

            request = gql.query("users", raw=True).request
            error = GraphQLTransportError("not a GraphQL response", request=request)
            assert error.request is request
            assert error.request.kind == "query"
            ```
        """
        self.body_excerpt = body_excerpt
        """The start of the response body, cut short and with secrets removed.
        It is an empty string when no body arrived.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLTransportError

            request = gql.query("users", raw=True).request
            error = GraphQLTransportError(
                "not JSON", request=request, body_excerpt="<html>"
            )
            assert error.body_excerpt == "<html>"
            assert GraphQLTransportError("no body", request=request).body_excerpt == ""
            ```
        """
        super().__init__(message)
        request._guard.check_exception(self)


class GraphQLConnectionError(GraphQLTransportError):
    """The client could not reach the server.

    The connection failed, for example because the server is down, the host
    name does not resolve, or a connection attempt timed out. A query is tried
    again after a connection failure, up to `ClientConfig.retries` times, and
    this error is raised when the last try fails. A mutation is not tried again,
    unless the call has `idempotent=True`, because the server may have run it.

    Catch it to test how your code behaves when the server is down. Check
    `request` to see what was being sent.

    The example uses a transport that raises this error, as the HTTP transport
    does when no server answers, so that it needs no network.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLConnectionError, build_client


        class FailingTransport:
            # Fails the way the HTTP transport does when no server answers.

            def send(self, request, *, timeout):
                raise GraphQLConnectionError(
                    "connection failed: connection refused",
                    request=request.redacted(),
                )

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=FailingTransport(),
            schema=gql.schema,
        )
        try:
            client.query("users")
        except GraphQLConnectionError as error:
            assert error.request.url == "http://localhost:8000/graphql"
        else:
            raise AssertionError("expected an error")
        ```
    """


class GraphQLTimeoutError(GraphQLTransportError):
    """The server did not answer in time.

    The wait for the server, the write of the request or the read of the answer
    took longer than `ClientConfig.timeout`. The client never tries again after
    this error, because the server may already have started the work. A timeout
    while connecting is a `GraphQLConnectionError` instead.

    Catch it to test how your code behaves when the server is slow. The limit
    for one call is set with `timeout=` on the call or in `ClientConfig`.

    The example uses a transport that raises this error, as the HTTP transport
    does when a read times out, so that it needs no network.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLTimeoutError, build_client


        class FailingTransport:
            # Fails the way the HTTP transport does when a read times out.

            def send(self, request, *, timeout):
                raise GraphQLTimeoutError(
                    "request timed out: read timeout",
                    request=request.redacted(),
                )

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=FailingTransport(),
            schema=gql.schema,
        )
        try:
            client.query("users")
        except GraphQLTimeoutError as error:
            assert "timed out" in str(error)
        else:
            raise AssertionError("expected an error")
        ```
    """


class GraphQLHTTPStatusError(GraphQLTransportError):
    """The server answered with an error status and no GraphQL result.

    The status was not 2xx, for example 502 from a proxy or 401 from a gateway,
    and the body was not a GraphQL response. `status_code` holds the status and
    `body_excerpt` holds the start of the body, with secrets removed. A non-2xx
    answer that is a valid GraphQL response is not this error. It becomes a
    `GraphQLRequestError` or a `GraphQLExecutionError`, according to its body.

    Catch it to test how your code behaves when something in front of the server
    fails.

    The example uses a transport that raises this error, as the HTTP transport
    does for a 502 answer, so that it needs no network.

    Args:
        message: The error text.
        request: The request that failed, as a `DiagnosticSnapshot`.
        status_code: The HTTP status code of the answer.
        body_excerpt: The start of the body, cut short and with secrets removed.

    Examples:
        ```python {.exec}
        from pytest_graphql import GraphQLHTTPStatusError, build_client


        class FailingTransport:
            # Fails the way the HTTP transport does for a 502 answer.

            def send(self, request, *, timeout):
                raise GraphQLHTTPStatusError(
                    "request failed with status 502: Bad Gateway",
                    request=request.redacted(),
                    status_code=502,
                    body_excerpt="Bad Gateway",
                )

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=FailingTransport(),
            schema=gql.schema,
        )
        try:
            client.query("users")
        except GraphQLHTTPStatusError as error:
            assert error.status_code == 502
            assert error.body_excerpt == "Bad Gateway"
        else:
            raise AssertionError("expected an error")
        ```
    """

    def __init__(
        self,
        message: str,
        *,
        request: DiagnosticSnapshot,
        status_code: int,
        body_excerpt: str = "",
    ) -> None:
        self.status_code = status_code
        """The HTTP status code of the answer.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLHTTPStatusError

            request = gql.query("users", raw=True).request
            error = GraphQLHTTPStatusError(
                "bad gateway", request=request, status_code=502
            )
            assert error.status_code == 502
            ```
        """
        super().__init__(message, request=request, body_excerpt=body_excerpt)


class GraphQLRequestError(GraphQLTestError):
    """The server refused the request before it ran it.

    The server answered with a GraphQL response that has `errors` and no `data`
    at all, whatever the status. This is what a server sends for a document it
    cannot parse or validate, or for a request that it does not accept. The
    operation never started, so this differs from `GraphQLExecutionError`,
    where the operation ran and failed. A response with a `data` entry, even a
    `null` one, is an execution result.

    `expect_error()` does not catch this error, because it is not an execution
    error. To test that a server refuses a request, catch this class.

    Args:
        message: The error text.
        request: The request that failed, as a `DiagnosticSnapshot`.
        status_code: The HTTP status code of the answer.
        media_type: The media type of the answer.
        errors: The error objects of the answer, as mappings. Secrets are
            removed.

    Examples:
        The example uses a transport that raises this error, as the HTTP
        transport does for a rejected document, so that it needs no network.

        ```python {.exec}
        from pytest_graphql import GraphQLRequestError, build_client


        class RejectingTransport:
            # Fails the way the HTTP transport does for a document the server rejects.

            def send(self, request, *, timeout):
                raise GraphQLRequestError(
                    "the server rejected the request before execution (status 400).",
                    request=request.redacted(),
                    status_code=400,
                    media_type="application/json",
                    errors=({"message": "Syntax Error: Unexpected Name 'usr'."},),
                )

            def close(self):
                pass


        client = build_client(
            url="http://localhost:8000/graphql",
            transport=RejectingTransport(),
            schema=gql.schema,
        )
        try:
            client.query("users")
        except GraphQLRequestError as error:
            assert error.status_code == 400
            assert error.errors[0]["message"].startswith("Syntax Error")
        else:
            raise AssertionError("expected an error")
        ```
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
        """The request that failed, with every secret removed.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLRequestError

            error = GraphQLRequestError(
                "rejected",
                request=gql.query("users", raw=True).request,
                status_code=400,
                media_type="application/json",
                errors=({"message": "Syntax Error"},),
            )
            assert error.request.kind == "query"
            ```
        """
        self.status_code = status_code
        """The HTTP status code of the answer.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLRequestError

            error = GraphQLRequestError(
                "rejected",
                request=gql.query("users", raw=True).request,
                status_code=400,
                media_type="application/json",
                errors=({"message": "Syntax Error"},),
            )
            assert error.status_code == 400
            ```
        """
        self.media_type = media_type
        """The media type of the answer, such as `application/json`.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLRequestError

            error = GraphQLRequestError(
                "rejected",
                request=gql.query("users", raw=True).request,
                status_code=400,
                media_type="application/json",
                errors=({"message": "Syntax Error"},),
            )
            assert error.media_type == "application/json"
            ```
        """
        self.errors = errors
        """The error objects of the answer, as a tuple of mappings. Secrets are removed.

        Examples:
            ```python {.exec}
            from pytest_graphql import GraphQLRequestError

            error = GraphQLRequestError(
                "rejected",
                request=gql.query("users", raw=True).request,
                status_code=400,
                media_type="application/json",
                errors=({"message": "Syntax Error"},),
            )
            assert error.errors[0]["message"] == "Syntax Error"
            ```
        """
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
