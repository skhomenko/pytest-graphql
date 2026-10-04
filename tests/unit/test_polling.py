"""``wait_until`` (SPEC 3.9, DESIGN_DECISIONS.md section 5, C12).

No test here sleeps in real time. An autouse fixture replaces the polling
module's clock and sleep with a fake pair, so a poll runs through minutes of
simulated time and every sleep it asks for is recorded exactly.
"""

from __future__ import annotations

import gc
import math
import traceback
import weakref
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import polling
from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLConnectionError,
    GraphQLExecutionError,
    GraphQLFieldError,
    OperationNotFoundError,
    WaitTimeoutError,
)
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.response import GraphQLResponse
from tests.unit.m8_support import (
    FakeClock,
    build_test_schema,
    failure,
    make_client,
    rejected,
    user,
)


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(polling, "_monotonic", fake.monotonic)
    monkeypatch.setattr(polling, "_sleep", fake.sleep)
    return fake


def _never(_value: Any) -> bool:
    return False


def _shipped(value: Any) -> bool:
    return bool(value.status == "SHIPPED")


# -- attempts and success ----------------------------------------------------


def test_success_on_the_first_attempt_returns_without_sleeping(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(schema, user("SHIPPED"))

    result = client.wait_until("user", id="u1", until=_shipped)

    assert result.status == "SHIPPED"
    assert len(transport.sent) == 1
    assert clock.sleeps == []


def test_success_on_a_later_attempt_sleeps_between_attempts(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(schema, user(), user(), user("SHIPPED"))

    result = client.wait_until(
        "user", id="u1", until=_shipped, timeout=60, interval=2, backoff=1.5
    )

    assert result.status == "SHIPPED"
    assert len(transport.sent) == 3
    assert clock.sleeps == [2, 3]


def test_the_value_handed_to_until_is_what_query_would_return(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("SHIPPED"))
    seen: list[Any] = []

    client.wait_until("user", id="u1", until=lambda value: seen.append(value) or True)

    assert seen[0].id == "u1"
    assert seen[0].__typename__ == "User"


def test_raw_hands_until_and_returns_the_response(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user("SHIPPED"))

    result = client.wait_until(
        "user",
        id="u1",
        raw=True,
        until=lambda response: response.has_data,
    )

    assert isinstance(result, GraphQLResponse)


def test_per_call_options_reach_every_attempt(schema: GraphQLSchema) -> None:
    client, transport = make_client(schema, user(), user("SHIPPED"))

    client.wait_until(
        "user",
        id="u1",
        fields=["id", "status"],
        headers={"X-Probe": "1"},
        until=_shipped,
        interval=1,
    )

    assert [dict(sent.headers).get("X-Probe") for sent in transport.sent] == ["1", "1"]
    assert all("name" not in sent.document for sent in transport.sent)


def test_a_variable_named_like_a_poll_option_goes_through_variables(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, user("SHIPPED"))

    client.wait_until("user", variables={"id": "u9"}, until=_shipped)

    assert transport.sent[0].variables["id"] == "u9"


# -- the deadline and the sleep sequence -------------------------------------


def test_timeout_zero_still_makes_one_attempt_and_never_sleeps(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until("user", id="u1", until=_never, timeout=0)

    assert len(transport.sent) == 1
    assert caught.value.attempts == 1
    assert clock.sleeps == []


def test_timeout_zero_returns_when_the_first_attempt_is_ready(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, user("SHIPPED"))

    assert client.wait_until("user", id="u1", until=_shipped, timeout=0)
    assert len(transport.sent) == 1


def test_the_sleep_sequence_is_exact_and_the_last_sleep_is_clamped(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user", id="u1", until=_never, timeout=20, interval=2, backoff=1.5
        )

    # remaining is 20, 18, 15, 10.5 and 3.75 after the first five attempts,
    # so the fifth sleep is the remaining 3.75, not 2 * 1.5**4 = 10.125.
    assert clock.sleeps == [2, 3, 4.5, 6.75, 3.75]
    assert len(transport.sent) == 6
    assert caught.value.attempts == 6
    assert caught.value.elapsed == 20


def test_there_is_no_separate_cap_on_a_single_delay(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    # SPEC 3.9 capped a delay at ten intervals. The design rule has no cap:
    # the deadline is the only bound.
    client, _ = make_client(schema, user())

    with pytest.raises(WaitTimeoutError):
        client.wait_until(
            "user", id="u1", until=_never, timeout=1000, interval=1, backoff=3
        )

    assert clock.sleeps[:6] == [1, 3, 9, 27, 81, 243]
    assert max(clock.sleeps) > 10


def test_a_constant_interval_repeats_until_the_deadline(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, _ = make_client(schema, user())

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until("user", id="u1", until=_never, timeout=5, interval=2)

    assert clock.sleeps == [2, 2, 1]
    assert caught.value.attempts == 4


def test_the_deadline_is_computed_once_and_slow_calls_use_it_up(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    # Each call costs five seconds. A deadline recomputed per attempt would
    # never arrive. Attempts start at 0, 6 and 12 seconds and the last one
    # ends at 17, past the deadline at 12.
    client, _ = make_client(schema, user(), on_send=lambda _r: clock.advance(5))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until("user", id="u1", until=_never, timeout=12, interval=1)

    assert caught.value.attempts == 3
    assert caught.value.elapsed == 17
    assert clock.sleeps == [1, 1]


def test_a_sleep_that_lands_on_the_deadline_is_followed_by_a_last_attempt(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(schema, user(), user("SHIPPED"))

    result = client.wait_until("user", id="u1", until=_shipped, timeout=5, interval=5)

    assert result.status == "SHIPPED"
    assert clock.sleeps == [5]
    assert len(transport.sent) == 2


@pytest.mark.parametrize(
    ("interval", "backoff", "attempt", "remaining", "expected"),
    [
        (2.0, 1.5, 1, 100.0, 2.0),
        (2.0, 1.5, 3, 100.0, 4.5),
        (2.0, 1.5, 3, 4.0, 4.0),
        (1.0, 2.0, 5000, 7.0, 7.0),  # backoff ** 4999 overflows a float
        (0.0, 2.0, 5000, 7.0, 0.0),  # zero times an overflow is still zero
        (0.0, 1.0, 1, 7.0, 0.0),
        (3.0, 1.0, 9, 100.0, 3.0),
    ],
)
def test_the_next_delay_formula(
    interval: float, backoff: float, attempt: int, remaining: float, expected: float
) -> None:
    assert polling._next_delay(interval, backoff, attempt, remaining) == expected


# -- ignore ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        BaseException,
        KeyboardInterrupt,
        SystemExit,
        (ValueError, KeyboardInterrupt),
        "ValueError",
        ValueError("an instance, not a class"),
        5,
        [ValueError],
        (ValueError, 5),
        int,
    ],
)
def test_ignore_accepts_only_exception_subclasses(
    schema: GraphQLSchema, bad: Any
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(TypeError, match="ignore"):
        client.wait_until("user", id="u1", until=_never, ignore=bad)

    assert transport.sent == []


@pytest.mark.parametrize(
    "good", [(), Exception, ValueError, (ValueError, KeyError), (GraphQLFieldError,)]
)
def test_ignore_accepts_a_class_or_a_tuple_of_classes(
    schema: GraphQLSchema, good: Any
) -> None:
    client, _ = make_client(schema, user("SHIPPED"))

    assert client.wait_until("user", id="u1", until=_shipped, ignore=good)


def test_an_ignored_error_counts_as_not_ready_and_is_remembered(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    client, transport = make_client(
        schema, rejected(failure("not yet")), user("SHIPPED")
    )

    result = client.wait_until(
        "user", id="u1", until=_shipped, ignore=GraphQLExecutionError, interval=3
    )

    assert result.status == "SHIPPED"
    assert len(transport.sent) == 2
    assert clock.sleeps == [3]


def test_ignored_errors_are_counted_as_attempts(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, rejected(failure("not yet")))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=_shipped,
            ignore=GraphQLExecutionError,
            timeout=4,
            interval=2,
        )

    assert caught.value.attempts == 3
    assert isinstance(caught.value.last_exception, GraphQLExecutionError)


def test_an_error_the_predicate_raises_is_ignored_when_listed(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("SHIPPED"))
    calls: list[int] = []

    def until(value: Any) -> bool:
        calls.append(1)
        if len(calls) < 3:
            raise IndexError("not yet")
        return bool(value.status == "SHIPPED")

    assert client.wait_until("user", id="u1", until=until, ignore=IndexError)
    assert len(calls) == 3


def test_an_exception_outside_ignore_propagates_unchanged(
    schema: GraphQLSchema, clock: FakeClock
) -> None:
    plain = RuntimeError("not listed")
    client, transport = make_client(schema, user(), plain)

    with pytest.raises(RuntimeError) as caught:
        client.wait_until(
            "user", id="u1", until=_never, ignore=(ValueError,), interval=1
        )

    assert caught.value is plain
    assert len(transport.sent) == 2
    assert clock.sleeps == [1]


def test_an_error_raised_by_the_predicate_outside_ignore_propagates(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())

    def until(_value: Any) -> bool:
        raise KeyError("bad predicate")

    with pytest.raises(KeyError, match="bad predicate"):
        client.wait_until("user", id="u1", until=until, ignore=(ValueError,))


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_a_base_exception_from_the_call_is_never_swallowed(
    schema: GraphQLSchema, signal: type[BaseException]
) -> None:
    client, _ = make_client(schema, user(), signal())

    with pytest.raises(signal):
        client.wait_until("user", id="u1", until=_never, ignore=(Exception,))


def test_a_base_exception_from_the_predicate_is_never_swallowed(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user())

    def until(_value: Any) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        client.wait_until("user", id="u1", until=until, ignore=(Exception,))


def test_a_keyboard_interrupt_during_the_sleep_is_never_swallowed(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, transport = make_client(schema, user())

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(polling, "_sleep", interrupted)

    with pytest.raises(KeyboardInterrupt):
        client.wait_until("user", id="u1", until=_never, ignore=(Exception,))

    assert len(transport.sent) == 1


# -- the failure -------------------------------------------------------------


def test_the_timeout_carries_attempts_elapsed_response_and_exception(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("NEW"))
    first = client.query("user", id="u1", raw=True)
    down = GraphQLConnectionError("down", request=first.request)
    client, _ = make_client(schema, user("NEW"), down)

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=_shipped,
            ignore=(GraphQLConnectionError,),
            timeout=3,
            interval=1,
        )

    error = caught.value
    assert error.attempts == 4
    assert error.elapsed == 3
    assert error.timeout == 3
    assert error.operation == "user"
    assert isinstance(error.last_response, GraphQLResponse)
    assert error.last_response.unwrap().status == "NEW"
    assert error.last_exception is not None
    assert type(error.last_exception) is GraphQLConnectionError


def test_a_swallowed_execution_error_supplies_the_last_response(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("NEW"), rejected(failure("later")))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=_never,
            ignore=GraphQLExecutionError,
            timeout=2,
            interval=1,
        )

    response = caught.value.last_response
    assert response is not None
    assert response.errors[0].message == "later"


def test_the_message_structure_names_attempts_elapsed_and_what_to_do(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("NEW"))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user", id="u1", until=_shipped, timeout=2, interval=1, ignore=ValueError
        )

    error = caught.value
    assert error.last_response is not None
    assert str(error) == (
        "wait_until('user') timed out: 3 attempt(s) in 2.00 s (limit 2 s).\n"
        "  Expected: until() to return true before the deadline.\n"
        f"  Last response: {error.last_response!r}\n"
        "  Last ignored exception: none.\n"
        "  Fix: raise timeout, check that until() can become true, or list the "
        "exception that hides the real failure in ignore."
    )


def test_the_message_names_the_last_ignored_exception(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user())

    def until(_value: Any) -> bool:
        raise ValueError("no tracking number yet")

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user", id="u1", until=until, ignore=ValueError, timeout=1, interval=1
        )

    assert "  Last ignored exception: ValueError: no tracking number yet\n" in str(
        caught.value
    )


def test_a_timeout_with_no_response_says_so(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user())
    first = client.query("user", id="u1", raw=True)
    down = GraphQLConnectionError("down", request=first.request)
    client, _ = make_client(schema, down)

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=_never,
            ignore=GraphQLConnectionError,
            timeout=0,
        )

    assert caught.value.last_response is None
    assert "  Last response: none, no attempt returned one.\n" in str(caught.value)


def test_a_guarded_last_exception_is_chained_as_the_cause(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("x")))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user", id="u1", until=_never, ignore=GraphQLExecutionError, timeout=0
        )

    assert caught.value.__cause__ is caught.value.last_exception


