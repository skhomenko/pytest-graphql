"""A poll timeout and a failed filter never print a request secret (M8 review).

The property: an exception text, and the exception itself as a cause, reach a
failure report only after they were checked against the redaction context of
the request attempt that produced them. Without that context the text is
withheld and the cause is not chained. The exception object stays available
on the error for a caller who wants it.

Each case plants one synthetic credential in the ``Authorization`` header,
makes the ignored exception echo it, and looks at every rendering a caller or
a log can reach.
"""

from __future__ import annotations

import dataclasses
import traceback
from typing import Any

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import polling
from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import (
    WITHHELD_TEXT,
    RequestInfo,
    exception_chain_is_clean,
)
from pytest_graphql._core.errors import (
    ExpectedErrorNotRaised,
    GraphQLExecutionError,
    GraphQLTestError,
    WaitTimeoutError,
)
from pytest_graphql._core.middleware import BaseMiddleware
from pytest_graphql._core.response import GraphQLResponse
from tests.unit.scripted_steps import (
    URL,
    FakeClock,
    ScriptedTransport,
    build_test_schema,
    failure,
    make_client,
    rejected,
    user,
)

_SECRET = "Bearer sk-live-SYNTHETIC-0123456789"
_HEADERS = {"Authorization": _SECRET}


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(polling, "_monotonic", fake.monotonic)
    monkeypatch.setattr(polling, "_sleep", fake.sleep)
    return fake


def _everything(error: BaseException) -> str:
    """Every text a caller or a log reaches from ``error``: its renderings,
    a full traceback, and the same for everything linked to it."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        texts += [
            str(current),
            repr(current),
            repr(current.args),
            "".join(traceback.format_exception(current)),
        ]
        pending += [
            linked
            for linked in (current.__cause__, current.__context__)
            if linked is not None
        ]
    return "\n".join(texts)


def _poll(client: GraphQLClient, **options: Any) -> WaitTimeoutError:
    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            timeout=0,
            until=options.pop("until", lambda _value: False),
            **options,
        )
    return caught.value


class _FailBefore(BaseMiddleware):
    def before_request(
        self,
        request: RequestInfo,  # noqa: ARG002 -- the overridable contract
    ) -> RequestInfo | None:
        raise ValueError(f"before_request saw {_SECRET}")


class _FailAfter(BaseMiddleware):
    def after_response(
        self,
        response: GraphQLResponse[Any],  # noqa: ARG002 -- the overridable contract
    ) -> GraphQLResponse[Any] | None:
        raise ValueError(f"after_response saw {_SECRET}")


class _FailingAuth:
    def apply(self, request: RequestInfo) -> RequestInfo:  # noqa: ARG002
        raise ValueError(f"auth saw {_SECRET}")


def _until_raises(_value: Any) -> bool:
    raise ValueError(f"until saw {_SECRET}")


def _client_for(where: str, schema: GraphQLSchema) -> GraphQLClient:
    if where == "transport":
        client, _ = make_client(
            schema, ValueError(f"transport saw {_SECRET}"), headers=_HEADERS
        )
    elif where == "before_request":
        client, _ = make_client(
            schema, user(), headers=_HEADERS, middleware=[_FailBefore()]
        )
    elif where == "after_response":
        client, _ = make_client(
            schema, user(), headers=_HEADERS, middleware=[_FailAfter()]
        )
    elif where == "auth":
        client = GraphQLClient(
            transport=ScriptedTransport(user()),
            schema=schema,
            config=ClientConfig(url=URL, headers=_HEADERS),
            auth=_FailingAuth(),
        )
    else:
        client, _ = make_client(schema, user(), headers=_HEADERS)
    return client


_PLACES = ["transport", "before_request", "after_response", "auth", "until"]


@pytest.mark.parametrize("where", _PLACES)
def test_an_ignored_exception_never_shows_the_secret(
    schema: GraphQLSchema, where: str
) -> None:
    client = _client_for(where, schema)
    options: dict[str, Any] = {"ignore": ValueError}
    if where == "until":
        options["until"] = _until_raises

    error = _poll(client, **options)

    assert _SECRET not in _everything(error)
    # The caller still gets the real exception, with its real text.
    assert isinstance(error.last_exception, ValueError)
    assert _SECRET in str(error.last_exception)


@pytest.mark.parametrize("where", ["transport", "before_request", "after_response"])
def test_a_failure_before_a_response_is_checked_against_its_own_request(
    schema: GraphQLSchema, where: str
) -> None:
    client = _client_for(where, schema)

    error = _poll(client, ignore=ValueError)

    assert error.last_response is None
    # The request existed, so the text was checked, found unsafe, and the
    # line was replaced by the withheld notice, not by the no-context one.
    assert f"  Last ignored exception: {WITHHELD_TEXT}\n" in str(error)
    assert "no request context" not in str(error)


def test_an_exception_with_no_request_context_is_withheld_whole(
    schema: GraphQLSchema,
) -> None:
    client = _client_for("auth", schema)

    error = _poll(client, ignore=ValueError)

    assert "Last ignored exception: ValueError: [withheld:" in str(error)
    assert "last_exception" in str(error)
    assert "auth saw" not in str(error)


def test_a_clean_exception_with_no_request_context_is_still_withheld(
    schema: GraphQLSchema,
) -> None:
    # The library cannot tell clean text from a secret without a request to
    # check it against, so it never guesses.
    class _PlainFailure:
        def apply(self, request: RequestInfo) -> RequestInfo:  # noqa: ARG002
            raise ValueError("plain")

    client = GraphQLClient(
        transport=ScriptedTransport(user()),
        schema=schema,
        config=ClientConfig(url=URL),
        auth=_PlainFailure(),
    )

    error = _poll(client, ignore=ValueError)

    assert "plain" not in str(error)
    assert isinstance(error.last_exception, ValueError)


_ROTATED = "Bearer sk-live-ROTATED-9876543210"


class _AuthOnSecondCall:
    """Fails the second call before any request exists, with a token that no
    earlier request held."""

    def __init__(self, error: Exception) -> None:
        self.calls = 0
        self._error = error

    def apply(self, request: RequestInfo) -> RequestInfo:
        self.calls += 1
        if self.calls == 2:
            raise self._error
        return request


def _poll_two_attempts(client: GraphQLClient, ignore: Any) -> WaitTimeoutError:
    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=lambda _value: False,
            ignore=ignore,
            timeout=1,
            interval=1,
        )
    return caught.value


def test_an_earlier_attempts_context_does_not_vouch_for_a_later_failure(
    schema: GraphQLSchema,
) -> None:
    # Attempt 1 returns a response. Attempt 2 fails before it has a request,
    # with a token attempt 1's request never held. Attempt 1's context cannot
    # check that token, so the text must be withheld whole.
    client = GraphQLClient(
        transport=ScriptedTransport(user()),
        schema=schema,
        config=ClientConfig(url=URL, headers=_HEADERS),
        auth=_AuthOnSecondCall(ValueError(f"second saw {_ROTATED}")),
    )

    error = _poll_two_attempts(client, ValueError)

    assert error.attempts == 2
    assert "[withheld: no request context" in str(error)
    assert _ROTATED not in _everything(error)


def test_an_earlier_attempts_context_does_not_vouch_for_a_later_cause(
    schema: GraphQLSchema,
) -> None:
    client = GraphQLClient(
        transport=ScriptedTransport(user()),
        schema=schema,
        config=ClientConfig(url=URL, headers=_HEADERS),
        auth=_AuthOnSecondCall(GraphQLTestError(f"second saw {_ROTATED}")),
    )

    error = _poll_two_attempts(client, GraphQLTestError)

    assert error.__cause__ is None
    assert _ROTATED not in _everything(error)


class _RotatingTransport:
    """Answers once, then fails every call echoing the header it was sent. A
    middleware changes that header on each call."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request: RequestInfo, *, timeout: float) -> Any:  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return user()
        raise ValueError(f"saw {request.headers['Authorization']}")

    def close(self) -> None:
        return None


