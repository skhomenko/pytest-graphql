"""Schema loading (SPEC 5.3): introspection, SDL files, and their fingerprints."""

from __future__ import annotations

from pathlib import Path

import pytest
from graphql import GraphQLSyntaxError, print_schema

from pytest_graphql._core.errors import SchemaError
from pytest_graphql._core.schema.source import IntrospectionSource, SDLFileSource
from tests.schema.fake_transport import FakeTransport
from tests.schema.resolvers import SDL_PATH, build_schema


def test_introspection_source_reproduces_the_sdl_built_schema() -> None:
    transport = FakeTransport(build_schema())
    source = IntrospectionSource(transport)

    rebuilt = source.load()

    assert print_schema(rebuilt) == print_schema(build_schema())


def test_introspection_source_preserves_deeply_wrapped_types() -> None:
    # Query.matrix: [[String!]!]! is the regression case for the
    # introspection truncation bug (PLAN.md M3).
    transport = FakeTransport(build_schema())
    rebuilt = IntrospectionSource(transport).load()

    matrix_type = rebuilt.query_type
    assert matrix_type is not None
    assert str(matrix_type.fields["matrix"].type) == "[[String!]!]!"


def test_introspection_source_fingerprint_uses_the_label_when_given() -> None:
    source = IntrospectionSource(FakeTransport(build_schema()), label="test-schema")
    assert source.fingerprint == "introspection:test-schema"


def test_introspection_source_fingerprint_falls_back_to_transport_type() -> None:
    source = IntrospectionSource(FakeTransport(build_schema()))
    assert source.fingerprint == "introspection:FakeTransport"


class _FailingExecutor:
    def execute(self, _query: str, *, _variables: object = None) -> dict[str, object]:
        return {"errors": [{"message": "boom"}]}


def test_introspection_source_raises_schema_error_on_execution_errors() -> None:
    source = IntrospectionSource(_FailingExecutor())
    with pytest.raises(SchemaError, match="boom"):
        source.load()


class _EmptyExecutor:
    def execute(self, _query: str, *, _variables: object = None) -> dict[str, object]:
        return {"data": None}


def test_introspection_source_raises_schema_error_on_missing_data() -> None:
    source = IntrospectionSource(_EmptyExecutor())
    with pytest.raises(SchemaError, match="no data"):
        source.load()


class _MalformedExecutor:
    def execute(self, _query: str, *, _variables: object = None) -> dict[str, object]:
        # A present but incomplete introspection payload: graphql-core's
        # build_client_schema raises a bare KeyError on this, not a
        # GraphQLTestError, unless IntrospectionSource translates it.
        return {"data": {"__schema": {}}}


def test_introspection_source_raises_schema_error_on_malformed_data() -> None:
    source = IntrospectionSource(_MalformedExecutor())
    with pytest.raises(SchemaError, match="did not build a valid schema") as excinfo:
        source.load()
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_sdl_file_source_loads_the_hostile_schema() -> None:
    source = SDLFileSource(SDL_PATH)
    schema = source.load()
    assert schema.query_type is not None
    assert "user" in schema.query_type.fields


def test_sdl_file_source_fingerprint_names_the_path() -> None:
    source = SDLFileSource(SDL_PATH)
    assert source.fingerprint == f"sdl:{SDL_PATH}"


def test_sdl_file_source_raises_schema_error_when_file_is_missing(
    tmp_path: Path,
) -> None:
    source = SDLFileSource(tmp_path / "does-not-exist.graphql")
    with pytest.raises(SchemaError, match="could not read"):
        source.load()


def test_sdl_file_source_raises_schema_error_on_malformed_sdl(
    tmp_path: Path,
) -> None:
    bad_sdl = tmp_path / "broken.graphql"
    bad_sdl.write_text("type Query { broken: }", encoding="utf-8")
    source = SDLFileSource(bad_sdl)
    with pytest.raises(SchemaError, match="not valid SDL") as excinfo:
        source.load()
    assert isinstance(excinfo.value.__cause__, GraphQLSyntaxError)
