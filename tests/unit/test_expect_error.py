"""``expect_error`` and ``CapturedErrors`` (SPEC 3.8, M8).

Every test runs a real client over a scripted transport, so each response is
exactly the one the test states and no socket opens.
"""

from __future__ import annotations

import re
import traceback
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import GraphQLClient
from pytest_graphql._core.errors import (
    ExpectedErrorNotRaised,
    GraphQLConnectionError,
    GraphQLExecutionError,
    GraphQLPartialDataError,
    GraphQLTestError,
)
from pytest_graphql._core.expect_error import CapturedErrors
from tests.unit.scripted_steps import (
    build_test_schema,
    envelope,
    failure,
    make_client,
    rejected,
    user,
)


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


def _call(client: GraphQLClient) -> Any:
    return client.query("user", id="u1", fields=["id"])


# -- the block must raise ----------------------------------------------------


def test_a_matching_block_passes_and_exposes_what_it_captured(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(
        schema,
        rejected(
            failure("denied", code="FORBIDDEN", path=["user"]),
            failure("second"),
        ),
    )

    with client.expect_error() as captured:
        _call(client)

    assert isinstance(captured, CapturedErrors)
    assert [error.message for error in captured.errors] == ["denied", "second"]
    assert captured.first is captured.errors[0]
    assert captured.first.code == "FORBIDDEN"
    assert captured.response.http.status_code == 200
    assert len(captured.response.errors) == 2


def test_the_block_not_raising_is_an_error_naming_the_response(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        _call(client)

    error = caught.value
    assert error.response is not None
    assert error.errors == ()
    assert error.unmatched == ()
    assert error.calls == 1
    assert str(error) == (
        "expect_error: the block did not raise GraphQLExecutionError.\n"
        "  Expected: a response with errors that the client raises.\n"
        f"  Last response (of 1 call(s) in the block): {error.response!r}\n"
        "  Fix: make the call fail the way the test expects, or remove "
        "expect_error.\n"
        "  A call with raise_on_error=False never raises, so read "
        "response.errors instead."
    )


def test_the_message_counts_every_call_the_block_made(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user("A"), user("B"))

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        _call(client)
        _call(client)

    assert caught.value.calls == 2
    assert "Last response (of 2 call(s) in the block): " in str(caught.value)


def test_a_block_that_made_no_call_says_so(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user())

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        pass

    error = caught.value
    assert error.response is None
    assert error.calls == 0
    assert str(error).splitlines()[:3] == [
        "expect_error: the block did not raise GraphQLExecutionError.",
        "  Expected: a response with errors that the client raises.",
        "  The block made no GraphQL call.",
    ]


def test_a_response_with_errors_that_the_client_let_through_is_named(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("boom")), raise_on_error=False)

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        client.query("user", id="u1", fields=["id"], raw=True)

    lines = str(caught.value).splitlines()
    assert (
        "  The last response carried 1 error(s) and the client did not raise: "
        "raise_on_error or raise_on_partial let it through."
    ) in lines
    assert caught.value.errors == ()


# -- other exceptions propagate unchanged ------------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        ValueError("unrelated"),
        KeyboardInterrupt(),
        SystemExit(3),
        GeneratorExit(),
    ],
)
def test_any_other_exception_propagates_unchanged(
    schema: GraphQLSchema, raised: BaseException
) -> None:
    client, _ = make_client(schema, user())

    with pytest.raises(type(raised)) as caught, client.expect_error():
        raise raised

    assert caught.value is raised
    assert caught.value.__cause__ is None


def test_a_transport_failure_is_not_an_expected_error(schema: GraphQLSchema) -> None:
    plain = RuntimeError("not graphql")
    client, _ = make_client(schema, plain)

    with pytest.raises(RuntimeError) as caught, client.expect_error():
        _call(client)

    assert caught.value is plain


def test_a_protocol_violation_is_not_an_expected_error(schema: GraphQLSchema) -> None:
    # No errors and no data: the server broke the protocol. Capturing it
    # would let a broken server satisfy `expect_error()`.
    client, _ = make_client(schema, envelope(None))

    with (
        pytest.raises(GraphQLExecutionError, match="protocol violation"),
        client.expect_error(),
    ):
        _call(client)


def test_partial_data_counts_as_an_execution_error(schema: GraphQLSchema) -> None:
    client, _ = make_client(
        schema,
        envelope({"user": {"id": "u1"}}, (failure("half", code="PARTIAL"),)),
    )

    with client.expect_error(code="PARTIAL") as captured:
        _call(client)

    assert captured.first.message == "half"
    assert captured.response.has_data


def test_an_exception_from_a_nested_block_passes_through_the_outer_one(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(),
        client.expect_error(),
    ):
        _call(client)

    assert caught.value.calls == 1


# -- filters -----------------------------------------------------------------


