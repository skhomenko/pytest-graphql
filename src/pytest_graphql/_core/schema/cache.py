"""In-memory schema caching, for the process lifetime (SPEC 5.3).

``CachedSchemaSource`` wraps any ``SchemaSource`` and loads it at most once.
It satisfies ``SchemaSource`` itself, so it composes with any code that
already accepts one. The disk cache SPEC 5.3 also describes, keyed by
``sha256(url + auth-identity + plugin version)`` and gated by the
``gql_schema_cache_dir`` fixture, is pytest-facing configuration and belongs
to the plugin layer built in M9, not here.
"""

from __future__ import annotations

from graphql import GraphQLSchema

from pytest_graphql._core.schema.source import SchemaSource


class CachedSchemaSource:
    """Loads ``source`` at most once and returns the same schema after that."""

    def __init__(self, source: SchemaSource) -> None:
        self._source = source
        self._schema: GraphQLSchema | None = None

    def load(self) -> GraphQLSchema:
        if self._schema is None:
            self._schema = self._source.load()
        return self._schema

    @property
    def fingerprint(self) -> str:
        return self._source.fingerprint
