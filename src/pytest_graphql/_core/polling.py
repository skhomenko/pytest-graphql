"""``wait_until`` (SPEC 3.9, DESIGN_DECISIONS.md section 5, C12).

A poll repeats a query until a predicate holds. Its rules are small and exact:

- One deadline, computed once from the monotonic clock before the first call.
- Every attempt is counted when its call is made, and at least one attempt
  always runs, ``timeout=0`` included.
- After a failed attempt the poll sleeps
  ``min(interval * backoff ** (attempt - 1), remaining)``, and it raises once
  ``remaining`` reaches zero. The deadline is the only bound on a delay.
- ``ignore`` takes ``Exception`` subclasses only, so ``KeyboardInterrupt`` and
  every other ``BaseException`` leaves the loop, during an attempt and during
  the sleep alike.
- The loop keeps the last response, the last swallowed exception and the
  redacted request of the attempt that raised it, and nothing else per
  attempt.
- A swallowed exception is shown, and chained as the timeout's cause, only
  after it was checked against that request (``WaitTimeoutError``). An
  attempt that failed before any request existed has no such request, so its
  exception text is withheld.

The clock and the sleep are read through the private names ``_monotonic`` and
``_sleep``, so a test replaces them here without patching the shared ``time``
module, which the local servers in the test suite also use.
"""

from __future__ import annotations

import math
import numbers
import reprlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from graphql import GraphQLSchema

from pytest_graphql._core.errors import (
    ArgumentError,
    GraphQLExecutionError,
    OperationNotFoundError,
    WaitTimeoutError,
)
from pytest_graphql._core.operation import resolve_operation

if TYPE_CHECKING:
    from pytest_graphql._core.client import GraphQLClient
    from pytest_graphql._core.diagnostics import DiagnosticSnapshot
    from pytest_graphql._core.response.envelope import GraphQLResponse

_monotonic = time.monotonic
_sleep = time.sleep


class RequestTrace:
    """One slot the client fills when an attempt fails after its request exists.

    A response carries its own request, so an attempt that got one needs no
    trace. An attempt that raised before a response has only the client's
    request, and an exception text can hold that request's secrets. The slot
    holds the redacted request and nothing else, so it adds no state that
    grows with the number of attempts.
    """

    __slots__ = ("snapshot",)

    def __init__(self) -> None:
        self.snapshot: DiagnosticSnapshot | None = None