def _client_with_errors(schema: GraphQLSchema) -> GraphQLClient:
    client, _ = make_client(
        schema,
        rejected(
            failure("Email already exists", code="CONFLICT", path=["user", "email"]),
            failure("Not allowed", code="FORBIDDEN", path=["user", 0]),
        ),
    )
    return client


def test_a_code_filter_matches_the_error_code_exactly(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(code="FORBIDDEN"):
        _call(client)


def test_a_code_filter_is_not_a_substring_search(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="FORBID"),
    ):
        _call(client)

    assert caught.value.unmatched == ("code",)


def test_a_path_filter_matches_the_whole_path(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(path=["user", "email"]):
        _call(client)


@pytest.mark.parametrize("path", [["user"], ["user", "email", "x"], ["email", "user"]])
def test_a_path_filter_is_not_a_prefix_match(
    schema: GraphQLSchema, path: list[str]
) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(path=path),
    ):
        _call(client)

    assert caught.value.unmatched == ("path",)


def test_a_path_filter_accepts_a_tuple_and_an_index(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(path=("user", 0)):
        _call(client)


def test_a_boolean_path_segment_is_not_an_index(schema: GraphQLSchema) -> None:
    # Python says True == 1. A path index is an integer, never a boolean.
    client = _client_with_errors(schema)

    with pytest.raises(TypeError, match="path"):
        client.expect_error(path=["user", True])


def test_a_message_filter_is_a_regular_expression_search(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(message_matches="already exist"):
        _call(client)


def test_a_message_filter_anchors_are_the_authors_to_write(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(message_matches=r"^Not allowed$"):
        _call(client)
    with (
        pytest.raises(ExpectedErrorNotRaised),
        client.expect_error(message_matches=r"^allowed"),
    ):
        _call(client)


def test_a_message_filter_accepts_a_compiled_pattern_and_its_flags(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(message_matches=re.compile("NOT ALLOWED", re.IGNORECASE)):
        _call(client)


def test_every_supplied_filter_must_match_some_error(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(
        code="CONFLICT", path=["user", 0], message_matches="Not allowed"
    ):
        _call(client)


def test_only_the_filters_that_matched_nothing_are_unmatched(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(
            code="CONFLICT", path=["nowhere"], message_matches="missing"
        ),
    ):
        _call(client)

    assert caught.value.unmatched == ("path", "message_matches")


def test_an_unmatched_filter_message_lists_every_error_returned(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="TIMEOUT", message_matches="slow"),
    ):
        _call(client)

    error = caught.value
    assert [each.message for each in error.errors] == [
        "Email already exists",
        "Not allowed",
    ]
    assert error.response is not None
    assert str(error) == (
        "expect_error: a filter matched none of the errors the server returned.\n"
        "  Unmatched filters: code='TIMEOUT', message_matches='slow'\n"
        "  The server returned 2 error(s):\n"
        '    [1] CONFLICT at ["user", "email"]: Email already exists\n'
        '    [2] FORBIDDEN at ["user", 0]: Not allowed\n'
        "  Fix: change the filters to match one of these errors, or fix the "
        "operation that returned them."
    )


def test_an_error_with_no_code_and_no_path_still_has_a_line(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("plain")))

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error(code="X"):
        _call(client)

    assert "    [1] (no code): plain" in str(caught.value).splitlines()


def test_a_failed_filter_still_fills_the_captured_object(
    schema: GraphQLSchema,
) -> None:
    client = _client_with_errors(schema)
    captured_box: list[CapturedErrors] = []

    with (
        pytest.raises(ExpectedErrorNotRaised),
        client.expect_error(code="TIMEOUT") as captured,
    ):
        captured_box.append(captured)
        _call(client)

    assert len(captured_box[0].errors) == 2


def test_a_failed_filter_chains_the_error_it_judged(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="TIMEOUT"),
    ):
        _call(client)

    assert isinstance(caught.value.__cause__, GraphQLExecutionError)


def test_the_error_list_is_bounded_and_says_how_many_it_left_out(
    schema: GraphQLSchema,
) -> None:
    many = tuple(failure(f"problem {n}", code="E") for n in range(25))
    client, _ = make_client(schema, rejected(*many), max_recorded_errors=3)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="NOPE"),
    ):
        _call(client)

    lines = str(caught.value).splitlines()
    assert "  The server returned 25 error(s):" in lines
    assert [line for line in lines if line.startswith("    [")] == [
        "    [1] E: problem 0",
        "    [2] E: problem 1",
        "    [3] E: problem 2",
    ]
    assert "    ... and 22 more not shown (max_recorded_errors=3)." in lines
    assert len(caught.value.errors) == 25


# -- the call and its arguments ----------------------------------------------


