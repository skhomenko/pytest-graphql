"""In-memory schema caching, for the process lifetime (SPEC 5.3)."""

from __future__ import annotations

from graphql import GraphQLSchema

from pytest_graphql._core.schema.cache import CachedSchemaSource
from pytest_graphql._core.schema.source import SDLFileSource
from tests.schema.resolvers import SDL_PATH


class _CountingSource:
    def __init__(self, source: SDLFileSource) -> None:
        self._source = source
        self.load_count = 0

    def load(self) -> GraphQLSchema:
        self.load_count += 1
        return self._source.load()

    @property
    def fingerprint(self) -> str:
        return self._source.fingerprint


def test_cached_schema_source_loads_the_wrapped_source_once() -> None:
    counting = _CountingSource(SDLFileSource(SDL_PATH))
    cached = CachedSchemaSource(counting)

    first = cached.load()
    second = cached.load()

    assert counting.load_count == 1
    assert first is second


def test_cached_schema_source_passes_the_fingerprint_through() -> None:
    source = SDLFileSource(SDL_PATH)
    cached = CachedSchemaSource(source)
    assert cached.fingerprint == source.fingerprint
