"""Building one input value from its schema type (SPEC 3.7).

``fake_input`` is the whole engine. It is a pure function of the schema type,
the derived seed, the node id, the field path and the call options, and it
reads no clock, no environment and no global state, so one call with one set of
inputs gives one result on every machine and every supported Python.

The rules, each pinned by a test:

- A non-null field is always filled. A nullable field is filled unless
  ``required_only`` is set.
- An enum value is picked by the sampler from the member names sorted by code
  point, never the first declared one. Sorting makes the pick independent of
  declaration order, so a server that reorders its enum does not change it.
- A nested input object is a level deeper than the object that holds it. The
  root is level 0. A list adds no level. Objects up to ``depth`` are filled in
  full. An object past ``depth`` is filled with required fields only, which
  cuts a recursive type at the cap and still builds a required nested object
  that cannot be left out. GraphQL forbids a non-null cycle of input objects,
  so required-only filling always ends.
- A list gets one to three elements, drawn from its own stream.
- A custom scalar resolves through the registry. A scalar with no spec raises
  ``ScalarNotRegisteredError``, and only when its field would be filled: an
  override or ``required_only`` never looks the scalar up.
- Every value draws from its own ``DeterministicRandom``, keyed by the seed and
  the full field path, with the root type name first. So a field keeps its
  value when another field is skipped, added or overridden.
- Output keys are the exact schema field names, in schema order. They are the
  one spelling every consumer accepts, and the exact names cannot collide the
  way two snake_case forms can.
- Deprecation is ignored. A deprecated input field or enum value is treated
  like any other.

Path segments: the root type name, then field names, with a list index as an
integer segment. A marker for list length uses the segment ``"#length"``,
which no field name can equal because a GraphQL name has no ``#``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLScalarType,
)

from pytest_graphql._core.errors import ScalarNotRegisteredError
from pytest_graphql._core.factory.rng import DeterministicRandom
from pytest_graphql._core.factory.scalars import BUILTIN_FAKES, ScalarRegistry
from pytest_graphql._core.factory.unique import UniqueSource, resolve

Path = tuple[str | int, ...]

#: A list has 1 to this many elements.
MAX_LIST_LENGTH = 3

LENGTH_MARKER = "#length"


@dataclass(frozen=True)
class Context:
    """Everything one call needs besides the type: explicit, never ambient."""

    seed: int
    node_id: str
    scalars: ScalarRegistry
    unique_source: UniqueSource
    required_only: bool
    depth: int


def fake_input(
    type_: GraphQLInputObjectType,
    context: Context,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A plain ``dict`` for ``type_``. ``overrides`` is keyed by exact field name."""
    return _object(type_, context, (type_.name,), 0, overrides or {})


def _object(
    type_: GraphQLInputObjectType,
    context: Context,
    path: Path,
    level: int,
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    lean = context.required_only or level > context.depth
    payload: dict[str, Any] = {}
    for name, field in type_.fields.items():
        field_path = (*path, name)
        if name in overrides:
            payload[name] = resolve(
                overrides[name], context.unique_source, context.node_id, field_path
            )
        elif isinstance(field.type, GraphQLNonNull) or not lean:
            payload[name] = _value(field.type, context, field_path, level)
    return payload


def _value(type_: GraphQLInputType, context: Context, path: Path, level: int) -> Any:
    if isinstance(type_, GraphQLNonNull):
        type_ = type_.of_type
    if isinstance(type_, GraphQLList):
        length = 1 + _stream(context, path, LENGTH_MARKER).below(MAX_LIST_LENGTH)
        return [
            _value(type_.of_type, context, (*path, index), level)
            for index in range(length)
        ]
    if isinstance(type_, GraphQLInputObjectType):
        return _object(type_, context, path, level + 1, {})
    if isinstance(type_, GraphQLEnumType):
        return _stream(context, path).choice(sorted(type_.values))
    assert isinstance(type_, GraphQLScalarType)
    builtin = BUILTIN_FAKES.get(type_.name)
    if builtin is not None:
        return builtin(_stream(context, path))
    spec = context.scalars.get(type_.name)
    if spec is None:
        raise ScalarNotRegisteredError(type_.name, path=path)
    return spec.fake(_stream(context, path))


def _stream(context: Context, path: Path, *extra: str) -> DeterministicRandom:
    return DeterministicRandom(context.seed, *path, *extra)