class _Rotate(BaseMiddleware):
    def __init__(self) -> None:
        self.calls = 0

    def before_request(self, request: RequestInfo) -> RequestInfo | None:
        self.calls += 1
        token = f"Bearer sk-live-ROTATING-{self.calls}-0123456789"
        return dataclasses.replace(request, headers={"Authorization": token})


def test_the_failed_attempts_own_request_checks_its_text(
    schema: GraphQLSchema,
) -> None:
    # The token differs on every call. Only the request of the attempt that
    # failed knows the one the exception echoed.
    client = GraphQLClient(
        transport=_RotatingTransport(),
        schema=schema,
        config=ClientConfig(url=URL),
        middleware=[_Rotate()],
    )

    error = _poll_two_attempts(client, ValueError)

    assert error.attempts == 2
    assert error.last_response is not None
    assert f"Last ignored exception: {WITHHELD_TEXT}" in str(error)
    assert "ROTATING-2" not in _everything(error)


# -- the cause chain ---------------------------------------------------------


def test_a_user_built_test_error_with_a_secret_is_not_chained(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, user(), headers=_HEADERS)

    def until(_value: Any) -> bool:
        raise GraphQLTestError(f"mine {_SECRET}")

    error = _poll(client, until=until, ignore=GraphQLTestError)

    assert error.__cause__ is None
    assert _SECRET not in _everything(error)
    assert isinstance(error.last_exception, GraphQLTestError)


