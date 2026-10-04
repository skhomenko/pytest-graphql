"""The M8 failure texts leak no credential (DESIGN_DECISIONS.md section 7).

``ExpectedErrorNotRaised``, the unmatched-filter failure and
``WaitTimeoutError`` render server responses and server errors. The same
adversarial server as the C2/C16 leak suite reflects the whole request into
an error message, an error path and an extension, and a credential is planted
in a header, a top-level variable, a nested input object, a list element and
the query string. Each failure must show none of them in any rendering a
caller or a log can reach: ``str``, ``repr``, ``args``, the last line of a
traceback, the attributes that hold server errors, and the response.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from graphql import GraphQLSchema, build_schema

from pytest_graphql import GraphQLClient
from pytest_graphql._core.diagnostics import WITHHELD_TEXT
from pytest_graphql._core.errors import (
    ExpectedErrorNotRaised,
    GraphQLExecutionError,
    WaitTimeoutError,
)
from tests.unit.local_http_server import local_server
from tests.unit.test_leak_suite import (
    _API_KEY,
    _HEADER,
    _LIST_FIRST,
    _LIST_SECOND,
    _NESTED,
    _PLANTED,
    _QUERY,
    _SDL,
    _TOP_LEVEL,
    _Capture,
    _client,
    _exception_texts,
    _login,
    _reachable,
    _Reflecting,
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from pytest_graphql._core import polling

    def refuse(seconds: float) -> None:
        raise AssertionError(f"a test slept for {seconds} s in real time")

    monkeypatch.setattr(polling, "_sleep", refuse)


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema(_SDL)


def _texts(error: BaseException) -> str:
    """Every rendering of ``error``, its chain and the objects it carries."""
    parts: list[str] = []
    for each in _reachable(error):
        parts += _exception_texts(each)
        parts.append("".join(traceback.format_exception(each)))
        for name in ("response", "last_response"):
            carried = getattr(each, name, None)
            if carried is not None:
                parts += [repr(carried), str(carried), repr(carried.errors)]
        for info in getattr(each, "errors", ()):
            parts.append(repr(info))
    return "\n".join(parts)


def _assert_clean(text: str) -> None:
    leaked = [secret for secret in _PLANTED if secret in text]
    assert not leaked, f"credential(s) reached a rendering: {leaked}"


@pytest.fixture
def served(schema: GraphQLSchema) -> Iterator[Callable[..., GraphQLClient]]:
    clients: list[GraphQLClient] = []
    stack: list[Any] = []

    def make(shape: str, **config: Any) -> GraphQLClient:
        manager = local_server(_Reflecting(shape))
        url = manager.__enter__()
        stack.append(manager)
        client = _client(url, schema, _Capture(), **config)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()
    for manager in reversed(stack):
        manager.__exit__(None, None, None)


def test_an_unmatched_filter_failure_shows_the_reflection_scrubbed(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("execution")

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="NOPE"),
    ):
        _login(client)

    text = _texts(caught.value)
    assert "REFLECTED body=" in str(caught.value)
    assert "[redacted:" in str(caught.value)
    _assert_clean(text)


def test_a_partial_data_response_is_scrubbed_in_an_unmatched_failure(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("partial")

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(message_matches="never present"),
    ):
        _login(client)

    assert "[redacted:" in str(caught.value)
    _assert_clean(_texts(caught.value))


def test_a_not_raised_failure_shows_the_response_without_a_credential(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("data")

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        _login(client)

    assert "[redacted:" in str(caught.value)
    _assert_clean(_texts(caught.value))


def test_an_errors_response_the_client_let_through_is_scrubbed(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("execution", raise_on_error=False)

    with pytest.raises(ExpectedErrorNotRaised) as caught, client.expect_error():
        _login(client)

    _assert_clean(_texts(caught.value))


def _poll(client: GraphQLClient, **options: Any) -> Any:
    # `login` is a mutation in the leak schema, so the poll uses the query.
    # `raw` hands `until` the response, so no unwrap can fail on the
    # reflecting server's data.
    return client.wait_until(
        "ping",
        raw=True,
        until=options.pop("until", lambda _response: False),
        ignore=options.pop("ignore", GraphQLExecutionError),
        timeout=0,
        **options,
    )


@pytest.mark.parametrize("shape", ["execution", "partial", "data"])
def test_a_wait_timeout_shows_no_credential(
    served: Callable[..., GraphQLClient], shape: str
) -> None:
    client = served(shape)

    with pytest.raises(WaitTimeoutError) as caught:
        _poll(client)

    assert caught.value.attempts == 1
    assert "[redacted:" in str(caught.value)
    _assert_clean(_texts(caught.value))


def test_a_credential_in_the_last_ignored_exception_text_is_withheld(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("data")

    def until(_value: Any) -> bool:
        raise RuntimeError(f"the header was {_HEADER}")

    with pytest.raises(WaitTimeoutError) as caught:
        _poll(client, until=until, ignore=RuntimeError)

    assert WITHHELD_TEXT in str(caught.value)
    assert caught.value.__cause__ is None
    _assert_clean(_texts(caught.value))
    # The attribute holds the real exception for a caller who wants it.
    assert isinstance(caught.value.last_exception, RuntimeError)


def test_a_credential_in_a_filter_the_author_wrote_is_withheld(
    served: Callable[..., GraphQLClient],
) -> None:
    client = served("execution")

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(message_matches=f"no-match-{_API_KEY}"),
    ):
        _login(client)

    assert f"Unmatched filters: {WITHHELD_TEXT}" in str(caught.value)
    _assert_clean(_texts(caught.value))


def test_the_planted_credentials_are_all_distinct_and_long_enough() -> None:
    # A planted value shorter than the minimum, or one that contains another,
    # would let an absence assertion above pass for the wrong reason.
    assert len(set(_PLANTED)) == len(_PLANTED) == 7
    assert all(len(secret) >= 8 for secret in _PLANTED)
    assert {
        _HEADER,
        _API_KEY,
        _TOP_LEVEL,
        _NESTED,
        _LIST_FIRST,
        _LIST_SECOND,
        _QUERY,
    } == set(_PLANTED)
