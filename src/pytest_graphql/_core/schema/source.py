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
    """Tells the client where to read the schema from.

    By default the client reads the schema by asking the server, with the
    standard introspection query. Give `build_client()` a `schema_source` when
    the schema lives somewhere else, for example in a `.graphql` file in your
    repository, or when the server has introspection turned off.

    Any object with a `load` method and a `fingerprint` property of this shape
    is a `SchemaSource`. The schema is a `graphql.GraphQLSchema` from
    `graphql-core`.

    Examples:
        A source that builds the schema from SDL text:

        ```python {.exec}
        from graphql import GraphQLSchema, build_schema

        from pytest_graphql import build_client


        class InlineSource:
            def __init__(self, sdl: str) -> None:
                self.sdl = sdl

            def load(self) -> GraphQLSchema:
                return build_schema(self.sdl)

            @property
            def fingerprint(self) -> str:
                return "inline"


        client = build_client(
            url="http://localhost:8000/graphql",
            schema_source=InlineSource("type Query { ping: Boolean! }"),
        )
        assert "ping" in client.schema.query_type.fields
        client.close()
        ```
    """

    def load(self) -> GraphQLSchema:
        """Read the schema and return it.

        Returns:
            The schema.

        Raises:
            Exception: When the schema cannot be read or is not valid. The
                exception reaches the caller.

        Examples:
            ```python {.exec}
            from graphql import build_schema

            class OneField:
                fingerprint = "one-field"

                def load(self):
                    return build_schema("type Query { ping: Boolean! }")

            schema = OneField().load()
            assert schema.query_type.name == "Query"
            ```
        """
        ...

    @property
    def fingerprint(self) -> str:
        """A stable label for this source.

        Use text that does not change between runs for the same schema, such as
        a file path. It names the source in cache keys and in failure reports.

        Examples:
            ```python {.exec}
            class FileSource:
                def __init__(self, path):
                    self.path = path

                @property
                def fingerprint(self):
                    return f"sdl:{self.path}"

            assert FileSource("schema.graphql").fingerprint == "sdl:schema.graphql"
            ```
        """
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
