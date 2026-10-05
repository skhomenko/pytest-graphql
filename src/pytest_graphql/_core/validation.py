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
from itertools import islice
from typing import Any

from graphql import (
    DocumentNode,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLSchema,
    is_required_input_field,
    validate,
)
from graphql.pyutils import is_iterable

from pytest_graphql._core.errors import ArgumentError, SelectionError
from pytest_graphql._core.factory.scalars import ScalarRegistry
from pytest_graphql._core.graphql_compat import invalid_input_path
from pytest_graphql._core.naming import field_signature, to_snake
from pytest_graphql._core.serialization import (
    Path,
    SerializationError,
    render_path,
    serialize_variable,
)

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
    scalars: ScalarRegistry | None = None,
) -> dict[str, Any]:
    """B14: serialize every variable, then check it against its declared type.

    ``graphql-core``'s ``validate`` checks the document only, so a value
    like ``id: {"x": 1}`` for an ``ID!`` variable passes it untouched. This
    is the gap B14 closes: every value is coerced here, and any violation
    becomes an ``ArgumentError`` naming the path and the declared type.

    The text comes from the path alone. graphql-core's own message repeats the
    offending value and differs between its 3.2 and 3.3 lines, so it is never
    used, and the error chains nothing that carries it.

    Serialization comes first, so validation checks the value that will be
    sent. Each custom scalar with a registered ``ScalarSpec`` goes through its
    ``serialize``, and a value a custom scalar would put on the wire that is
    not JSON is refused here (``serialization.py``). Coercion is only the
    check. Its result is a Python-side value (a scalar's parser output, an
    enum's internal value, a stored default), so it is dropped, and the
    serialized value is what is returned and sent.
    """
    wire: dict[str, Any] = {}
    for name, type_, value in variables:
        try:
            serialized = serialize_variable(value, type_, scalars, name)
        except SerializationError as error:
            raise ArgumentError.invalid_value(
                kind=kind,
                operation_name=operation_name,
                arg_name=name,
                detail=str(error),
            ) from None
        bad = invalid_input_path(serialized, type_)
        if bad is not None:
            raise ArgumentError.invalid_value(
                kind=kind,
                operation_name=operation_name,
                arg_name=name,
                detail=_describe(type_, serialized, (name, *bad)),
            ) from None
        wire[name] = serialized
    return wire


#: The most field names one message lists, and the longest any one name or type
#: is printed, so a hostile value or schema keeps the message bounded.
_MAX_NAMES = 5
_MAX_NAME_LENGTH = 60

_ABSENT: Any = object()


def _clip(text: str) -> str:
    """``text`` cut to a fixed length, with the cut marked."""
    if len(text) <= _MAX_NAME_LENGTH:
        return text
    return text[:_MAX_NAME_LENGTH] + "..."


def _describe(type_: GraphQLInputType, value: Any, path: Path) -> str:
    """What is wrong at ``path``, in terms of the schema and never of the value.

    ``path`` starts with the variable name. graphql-core reports a missing or
    an unknown input field at the enclosing object, so when the failure sits
    on an input object the message names those fields. They are the schema's
    own names, or the names the caller wrote for fields the schema lacks, and
    never what the fields hold. Every name and type is clipped to a fixed
    length, whatever its source.
    """
    where = _clip(render_path(path))
    declared, found = _locate(type_, value, path[1:])
    inner = declared.of_type if isinstance(declared, GraphQLNonNull) else declared
    if isinstance(inner, GraphQLInputObjectType) and isinstance(found, Mapping):
        missing = _first_names(
            field_name
            for field_name, field in inner.fields.items()
            if is_required_input_field(field) and field_name not in found
        )
        unknown = _first_names(
            key for key in found if isinstance(key, str) and key not in inner.fields
        )
        parts = []
        if missing[1]:
            parts.append(f"is missing the required {_fields(*missing)}")
        if unknown[1]:
            parts.append(f"has no {_fields(*unknown)}")
        if parts:
            return f"{where} {' and '.join(parts)} on '{_clip(inner.name)}'."
    return f"{where} is not a valid value of type '{_clip(str(declared))}'."


def _first_names(names: Iterable[str]) -> tuple[list[str], int]:
    """The first few names and how many there were, holding no more than a few."""
    shown: list[str] = []
    total = 0
    for name in names:
        total += 1
        if len(shown) < _MAX_NAMES:
            shown.append(_clip(name))
    return shown, total


def _fields(shown: list[str], total: int) -> str:
    listed = ", ".join(repr(name) for name in shown)
    more = total - len(shown)
    suffix = f" and {more} more" if more > 0 else ""
    return (f"field {listed}" if total == 1 else f"fields {listed}") + suffix


def _locate(
    type_: GraphQLInputType, value: Any, path: tuple[str | int, ...]
) -> tuple[Any, Any]:
    """The declared type and the value at ``path``, as far as both reach.

    A path graphql-core reports can end above the value it concerns, and a list
    slot holding a single value is reported at the list. The walk stops where
    the declared type, or the value, no longer matches the next segment. A list
    is read for one slot, and is never copied to find it.
    """
    declared: Any = type_
    current: Any = value
    for segment in path:
        inner = declared.of_type if isinstance(declared, GraphQLNonNull) else declared
        if isinstance(inner, GraphQLList) and isinstance(segment, int):
            if not is_iterable(current) or isinstance(current, Mapping):
                break
            slot = next(islice(current, segment, None), _ABSENT)
            if slot is _ABSENT:
                break
            declared, current = inner.of_type, slot
        elif (
            isinstance(inner, GraphQLInputObjectType)
            and isinstance(current, Mapping)
            and segment in inner.fields
        ):
            declared, current = inner.fields[segment].type, current.get(segment)
        else:
            break
    return declared, current
