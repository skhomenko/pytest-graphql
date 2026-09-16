"""What keeps an invalid document from ever reaching the transport (SPEC 8.2).

Three checks, each a distinct row of SPEC 8.2's "before sending" table:

- ``check_no_reserved_collision`` (B23): a schema argument that shares a name
  with a per-call option can never be reached through a plain keyword, so a
  caller is told to use ``variables={...}`` before that silently swallows
  their value as an option instead.
- ``validate_document`` (SPEC 8.2 "Document fails schema validation"): the
  assembled document, run through ``graphql-core``'s own ``validate``.
- ``coerce_variables`` (B14): ``graphql-core``'s ``validate`` checks document
  shape only, never a variable's value, so a document that validates can
  still carry ``id: {"x": 1}`` for an ``ID!`` variable. Each value is coerced
  against its declared type before anything is sent.

``operation.py`` calls all three while assembling a document. Nothing here
talks to a transport or a schema-loading source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from graphql import (
    DocumentNode,
    GraphQLError,
    GraphQLField,
    GraphQLInputType,
    GraphQLNonNull,
    GraphQLSchema,
    validate,
)
from graphql.utilities import coerce_input_value

from pytest_graphql._core.errors import ArgumentError, SelectionError
from pytest_graphql._core.naming import field_signature, to_snake

#: SPEC 3.2's per-call option table. A keyword here is an option, however
#: ``query``/``mutation``/``execute`` receive it; every other keyword is a
#: candidate GraphQL variable (B23). Kept as one frozenset so this module,
#: ``operation.py``, and the client built on top of both (M5c) can never
#: disagree about what counts as an option.
RESERVED_OPTIONS: frozenset[str] = frozenset(
    {
        "fields",
        "variables",
        "raw",
        "validate",
        "raise_on_error",
        "raise_on_partial",
        "max_depth",
        "cycle_policy",
        "per_type_depth_cap",
        "include_deprecated",
        "max_fields",
        "operation_name",
        "timeout",
        "retries",
        "idempotent",
        "headers",
    }
)


def check_no_reserved_collision(
    *,
    kind: str,
    operation_name: str,
    field_def: GraphQLField,
    kwargs: Mapping[str, Any],
    explicit_variables: Mapping[str, Any],
) -> None:
    """B23: refuse an operation argument a plain keyword can never reach.

    Any of the operation's own argument names, exact or snake, that matches
    a reserved option name is unreachable through a bare keyword: that
    keyword is always captured as the option instead (B23's split runs
    before argument resolution ever sees it). The one route left is the
    ``variables={...}`` escape hatch.

    Two shapes both need this, not only the obvious one. A *required*
    argument shaped like this is refused unconditionally: leaving it unset
    always fails, so there is no call this argument can ever answer for
    without ``variables={...}``. An *optional* one raises only when the
    caller's own keywords actually named it: that value would otherwise be
    captured as the option and silently vanish, never reaching this
    argument at all, rather than surfacing as a "missing argument". An
    optional argument nobody touched this call is not an error; the caller
    may simply not need it.
    """
    for arg_name, argument in field_def.args.items():
        if arg_name in explicit_variables:
            continue
        snake_name = to_snake(arg_name)
        if arg_name not in RESERVED_OPTIONS and snake_name not in RESERVED_OPTIONS:
            continue
        required = isinstance(argument.type, GraphQLNonNull)
        attempted = arg_name in kwargs or snake_name in kwargs
        if not required and not attempted:
            continue
        raise ArgumentError.reserved_name_collision(
            kind=kind,
            operation_name=operation_name,
            arg_name=arg_name,
            signature=field_signature(operation_name, field_def),
        )


def validate_document(schema: GraphQLSchema, document: DocumentNode) -> None:
    """SPEC 8.2: reject a document graphql-core's own rules refuse.

    ``validate`` returns a list rather than raising, so an empty list is the
    success case. The first error becomes the message: SPEC 8.2 asks for
    "the graphql-core message", singular, not a batch.
    """
    errors = validate(schema, document)
    if errors:
        raise SelectionError.from_validation(errors[0].message)


def coerce_variables(
    variables: Iterable[tuple[str, GraphQLInputType, Any]],
    *,
    kind: str,
    operation_name: str,
) -> dict[str, Any]:
    """B14: coerce every variable's value against its declared type.

    ``graphql-core``'s ``validate`` checks the document only, so a value
    like ``id: {"x": 1}`` for an ``ID!`` variable passes it untouched. This
    is the gap B14 closes: every value is coerced here, and any violation
    becomes an ``ArgumentError`` naming the path graphql-core's own message
    already describes.
    """
    coerced: dict[str, Any] = {}
    for name, type_, value in variables:
        try:
            coerced[name] = coerce_input_value(value, type_)
        except GraphQLError as error:
            raise ArgumentError.invalid_value(
                kind=kind,
                operation_name=operation_name,
                arg_name=name,
                detail=error.message,
            ) from error
    return coerced
