"""``validation.py`` in isolation: SPEC 3.2's option table, B23, and B14.

``test_operation.py`` proves these checks fire correctly through the public
``assemble_operation`` entry point. This file tests the same functions
directly, one property per test, the way ``test_selection_policy.py`` tests
``policy.py`` apart from the builder that calls it.
"""

from __future__ import annotations

import pytest
from graphql import GraphQLInt, GraphQLNonNull, GraphQLString, parse
from graphql import (
    build_schema as build_sdl_schema,
)

from pytest_graphql._core.errors import ArgumentError, SelectionError
from pytest_graphql._core.validation import (
    RESERVED_OPTIONS,
    check_no_reserved_collision,
    coerce_variables,
    validate_document,
)
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()

_COLLISION_SCHEMA = build_sdl_schema(
    """
    type Query {
      probe(timeout: Int!, headers: String): Boolean!
    }
    """
)
_PROBE_FIELD = _COLLISION_SCHEMA.query_type.fields["probe"]  # type: ignore[union-attr]


def test_reserved_options_matches_spec_3_2_per_call_option_table() -> None:
    # SPEC 3.2's "Per-call options" table, exactly. A change to either one
    # without the other is exactly the drift RESERVED_OPTIONS exists to
    # prevent, so this is pinned name for name rather than by count.
    assert {
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
    } == RESERVED_OPTIONS


class TestCheckNoReservedCollision:
    def test_raises_for_a_required_argument_named_like_an_option(self) -> None:
        # Required and untouched by kwargs: still always unreachable.
        with pytest.raises(ArgumentError) as excinfo:
            check_no_reserved_collision(
                kind="query",
                operation_name="probe",
                field_def=_PROBE_FIELD,
                kwargs={},
                explicit_variables={},
            )
        message = str(excinfo.value)
        assert "'timeout'" in message
        assert "collides with a reserved per-call option name" in message

    def test_passes_when_supplied_through_the_variables_escape_hatch(self) -> None:
        check_no_reserved_collision(
            kind="query",
            operation_name="probe",
            field_def=_PROBE_FIELD,
            kwargs={"timeout": 5},
            explicit_variables={"timeout": 5},
        )

    def test_does_not_raise_for_an_optional_colliding_argument_left_untouched(
        self,
    ) -> None:
        # 'headers' collides with a reserved option too, but it is nullable
        # and this call never mentions it, so there is nothing to warn about.
        assert not isinstance(_PROBE_FIELD.args["headers"].type, GraphQLNonNull)
        check_no_reserved_collision(
            kind="query",
            operation_name="probe",
            field_def=_PROBE_FIELD,
            kwargs={"timeout": 5},
            explicit_variables={"timeout": 5},
        )

    def test_raises_for_an_optional_colliding_argument_the_kwargs_named(self) -> None:
        # The caller wrote headers="x" meaning it for the 'headers' argument,
        # but a bare keyword named 'headers' is always captured as the
        # per-call option instead, so that value would silently vanish
        # rather than reaching the argument at all.
        with pytest.raises(ArgumentError) as excinfo:
            check_no_reserved_collision(
                kind="query",
                operation_name="probe",
                field_def=_PROBE_FIELD,
                kwargs={"timeout": 5, "headers": "x"},
                explicit_variables={"timeout": 5},
            )
        assert "'headers'" in str(excinfo.value)

    def test_does_not_raise_when_nothing_collides(self) -> None:
        user_field = SCHEMA.query_type.fields["user"]  # type: ignore[union-attr]
        check_no_reserved_collision(
            kind="query",
            operation_name="user",
            field_def=user_field,
            kwargs={"id": "U1"},
            explicit_variables={},
        )


class TestValidateDocument:
    def test_passes_a_valid_document_silently(self) -> None:
        document = parse("{ users { id } }")
        validate_document(SCHEMA, document)

    def test_wraps_the_first_graphql_core_message(self) -> None:
        document = parse("{ doesNotExist }")
        with pytest.raises(SelectionError) as excinfo:
            validate_document(SCHEMA, document)
        assert "doesNotExist" in str(excinfo.value)


class TestCoerceVariables:
    def test_coerces_every_variable_in_the_iterable(self) -> None:
        result = coerce_variables(
            [
                ("id", GraphQLNonNull(GraphQLString), "U1"),
                ("count", GraphQLInt, 5),
            ],
            kind="query",
            operation_name="probe",
        )
        assert result == {"id": "U1", "count": 5}

    def test_raises_argument_error_naming_the_variable_on_a_bad_value(self) -> None:
        with pytest.raises(ArgumentError) as excinfo:
            coerce_variables(
                [("id", GraphQLNonNull(GraphQLString), {"x": 1})],
                kind="query",
                operation_name="probe",
            )
        message = str(excinfo.value)
        assert "query 'probe' received an invalid value for argument 'id'" in message

    def test_raises_on_the_first_bad_variable_and_stops(self) -> None:
        with pytest.raises(ArgumentError) as excinfo:
            coerce_variables(
                [
                    ("first", GraphQLNonNull(GraphQLString), object()),
                    ("second", GraphQLNonNull(GraphQLString), object()),
                ],
                kind="query",
                operation_name="probe",
            )
        assert "'first'" in str(excinfo.value)