def _checked_number(value: object, option: str, *, minimum: float) -> float:
    """``value`` as a finite number of at least ``minimum``, or a refusal.

    Refused before any call is made. A boolean is not a number, and NaN and
    infinity are refused because the deadline arithmetic needs a real bound:
    an infinite timeout is a poll that can never end.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(
            f"wait_until {option}= must be a number, got {type(value).__name__}."
        )
    try:
        number = float(cast("float", value))
    except OverflowError:
        number = math.inf
    if not math.isfinite(number) or number < minimum:
        raise ValueError(
            f"wait_until {option}= must be a finite number of at least "
            f"{minimum:g}, got {reprlib.repr(value)}."
        )
    return number


def _checked_ignore(ignore: object) -> tuple[type[Exception], ...]:
    """The exception classes to swallow, or a ``TypeError`` naming the offender.

    Only ``Exception`` subclasses qualify. ``BaseException`` and its
    ``KeyboardInterrupt`` and ``SystemExit`` children are how a person or the
    runtime stops a test, so a poll loop never swallows them.
    """
    candidates = ignore if isinstance(ignore, tuple) else (ignore,)
    for each in candidates:
        if not (isinstance(each, type) and issubclass(each, Exception)):
            raise TypeError(
                "wait_until ignore= takes Exception subclasses only, got "
                f"{reprlib.repr(each)}. BaseException subclasses such as "
                "KeyboardInterrupt are never swallowed."
            )
    return cast("tuple[type[Exception], ...]", candidates)


def _require_query(schema: GraphQLSchema, name: str) -> None:
    """Refuse a name that is not a query, by looking it up in the schema.

    Only the schema decides. A query is polled whatever its name looks like,
    and a mutation or subscription is refused whatever its name looks like.
    A name that is a query as well as a mutation is polled as the query.
    """
    try:
        resolve_operation(schema, "query", name)
    except OperationNotFoundError as missing:
        for kind in ("mutation", "subscription"):
            try:
                resolve_operation(schema, kind, name)
            except OperationNotFoundError:
                continue
            raise ArgumentError.not_a_query(kind=kind, name=name) from None
        raise missing from None


def _next_delay(
    interval: float, backoff: float, attempt: int, remaining: float
) -> float:
    """``min(interval * backoff ** (attempt - 1), remaining)``, overflow-safe.

    A long poll with a small interval can reach an exponent that overflows a
    float. The delay is then larger than any deadline, so it is clamped to
    ``remaining``. Zero times an overflow is still zero.
    """
    try:
        delay = interval * backoff ** (attempt - 1)
    except OverflowError:
        delay = math.inf if interval > 0 else 0.0
    return min(delay, remaining)


def wait_until(
    client: GraphQLClient,
    name: str,
    /,
    *,
    until: Callable[[Any], Any],
    timeout: float = 30.0,
    interval: float = 1.0,
    backoff: float = 1.0,
    ignore: type[Exception] | tuple[type[Exception], ...] = (),
    **variables: Any,
) -> Any:
    """Run the query ``name`` until ``until(value)`` is truthy, and return ``value``.

    ``value`` is what ``client.query`` would return for the same arguments,
    or the whole response when ``raw=True``. Every other keyword is read as
    ``client.query`` reads it: a per-call option, or a variable. A variable
    named like one of this function's own options goes through
    ``variables={...}``. ``timeout`` here is the deadline of the whole poll,
    so the HTTP timeout of one attempt is the configured ``ClientConfig.timeout``.

    An exception listed in ``ignore`` is "not ready yet", whether the call or
    ``until`` raised it. Any other exception propagates at once.
    """
    limit = _checked_number(timeout, "timeout", minimum=0)
    first_delay = _checked_number(interval, "interval", minimum=0)
    factor = _checked_number(backoff, "backoff", minimum=1)
    swallow = _checked_ignore(ignore)
    if not callable(until):
        raise TypeError(
            f"wait_until until= must be callable, got {type(until).__name__}."
        )
    _require_query(client.schema, name)
    raw = bool(variables.get("raw", False))

    started = _monotonic()
    deadline = started + limit
    attempts = 0
    trace = RequestTrace()
    last_response: GraphQLResponse[Any] | None = None
    last_exception: Exception | None = None
    last_exception_request: DiagnosticSnapshot | None = None
    while True:
        attempts += 1
        trace.snapshot = None
        attempt_response: GraphQLResponse[Any] | None = None
        try:
            attempt_response = client._run_operation(
                "query", name, variables, trace=trace
            )
            last_response = attempt_response
            value = attempt_response if raw else attempt_response.unwrap()
            if until(value):
                return value
        except swallow as failure:
            last_exception = failure
            if (
                isinstance(failure, GraphQLExecutionError)
                and failure.response is not None
            ):
                attempt_response = last_response = failure.response
            # The request of this attempt, never an earlier one: a token can
            # change between attempts. None when no request existed yet.
            last_exception_request = (
                attempt_response.request
                if attempt_response is not None
                else trace.snapshot
            )
        # Outside the handler on purpose: nothing here can be swallowed, and
        # the next attempt does not run while an exception is being handled,
        # so a swallowed exception is never chained to the one before it.
        remaining = deadline - _monotonic()
        if remaining <= 0:
            error = WaitTimeoutError(
                operation=name,
                attempts=attempts,
                elapsed=_monotonic() - started,
                timeout=limit,
                last_response=last_response,
                last_exception=last_exception,
                last_exception_request=last_exception_request,
            )
            # A traceback prints the whole cause chain. Whether the exception
            # class belongs to the library says nothing about whether its text
            # was checked, so the error decides from the text itself.
            raise error from error.safe_cause()
        _sleep(_next_delay(first_delay, factor, attempts, remaining))
