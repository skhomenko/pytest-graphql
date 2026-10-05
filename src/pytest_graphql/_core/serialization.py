"""Variable serialization: the JSON a variable puts on the wire.

``ScalarSpec.serialize`` turns a Python value into JSON. ``serialize_variable``
walks one variable value against its declared input type and returns the value
that is sent. It applies ``serialize`` at every custom scalar it meets: at the
top, inside a list or a list of lists, inside an input object, and inside a
list of input objects. It runs before validation, and validation then checks
this same value, so what is checked is exactly what is sent.

Rules:

- The result is the wire value. Nothing a type's own parser or default
  produces is put on the wire, because those are Python-side values: a custom
  scalar's ``parse_value`` can return a ``Decimal``, and an enum can hold a
  Python object. Validation only checks the wire value and never replaces it.
- ``None`` stays ``None`` and ``serialize`` is not called for it.
- A custom scalar with a registered spec goes through ``spec.serialize``. A
  custom scalar with none keeps its value, which has to be JSON already.
- Whatever reaches the wire for a custom scalar is checked to be JSON: ``None``,
  a bool, an int, a finite float, a str, a list or tuple of those, or a dict
  with str keys. Validation cannot check it, because a custom scalar accepts any
  value, so without this check a ``Decimal`` would fail in the transport with an
  error that names neither the variable nor the scalar.
- A built-in scalar goes through its own ``parse_value`` when that accepts the
  value, so ``5`` for an ``ID`` is sent as ``"5"``. A value it refuses is left
  as it is for validation to report. An enum value is the member name, and is
  left as it is.
- A list slot takes any iterable that is not a string or a mapping, as
  graphql-core does, and a single value is wrapped in a list of one. The result
  is always a list, so a custom scalar whose ``serialize`` returns a list is
  never mistaken for the list slot.
- An input field the caller left out stays out. The server applies the
  field's default, so no default value is sent from this side.
- A refusal names the place (``$input.lines[0].price``) and the type of the
  offending value. It never repeats the value or the message of an exception
  raised by ``serialize``, because either can hold data a test wants kept out
  of its output. The refusal is raised with no exception chained to it, so
  that text is in no traceback either.
- The caller's value is never changed. The result is a new structure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from graphql import (
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLScalarType,
    is_specified_scalar_type,
)
from graphql.pyutils import is_iterable

from pytest_graphql._core.factory.scalars import ScalarRegistry
from pytest_graphql._core.graphql_compat import parse_scalar_input

#: How deeply a JSON value from a custom scalar may nest before it is refused.
MAX_JSON_DEPTH = 128

Path = tuple[str | int, ...]


class SerializationError(ValueError):
    """A variable value cannot be put on the wire as JSON."""


def render_path(path: Path) -> str:
    """``$input.lines[0].price``: the variable, then names and indexes."""
    text = f"${path[0]}"
    for segment in path[1:]:
        text += f"[{segment}]" if isinstance(segment, int) else f".{segment}"
    return text


def serialize_variable(
    value: Any,
    type_: GraphQLInputType,
    scalars: ScalarRegistry | None,
    name: str,
) -> Any:
    """``value`` with every registered custom scalar serialized to JSON."""
    return _walk(value, type_, scalars, (name,))


def _walk(
    value: Any, type_: GraphQLInputType, scalars: ScalarRegistry | None, path: Path
) -> Any:
    if value is None:
        return None
    if isinstance(type_, GraphQLNonNull):
        type_ = type_.of_type
    if isinstance(type_, GraphQLList):
        # The same test graphql-core makes. One item where a list is declared
        # is a list of one, wrapped here so the result is always a list.
        items = list(value) if is_iterable(value) else [value]
        return [
            _walk(item, type_.of_type, scalars, (*path, index))
            for index, item in enumerate(items)
        ]
    if isinstance(type_, GraphQLInputObjectType):
        if not isinstance(value, Mapping):
            return value
        fields = type_.fields
        return {
            key: (
                _walk(item, fields[key].type, scalars, (*path, key))
                if key in fields
                else item
            )
            for key, item in value.items()
        }
    if isinstance(type_, GraphQLScalarType):
        if is_specified_scalar_type(type_):
            return _built_in(value, type_)
        return _custom(value, type_.name, scalars, path)
    return value


def _built_in(value: Any, type_: GraphQLScalarType) -> Any:
    """The JSON form of a built-in scalar, or ``value`` when the scalar refuses it."""
    try:
        return parse_scalar_input(type_, value)
    except Exception:
        # Validation reports the refusal, by path and declared type.
        return value


def _custom(value: Any, scalar: str, scalars: ScalarRegistry | None, path: Path) -> Any:
    spec = scalars.get(scalar) if scalars is not None else None
    if spec is None:
        return _json(value, scalar, False, path, 0)
    try:
        serialized = spec.serialize(value)
    except Exception as error:
        raise SerializationError(
            f"{render_path(path)} could not be serialized.\n"
            f"  The serialize function of ScalarSpec {scalar!r} raised "
            f"{type(error).__name__}.\n"
            "  It receives the Python value you supplied for the variable."
        ) from None
    return _json(serialized, scalar, True, path, 0)


def _json(value: Any, scalar: str, registered: bool, path: Path, depth: int) -> Any:
    """``value`` as plain JSON, or a ``SerializationError`` naming where it is not."""
    if depth > MAX_JSON_DEPTH:
        raise SerializationError(
            f"{render_path(path)} is nested too deeply for the scalar {scalar!r}.\n"
            f"  JSON values may nest up to {MAX_JSON_DEPTH} levels."
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise _refusal(path, scalar, registered, "a float that is not finite")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _refusal(
                    path, scalar, registered, f"an object key of type {_name(key)}"
                )
            out[key] = _json(item, scalar, registered, (*path, key), depth + 1)
        return out
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _json(item, scalar, registered, (*path, index), depth + 1)
            for index, item in enumerate(value)
        ]
    raise _refusal(path, scalar, registered, f"a {_name(value)}")


def _name(value: object) -> str:
    return type(value).__name__


def _refusal(
    path: Path, scalar: str, registered: bool, found: str
) -> SerializationError:
    where = render_path(path)
    if registered:
        return SerializationError(
            f"{where} is not JSON after serialize.\n"
            f"  The serialize function of ScalarSpec {scalar!r} returned {found}, "
            "which is not JSON.\n"
            "  It must return None, a bool, an int, a finite float, a str, a "
            "list or a dict with str keys."
        )
    return SerializationError(
        f"{where} holds {found}, which is not JSON.\n"
        f"  The scalar {scalar!r} has no registered ScalarSpec, so the value is "
        "sent as it is.\n"
        "  Register one whose serialize returns JSON:\n"
        f'    registry.register(ScalarSpec(name="{scalar}", serialize=str, '
        "fake=lambda rng: ...))"
    )