def test_a_bad_filter_value_is_refused_when_the_context_is_built(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema)

    with pytest.raises(TypeError, match="code"):
        client.expect_error(code=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="path"):
        client.expect_error(path="user.email")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="path"):
        client.expect_error(path=[1.5])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="matches"):
        client.expect_error(message_matches=5)  # type: ignore[arg-type]
    with pytest.raises(re.error):
        client.expect_error(message_matches="(")


def test_a_call_made_through_a_clone_is_seen_by_the_block(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())
    other = client.as_("a-token-value")

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        other.query("user", id="u1", fields=["id"])

    assert caught.value.calls == 1


def test_a_call_after_the_block_is_not_counted(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, rejected(failure("x")), user())

    with client.expect_error():
        _call(client)
    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        pass

    assert caught.value.calls == 0


def test_an_outer_block_counts_the_calls_of_an_inner_one(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("x")), user())

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        with client.expect_error():
            _call(client)
        _call(client)

    assert caught.value.calls == 2


def test_the_block_state_is_released_when_the_block_raises(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())

    with pytest.raises(ValueError, match="stop"), client.expect_error():
        raise ValueError("stop")
    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        pass

    assert caught.value.calls == 0


def test_reading_the_capture_before_the_block_ends_is_refused(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("x")))

    with client.expect_error() as captured:
        for read in (
            lambda: captured.errors,
            lambda: captured.response,
            lambda: captured.first,
        ):
            with pytest.raises(GraphQLTestError, match="after the with block"):
                read()
        _call(client)

    assert captured.first.message == "x"


def test_a_captured_object_shows_a_count_and_no_server_text(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("hidden message")))

    with client.expect_error() as captured:
        _call(client)

    assert repr(captured) == "CapturedErrors(errors=1)"


def test_a_connection_failure_inside_the_block_propagates(
    schema: GraphQLSchema,
) -> None:
    # The transport exception needs a snapshot, so build one from a response.
    client, _ = make_client(schema, user())
    response = client.query("user", id="u1", fields=["id"], raw=True)
    down = GraphQLConnectionError("down", request=response.request)
    client, _ = make_client(schema, down)

    with pytest.raises(GraphQLConnectionError) as caught, client.expect_error():
        _call(client)

    assert caught.value is down


def test_partial_data_error_is_a_subclass_the_block_accepts(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(
        schema, envelope({"user": {"id": "u1"}}, (failure("half"),))
    )

    with pytest.raises(GraphQLPartialDataError) as caught:
        _call(client)

    assert isinstance(caught.value, GraphQLExecutionError)


def test_the_traceback_of_a_failed_filter_is_readable(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="TIMEOUT"),
    ):
        _call(client)

    text = "".join(traceback.format_exception(caught.value))
    assert "ExpectedErrorNotRaised: expect_error: a filter matched none" in text


# -- count and a response-less error ------------------------------------------


def test_count_is_an_exact_total_of_the_errors_returned(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with client.expect_error(count=2) as captured:
        _call(client)

    assert len(captured.errors) == 2


@pytest.mark.parametrize("wrong", [1, 3])
def test_a_count_that_differs_fails_with_the_full_list(
    schema: GraphQLSchema, wrong: int
) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(count=wrong),
    ):
        _call(client)

    error = caught.value
    assert error.unmatched == ("count",)
    assert len(error.errors) == 2
    lines = str(error).splitlines()
    assert f"  Unmatched filters: count={wrong}" in lines
    assert "  The server returned 2 error(s):" in lines


def test_count_ignores_the_matching_filters(schema: GraphQLSchema) -> None:
    # `code` matches one of two errors. The count is still the total, 2.
    client = _client_with_errors(schema)

    with client.expect_error(code="FORBIDDEN", count=2):
        _call(client)
    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="FORBIDDEN", count=1),
    ):
        _call(client)

    assert caught.value.unmatched == ("count",)


def test_count_and_a_filter_can_both_be_unmatched(schema: GraphQLSchema) -> None:
    client = _client_with_errors(schema)

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="NOPE", count=5),
    ):
        _call(client)

    assert caught.value.unmatched == ("code", "count")


@pytest.mark.parametrize(
    ("count", "error"),
    [
        (0, ValueError),
        (-1, ValueError),
        (True, TypeError),
        (1.0, TypeError),
        ("2", TypeError),
    ],
)
def test_a_bad_count_is_refused_when_the_context_is_built(
    schema: GraphQLSchema, count: Any, error: type[Exception]
) -> None:
    client, _ = make_client(schema)

    with pytest.raises(error, match="count"):
        client.expect_error(count=count)


def test_an_execution_error_built_without_a_response_is_not_captured(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema)
    built = GraphQLExecutionError("built by hand")

    with pytest.raises(GraphQLExecutionError) as caught, client.expect_error():
        raise built

    assert caught.value is built
