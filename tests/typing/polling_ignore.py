"""M8: ``wait_until(ignore=...)`` takes ``Exception`` subclasses, statically too.

The runtime refuses a ``BaseException`` class with a ``TypeError``. The
signature says the same, so the strict check rejects it where it is written.
``expect_error`` is a context manager that yields ``CapturedErrors``.

This file is checked by ``tests/typing/test_typing_fixtures.py`` and never
run.
"""

from __future__ import annotations

from pytest_graphql import GraphQLClient, GraphQLExecutionError
from pytest_graphql._core.expect_error import CapturedErrors


def poll(client: GraphQLClient) -> None:
    client.wait_until("order", until=bool, ignore=ValueError)
    client.wait_until("order", until=bool, ignore=(ValueError, GraphQLExecutionError))
    client.wait_until("order", until=bool, ignore=KeyboardInterrupt)  # expect: arg-type
    client.wait_until("order", until=bool, ignore=BaseException)  # expect: arg-type
    client.wait_until(
        "order",
        until=bool,
        ignore=(ValueError, SystemExit),  # expect: arg-type
    )


def expect(client: GraphQLClient) -> None:
    with client.expect_error(code="FORBIDDEN") as captured:
        reveal_type(captured)  # reveal: CapturedErrors
    reveal_type(captured.first.code)  # reveal: str | None
    assert isinstance(captured, CapturedErrors)