def test_a_foreign_last_exception_is_never_chained(schema: GraphQLSchema) -> None:
    # A traceback prints the whole cause chain. An exception the library did
    # not build was never checked against the request's secrets, so it must
    # not be printed beside the timeout.
    client, _ = make_client(schema, user())

    def until(_value: Any) -> bool:
        raise ValueError("foreign text")

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until("user", id="u1", until=until, ignore=ValueError, timeout=0)

    assert caught.value.__cause__ is None
    # The message names it once. A chained cause would print it a second time.
    assert "".join(traceback.format_exception(caught.value)).count("foreign text") == 1


# -- queries only ------------------------------------------------------------


def test_a_mutation_name_is_refused_before_any_call(schema: GraphQLSchema) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(ArgumentError) as caught:
        client.wait_until("updateUser", id="u1", until=_never)

    assert transport.sent == []
    assert str(caught.value) == (
        "wait_until polls queries only, and 'updateUser' is a mutation.\n"
        "  Polling would repeat its side effect on every attempt.\n"
        "  Poll a query that reads the result, or call mutation('updateUser') once."
    )


def test_the_snake_case_spelling_of_a_mutation_is_refused_too(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(ArgumentError, match="is a mutation"):
        client.wait_until("update_user", id="u1", until=_never)

    assert transport.sent == []


def test_a_name_that_exists_as_both_is_polled_as_the_query(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(WaitTimeoutError):
        client.wait_until("reset", raw=True, until=_never, timeout=0)

    assert transport.sent[0].kind == "query"


def test_a_query_that_only_looks_like_a_mutation_is_polled(
    schema: GraphQLSchema,
) -> None:
    # Resolution is by schema lookup. `createReport` starts like a mutation
    # name and is a query here, so it is polled.
    client, transport = make_client(schema, user())

    with pytest.raises(WaitTimeoutError):
        client.wait_until("createReport", id="r1", raw=True, until=_never, timeout=0)

    assert transport.sent[0].kind == "query"


def test_an_unknown_name_is_an_operation_not_found_error(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(OperationNotFoundError):
        client.wait_until("usr", id="u1", until=_never)

    assert transport.sent == []


# -- argument checks ---------------------------------------------------------


@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("timeout", -1, ValueError),
        ("timeout", math.nan, ValueError),
        ("timeout", math.inf, ValueError),
        ("timeout", "5", TypeError),
        ("timeout", True, TypeError),
        ("timeout", None, TypeError),
        ("interval", -0.5, ValueError),
        ("interval", math.nan, ValueError),
        ("interval", math.inf, ValueError),
        ("interval", "1", TypeError),
        ("backoff", 0.5, ValueError),
        ("backoff", math.nan, ValueError),
        ("backoff", math.inf, ValueError),
        ("backoff", False, TypeError),
    ],
)
def test_a_bad_numeric_option_is_refused_before_any_call(
    schema: GraphQLSchema, option: str, value: Any, error: type[Exception]
) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(error, match=option):
        client.wait_until("user", id="u1", until=_never, **{option: value})

    assert transport.sent == []


def test_until_must_be_callable(schema: GraphQLSchema) -> None:
    client, transport = make_client(schema, user())

    with pytest.raises(TypeError, match="until"):
        client.wait_until("user", id="u1", until="SHIPPED")  # type: ignore[arg-type]

    assert transport.sent == []


# -- bounded state -----------------------------------------------------------


class _NotYetError(ValueError):
    """A subclass, because a built-in exception instance has no weak reference."""


class _Watch(BaseMiddleware):
    """Holds a weak reference to every response, so a leak is countable."""

    def __init__(self) -> None:
        self.responses: list[weakref.ref[GraphQLResponse[Any]]] = []

    def after_response(
        self, response: GraphQLResponse[Any]
    ) -> GraphQLResponse[Any] | None:
        self.responses.append(weakref.ref(response))
        return None


def test_a_long_poll_keeps_only_the_last_response_and_exception(
    schema: GraphQLSchema,
) -> None:
    watch = _Watch()
    client, transport = make_client(
        schema, user(), middleware=[watch], max_recorded_calls=5
    )
    errors: list[weakref.ref[BaseException]] = []

    def until(_value: Any) -> bool:
        error = _NotYetError("not yet")
        errors.append(weakref.ref(error))
        raise error

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user", id="u1", until=until, ignore=_NotYetError, timeout=2000, interval=1
        )
    gc.collect()

    assert caught.value.attempts >= 1000
    assert len(watch.responses) == caught.value.attempts
    assert sum(ref() is not None for ref in watch.responses) <= 1
    assert sum(ref() is not None for ref in errors) <= 1
    assert len(client.recorder) == 5
    assert len(transport.sent) == caught.value.attempts


def test_a_swallowed_execution_error_without_a_response_keeps_the_last_one(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user("NEW"), GraphQLExecutionError("by hand"))

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=_never,
            ignore=GraphQLExecutionError,
            timeout=1,
            interval=1,
        )

    assert caught.value.last_response is not None
    assert caught.value.last_response.unwrap().status == "NEW"
    assert str(caught.value.last_exception) == "by hand"