def test_a_clean_user_built_test_error_is_chained(schema: GraphQLSchema) -> None:
    client, _ = make_client(schema, user(), headers=_HEADERS)

    def until(_value: Any) -> bool:
        raise GraphQLTestError("not ready")

    error = _poll(client, until=until, ignore=GraphQLTestError)

    assert error.__cause__ is error.last_exception


class _GroupedError(GraphQLTestError):
    """Carries other exceptions the way an exception group does."""

    def __init__(self, message: str, members: tuple[BaseException, ...]) -> None:
        super().__init__(message)
        self.exceptions = members


@pytest.mark.parametrize("link", ["cause", "context", "note", "member"])
def test_a_secret_anywhere_in_the_chain_stops_the_chaining(
    schema: GraphQLSchema, link: str
) -> None:
    client, _ = make_client(schema, user(), headers=_HEADERS)

    def until(_value: Any) -> bool:
        hidden = RuntimeError(f"hidden {_SECRET}")
        if link == "member":
            raise _GroupedError("clean on its face", (hidden,))
        outer = GraphQLTestError("clean on its face")
        if link == "cause":
            raise outer from hidden
        if link == "note":
            outer.__notes__ = [f"note {_SECRET}"]  # add_note needs Python 3.11
            raise outer
        try:
            raise hidden
        except RuntimeError:
            raise outer from None

    error = _poll(client, until=until, ignore=GraphQLTestError)

    assert error.__cause__ is None
    assert _SECRET not in str(error)
    assert _SECRET not in "".join(traceback.format_exception(error))


def test_a_library_error_with_its_own_response_is_still_chained(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(schema, rejected(failure("x")), headers=_HEADERS)

    error = _poll(client, ignore=GraphQLExecutionError)

    assert error.__cause__ is error.last_exception


# -- expect_error ------------------------------------------------------------


def test_a_failed_filter_does_not_chain_an_error_whose_cause_has_a_secret(
    schema: GraphQLSchema,
) -> None:
    client, _ = make_client(
        schema, rejected(failure("denied", code="A")), headers=_HEADERS
    )

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="NOPE"),
    ):
        try:
            client.query("user", id="u1", fields=["id"])
        except GraphQLExecutionError as raised:
            raise raised from RuntimeError(f"hidden {_SECRET}")

    assert caught.value.__cause__ is None
    assert _SECRET not in "".join(traceback.format_exception(caught.value))


def test_a_failed_filter_still_chains_a_clean_error(schema: GraphQLSchema) -> None:
    client, _ = make_client(
        schema, rejected(failure("denied", code="A")), headers=_HEADERS
    )

    with (
        pytest.raises(ExpectedErrorNotRaised) as caught,
        client.expect_error(code="NOPE"),
    ):
        client.query("user", id="u1", fields=["id"])

    assert isinstance(caught.value.__cause__, GraphQLExecutionError)


# -- the chain check itself --------------------------------------------------


@pytest.fixture
def snapshot(schema: GraphQLSchema) -> Any:
    client, _ = make_client(schema, user(), headers=_HEADERS)
    return client.query("user", id="u1", raw=True).request


def test_an_empty_context_vouches_for_nothing(snapshot: Any) -> None:
    assert exception_chain_is_clean(ValueError("plain"), ()) is False
    assert exception_chain_is_clean(ValueError("plain"), (snapshot,)) is True


def test_a_clean_chain_with_a_cycle_is_walked_once(snapshot: Any) -> None:
    first, second = ValueError("one"), ValueError("two")
    first.__cause__ = second
    second.__context__ = first

    assert exception_chain_is_clean(first, (snapshot,)) is True


def test_a_chain_longer_than_the_bound_is_not_vouched_for(snapshot: Any) -> None:
    head = tail = ValueError("0")
    for number in range(1, 200):
        nxt = ValueError(str(number))
        tail.__cause__ = nxt
        tail = nxt

    assert exception_chain_is_clean(head, (snapshot,)) is False


def test_an_unreadable_message_is_not_vouched_for(snapshot: Any) -> None:
    class _UnreadableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    assert exception_chain_is_clean(_UnreadableError(), (snapshot,)) is False


def test_the_check_changes_no_exception(snapshot: Any) -> None:
    error = GraphQLTestError(f"mine {_SECRET}")

    assert exception_chain_is_clean(error, (snapshot,)) is False
    assert error.args == (f"mine {_SECRET}",)


def test_the_poll_keeps_one_request_slot_however_long_it_runs(
    schema: GraphQLSchema,
) -> None:
    client, transport = make_client(schema, ValueError("down"), headers=_HEADERS)

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_until(
            "user",
            id="u1",
            until=lambda _value: False,
            ignore=ValueError,
            timeout=500,
            interval=1,
        )

    assert caught.value.attempts == len(transport.sent) > 100
    assert caught.value.last_exception_request is not None
