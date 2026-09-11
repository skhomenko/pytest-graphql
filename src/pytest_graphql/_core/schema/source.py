"""Where a ``GraphQLSchema`` comes from (SPEC 5.3).

``SchemaSource`` returns graphql-core's own schema object rather than a
bespoke type index, so parsing, printing, validation and type resolution all
come for free.

``IntrospectionSource`` needs to run exactly one query and read back its
envelope. It depends on ``QueryExecutor`` for that, not on the wire-level
``Transport`` protocol (``send(request, timeout) -> RawResponse``) that
``transport/`` defines in M5a: that protocol, and the ``RequestInfo`` and
``RawResponse`` types it speaks in, do not exist yet at this point in the
build order. ``QueryExecutor`` is the minimal shape ``FakeTransport``
already has. When M5a's transport lands, it is expected to gain an adapter
satisfying this protocol, or this module is expected to change to speak
``Transport`` directly; either way, that is M5a's decision to make, not this
one's to anticipate.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from graphql import (
    GraphQLSchema,
    build_client_schema,
    build_schema,
    get_introspection_query,
)

from pytest_graphql._core.errors import SchemaError


class SchemaSource(Protocol):
    """Loads a ``GraphQLSchema``, on demand, from wherever it lives."""

    def load(self) -> GraphQLSchema: ...

    @property
    def fingerprint(self) -> str:
        """A stable label for this source, for cache keys and failure reports."""
        ...


@runtime_checkable
class QueryExecutor(Protocol):
    """The minimal ability to run one GraphQL query and get back its envelope."""

    def execute(
        self, query: str, *, variables: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]: ...


class IntrospectionSource:
    """Loads a schema by running the standard introspection query.

    Uses graphql-core's own ``get_introspection_query`` and
    ``build_client_schema`` rather than a hand-written introspection query,
    which is what let the reference implementation's version silently
    truncate deeply wrapped types such as ``[[String!]!]!``.
    """

    def __init__(self, transport: QueryExecutor, *, label: str | None = None) -> None:
        self._transport = transport
        self._label = label

    def load(self) -> GraphQLSchema:
        envelope = self._transport.execute(get_introspection_query())
        errors = envelope.get("errors")
        if errors:
            raise SchemaError(f"introspection query failed: {errors!r}")
        data = envelope.get("data")
        if data is None:
            raise SchemaError("introspection query returned no data")
        try:
            return build_client_schema(data)
        except Exception as exc:
            raise SchemaError(
                f"introspection data did not build a valid schema: {exc}"
            ) from exc

    @property
    def fingerprint(self) -> str:
        if self._label is not None:
            return f"introspection:{self._label}"
        return f"introspection:{type(self._transport).__name__}"


class SDLFileSource:
    """Loads a schema from a local ``.graphql`` SDL file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> GraphQLSchema:
        try:
            sdl = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaError(
                f"could not read schema file {self._path}: {exc}"
            ) from exc
        try:
            return build_schema(sdl)
        except Exception as exc:
            raise SchemaError(
                f"schema file {self._path} is not valid SDL: {exc}"
            ) from exc

    @property
    def fingerprint(self) -> str:
        return f"sdl:{self._path}"
