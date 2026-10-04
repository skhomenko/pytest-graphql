"""``expect_error`` and ``CapturedErrors`` (SPEC 3.8, M8).

``with gql.expect_error(...)`` asserts that a block ends in a
``GraphQLExecutionError`` and that every supplied filter matches at least one
of the response's errors. Anything else a block does is not this module's
business: another exception, a ``BaseException`` included, leaves the block
unchanged.

The block's responses are seen through a context variable rather than through
the client that opened the block, so a call made through a clone
(``gql.as_(...)``) counts too. The variable holds the active blocks, and each
block keeps only its last response and a call count.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from pytest_graphql._core.diagnostics import exception_chain_is_clean
from pytest_graphql._core.errors import (
    ExpectedErrorNotRaised,
    GraphQLExecutionError,
    GraphQLTestError,
)
from pytest_graphql._core.matching.values import Matches, matches

if TYPE_CHECKING:
    from pytest_graphql._core.response.envelope import GraphQLResponse
    from pytest_graphql._core.response.types import GraphQLErrorInfo


class CapturedErrors:
    """What an ``expect_error`` block caught, readable once the block ends."""

    __slots__ = ("_response",)

    def __init__(self) -> None:
        self._response: GraphQLResponse[Any] | None = None

    def _fill(self, response: GraphQLResponse[Any]) -> None:
        self._response = response

    def _filled(self) -> GraphQLResponse[Any]:
        if self._response is None:
            raise GraphQLTestError(
                "nothing is captured yet: read errors, response and first "
                "after the with block, not inside it."
            )
        return self._response

    @property
    def response(self) -> GraphQLResponse[Any]:
        """The response whose errors were raised."""
        return self._filled()

    @property
    def errors(self) -> tuple[GraphQLErrorInfo, ...]:
        """Every error the server returned, whatever the filters were."""
        return self._filled().errors

    @property
    def first(self) -> GraphQLErrorInfo:
        """The first error the server returned."""
        return self._filled().errors[0]

    def __repr__(self) -> str:
        # A count only. The errors hold server text, which no repr shows.
        count = 0 if self._response is None else len(self._response.errors)
        return f"CapturedErrors(errors={count})"


class _Watch:
    """One open block's view of the responses made while it was open."""

    __slots__ = ("calls", "last")

    def __init__(self) -> None:
        self.calls = 0
        self.last: GraphQLResponse[Any] | None = None


_ACTIVE: ContextVar[tuple[_Watch, ...]] = ContextVar(
    "pytest_graphql_expect_error", default=()
)


def observe_response(response: GraphQLResponse[Any]) -> None:
    """Tell every open block in this context that a response arrived.

    Called by the client for every response it builds, whether or not the
    client then raises, so a block that never sees an error can still name
    the response it did see.
    """
    for watch in _ACTIVE.get():
        watch.calls += 1
        watch.last = response


def _checked_path(path: object) -> tuple[str | int, ...]:
    if isinstance(path, Sequence) and not isinstance(path, (str, bytes)):
        segments = tuple(path)
        if all(
            isinstance(each, (str, int)) and not isinstance(each, bool)
            for each in segments
        ):
            return cast("tuple[str | int, ...]", segments)
    raise TypeError(
        "expect_error(path=...) takes a list or tuple of field names and "
        f"list indexes, got {path!r}."
    )


def _same_path(
    actual: tuple[str | int, ...] | None, expected: tuple[str | int, ...]
) -> bool:
    """Equal segment by segment, and a name never equals an index."""
    return (
        actual is not None
        and len(actual) == len(expected)
        and all(
            type(a) is type(e) and a == e for a, e in zip(actual, expected, strict=True)
        )
    )


@dataclass(frozen=True)
class _Filters:
    """The filters one block was given, each checked on its own.

    Every supplied filter must match at least one error. They need not match
    the same one, which is the rule SPEC 3.8 states. ``count`` is not a
    matching filter: it is an exact assertion on the total number of errors
    the server returned, whatever the other filters matched.
    """

    code: str | None
    path: tuple[str | int, ...] | None
    message: Matches | None
    message_text: str
    count: int | None = None

    def unmatched(self, errors: Sequence[GraphQLErrorInfo]) -> list[tuple[str, str]]:
        """The filters no error satisfied, as ``(name, text of its value)``."""
        missing: list[tuple[str, str]] = []
        if self.code is not None and not any(e.code == self.code for e in errors):
            missing.append(("code", repr(self.code)))
        if self.path is not None and not any(
            _same_path(e.path, self.path) for e in errors
        ):
            missing.append(("path", repr(list(self.path))))
        if self.message is not None and not any(
            self.message == e.message for e in errors
        ):
            missing.append(("message_matches", self.message_text))
        if self.count is not None and len(errors) != self.count:
            missing.append(("count", repr(self.count)))
        return missing


class ExpectedError(AbstractContextManager[CapturedErrors]):
    """The context manager ``GraphQLClient.expect_error`` returns.

    The arguments are checked when it is built, so a bad filter fails at the
    call and not after the block has run.
    """

    def __init__(
        self,
        *,
        code: str | None = None,
        path: Sequence[str | int] | None = None,
        message_matches: str | re.Pattern[str] | None = None,
        count: int | None = None,
    ) -> None:
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int):
                raise TypeError(
                    "expect_error(count=...) takes an integer, got "
                    f"{type(count).__name__}."
                )
            if count < 1:
                raise ValueError(
                    "expect_error(count=...) must be at least 1, because a "
                    f"block that raises has at least one error, got {count}."
                )
        if code is not None and not isinstance(code, str):
            raise TypeError(
                f"expect_error(code=...) takes a string, got {type(code).__name__}."
            )
        message = None if message_matches is None else matches(message_matches)
        text = (
            ""
            if message_matches is None
            else repr(
                message_matches.pattern
                if isinstance(message_matches, re.Pattern)
                else message_matches
            )
        )
        self._filters = _Filters(
            code=code,
            path=None if path is None else _checked_path(path),
            message=message,
            message_text=text,
            count=count,
        )
        self._open: list[tuple[_Watch, CapturedErrors, Token[tuple[_Watch, ...]]]] = []

    def __enter__(self) -> CapturedErrors:
        watch = _Watch()
        captured = CapturedErrors()
        token = _ACTIVE.set((*_ACTIVE.get(), watch))
        self._open.append((watch, captured, token))
        return captured

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        watch, captured, token = self._open.pop()
        _ACTIVE.reset(token)
        if exc is None:
            raise ExpectedErrorNotRaised.no_error(
                response=watch.last, calls=watch.calls
            )
        # A protocol violation is a GraphQLExecutionError with no errors at
        # all. Capturing it would let a broken server satisfy this block.
        # An instance a caller built without a response has nothing to capture.
        if (
            not isinstance(exc, GraphQLExecutionError)
            or exc.response is None
            or not exc.errors
        ):
            return False
        captured._fill(exc.response)
        missing = self._filters.unmatched(exc.errors)
        if missing:
            # A traceback prints everything linked to the cause. The error
            # was checked against its own response, and what a caller linked
            # to it was not, so the whole chain is checked before it is shown.
            cause = (
                exc if exception_chain_is_clean(exc, (exc.response.request,)) else None
            )
            raise ExpectedErrorNotRaised.unmatched_filters(
                response=exc.response, filters=missing, calls=watch.calls
            ) from cause
        return True
