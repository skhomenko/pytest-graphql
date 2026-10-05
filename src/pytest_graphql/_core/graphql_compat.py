"""The one place that absorbs differences between graphql-core 3.2 and 3.3.

The declared range is ``graphql-core>=3.2,<3.4``, and the library keeps one
documented behavior on both. Every difference that matters is handled here, so
no other module reads a graphql-core attribute whose shape changed or branches
on the version. A difference is detected by what graphql-core offers, never by
its version number. DESIGN section 10 states which differences exist.

AST collections. A parsed node carries a tuple for a list-valued field in 3.2,
empty when the source wrote none. In 3.3 the same field is ``None`` when the
source wrote none, and the type says so. :func:`ast_tuple` reads either shape
as a tuple, so a caller never tests for ``None``.

Printed literals. ``print_ast`` puts spaces inside the braces of an object
literal in 3.3 (``{ a: 1 }``) and none in 3.2 (``{a: 1}``). The selection
layer builds the same literal both from a node and by hand, and compares the
text, so it needs one spelling. :func:`print_value` is that spelling.

Defaults. An argument or input field built from SDL or from introspection keeps
its default as a literal in 3.3, in a new ``default`` object, and leaves the
legacy ``default_value`` as ``Undefined``. In 3.2 ``default_value`` holds the
coerced Python value. :func:`default_of` returns that coerced value on both
lines, so no other module reads ``default_value``.

Scalar parsing. 3.3 renamed a scalar's ``parse_value`` to ``coerce_input_value``
and kept the old name as a deprecated alias. :func:`parse_scalar_input` calls
whichever the installed line treats as current.

Variable coercion. :func:`invalid_input_path` answers where a value fails its
input type, which is the one question the library asks of variable coercion.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar, cast

import graphql.utilities
from graphql import (
    GraphQLArgument,
    GraphQLInputField,
    GraphQLInputType,
    GraphQLScalarType,
    ListValueNode,
    ObjectValueNode,
    Undefined,
    ValueNode,
    print_ast,
)

_T = TypeVar("_T")

#: True on graphql-core 3.3 and later. The public ``GraphQLDefaultInput`` class
#: arrived with the 3.3 default model, so its presence says which model this is.
#: Every choice between the two behaviors below rests on this one flag, and a
#: 3.3 export that is missing fails the import instead of selecting the 3.2
#: behavior, which would be wrong on 3.3 without any error.
LINE_3_3: bool = hasattr(graphql, "GraphQLDefaultInput")


def _utility(name: str) -> Any:
    """A ``graphql.utilities`` export, as ``Any`` because the lines disagree on it.

    A signature written for one line would fail ``mypy --strict`` on the other.
    A missing name raises ``AttributeError`` here, at import.
    """
    return getattr(graphql.utilities, name)


_coerce_input_value: Any = _utility("coerce_input_value")
_validate_input_value: Any = _utility("validate_input_value") if LINE_3_3 else None
_coerce_input_literal: Any = _utility("coerce_input_literal") if LINE_3_3 else None


class _FirstFailureError(Exception):
    """Raised from a graphql-core callback to end the walk at its first failure."""

    def __init__(self, path: tuple[str | int, ...]) -> None:
        super().__init__()
        self.path = path


def ast_tuple(collection: Iterable[_T] | None) -> tuple[_T, ...]:
    """An optional AST collection as a tuple, empty when it is absent."""
    return () if collection is None else tuple(collection)


def parse_scalar_input(scalar: GraphQLScalarType, value: Any) -> Any:
    """``value`` parsed by ``scalar``'s input hook, which raises if it refuses it."""
    parse = getattr(scalar, "coerce_input_value" if LINE_3_3 else "parse_value")
    return parse(value)


def default_of(item: GraphQLArgument | GraphQLInputField) -> Any:
    """The coerced default of an argument or an input field, or ``Undefined``.

    This is what ``default_value`` holds in 3.2, whichever way the default was
    declared. In 3.3 a default written in SDL, or received through
    introspection, is stored as a literal and coerced here with graphql-core's
    public coercion functions. A default that cannot be coerced to its type
    reads as no default, whatever it raises, which is what 3.2 gives for the
    same introspection result.
    """
    if not LINE_3_3:
        return item.default_value
    default: Any = cast("Any", item).default
    if default is None:
        return item.default_value
    try:
        if default.literal is not None:
            return _coerce_input_literal(default.literal, item.type)
        return _coerce_input_value(default.value, item.type)
    except Exception:
        return Undefined


def print_value(node: ValueNode) -> str:
    """A value literal as text, spelled the same on both graphql-core lines.

    Object and list literals are written here, as ``{a: 1, b: [2, 3]}``, which
    is the 3.2 spelling and the one the selection layer builds by hand. Every
    other node is a leaf, and ``print_ast`` prints a leaf the same on both
    lines.
    """
    if isinstance(node, ObjectValueNode):
        fields = (
            f"{field.name.value}: {print_value(field.value)}"
            for field in ast_tuple(node.fields)
        )
        return "{" + ", ".join(fields) + "}"
    if isinstance(node, ListValueNode):
        return "[" + ", ".join(print_value(v) for v in ast_tuple(node.values)) + "]"
    return print_ast(node)


def invalid_input_path(
    value: Any, type_: GraphQLInputType
) -> tuple[str | int, ...] | None:
    """Where ``value`` first fails to be a valid ``type_``, or ``None`` if it fits.

    The answer is a path of field names and list indexes, empty for the value
    itself. It is the only part of graphql-core's verdict this returns. The
    message graphql-core attaches to a failure repeats the offending value
    (``Int cannot represent non-integer value: 'x'``), and a caller that
    builds its own text from the path can never repeat it.

    Variable coercion changed between the two lines. 3.2 reports through the
    ``on_error`` callback of ``coerce_input_value``. 3.3 removed that callback,
    and ``coerce_input_value`` returns ``Undefined`` there, so the path comes
    from ``validate_input_value``, which 3.2 does not have. The first report
    ends the walk, so memory and calls into a scalar's hooks do not grow with
    the number of failures in a value.

    Both lines report the path of the enclosing object for a missing or an
    unknown input field, and the path of the value itself otherwise.
    """

    def stop(path: Any) -> None:
        raise _FirstFailureError(tuple(path))

    try:
        if _validate_input_value is not None:
            _validate_input_value(value, type_, lambda _error, path: stop(path))
        else:
            # 3.2: coerce_input_value takes the callback, and the exception
            # leaves the walk at the first failure it reports.
            _coerce_input_value(value, type_, lambda path, _value, _error: stop(path))
    except _FirstFailureError as failure:
        return failure.path
    return None
