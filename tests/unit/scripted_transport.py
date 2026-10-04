"""Helpers for the plugin reporting tests: a scripted transport and two documents.

``ScriptedTransport`` returns the outcomes a test queued, one per call, and
repeats the last one when the queue runs out. An outcome is a ``RawResponse`` or
an exception to raise. The transport implements ``send()`` and ``close()`` and
nothing else, so a client over it shares it, as a plain two-method transport
does (C19).

The inner pytest sessions of the plugin tests import this module, because
``pytester.runpytest()`` runs the inner session in this process.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.transport.base import RawResponse
from tests.unit import plugin_probe
from tests.unit.plugin_inner import QUIET_INNER

USER_QUERY = "query user($id: ID!) { user(id: $id) { id name } }"
UPDATE_MUTATION = (
    "mutation updateUser($id: ID!, $name: String) "
    "{ updateUser(id: $id, name: $name) { id name } }"
)


def envelope(
    data: Any = None,
    errors: tuple[Mapping[str, Any], ...] = (),
    *,
    status: int = 200,
) -> RawResponse:
    return RawResponse(
        status_code=status,
        media_type="application/graphql-response+json",
        data=data,
        errors=tuple(errors),
        extensions=None,
        headers={},
    )


class ScriptedTransport:
    """Returns or raises the queued outcomes, in order, and records each request."""

    def __init__(
        self,
        *outcomes: RawResponse | BaseException,
        routes: Mapping[str, RawResponse | BaseException] | None = None,
    ) -> None:
        self._queue: deque[RawResponse | BaseException] = deque(outcomes)
        self._last: RawResponse | BaseException | None = None
        #: An outcome by operation name, which wins over the queue.
        self._routes = dict(routes or {})
        self.sent: list[RequestInfo] = []
        self.close_calls = 0

    def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002
        self.sent.append(request)
        routed = self._routes.get(request.operation or "")
        if routed is not None:
            if isinstance(routed, BaseException):
                raise routed
            return routed
        if self._queue:
            self._last = self._queue.popleft()
        outcome = self._last
        if outcome is None:
            raise AssertionError("the scripted transport has no outcome queued")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.close_calls += 1


def user_data(**overrides: Any) -> dict[str, Any]:
    user: dict[str, Any] = {"id": "123", "name": "John"}
    user.update(overrides)
    return {"user": user}


SCRIPTED_CONFTEST = """
import pytest

from pytest_graphql import HeaderAuth
from tests.schema.resolvers import build_schema
from tests.unit import plugin_probe
from tests.unit.scripted_transport import (
    UPDATE_MUTATION,
    USER_QUERY,
    ScriptedTransport,
    envelope,
    user_data,
)

ROUTES = {routes}


@pytest.fixture(scope="session")
def gql_schema():
    return build_schema()


@pytest.fixture(scope="session")
def gql_transport():
    return ScriptedTransport(routes=ROUTES)


@pytest.fixture(scope="session")
def gql_url():
    return "http://127.0.0.1:9/graphql"


def pytest_runtest_logreport(report):
    plugin_probe.EVENTS.append(
        (
            "report",
            report.nodeid,
            report.when,
            report.outcome,
            list(report.sections),
            report.longreprtext if report.failed else "",
        )
    )
"""

#: The documents an inner test file uses, so each test can name them.
INNER_IMPORTS = """
from tests.unit.scripted_transport import UPDATE_MUTATION, USER_QUERY
"""

#: Two calls, the second of which the server refuses, as in SPEC 7.5.
SPEC_ROUTES = """{
    "user": envelope(user_data()),
    "updateUser": envelope(
        None,
        ({
            "message": "name already taken",
            "path": ["updateUser"],
            "extensions": {"code": "CONFLICT"},
        },),
    ),
}"""


def run_scripted(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    test: str,
    *,
    routes: str = SPEC_ROUTES,
    conftest: str = "",
    ini: str = "",
    env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
) -> pytest.RunResult:
    """One inner session over a scripted transport and the hostile schema.

    Each report is recorded as an ``"report"`` event, so a test reads the
    sections a test's report carries without parsing the terminal.
    """
    plugin_probe.EVENTS.clear()
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    pytester.makeconftest(SCRIPTED_CONFTEST.format(routes=routes) + "\n" + conftest)
    if ini:
        pytester.makeini("[pytest]\n" + ini)
    pytester.makepyfile(test_inner=INNER_IMPORTS + test)
    return pytester.runpytest(*QUIET_INNER, *args)


def reports(nodeid_part: str = "", when: str | None = None) -> list[tuple[Any, ...]]:
    """The recorded reports whose node id holds ``nodeid_part``."""
    return [
        event
        for event in plugin_probe.EVENTS
        if event[0] == "report"
        and nodeid_part in event[1]
        and (when is None or event[2] == when)
    ]


def sections_of(nodeid_part: str, when: str = "call") -> dict[str, str]:
    """The sections of one report, by title."""
    found = reports(nodeid_part, when)
    assert found, f"no {when} report for {nodeid_part!r}"
    return dict(found[-1][4])
