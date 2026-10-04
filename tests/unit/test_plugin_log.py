"""``--gql-log`` and ``--gql-log-level`` (SPEC 7.3, DESIGN section 7).

With ``--gql-log`` every call is logged through ``logging``, once it is recorded,
from the recorded snapshot only. ``summary`` is one line. ``full`` adds the
document, the variables, the skipped fields, the data and the errors, and leaves
out the ``curl`` command, which belongs to the report of a failure.
"""

from __future__ import annotations

import logging
import re

import pytest

from pytest_graphql.plugin import reporting
from tests.unit import plugin_probe
from tests.unit.scripted_transport import SPEC_ROUTES, run_scripted

CALLS = """
import logging
import pytest


def test_calls(gql, caplog):
    gql.execute(USER_QUERY, {"id": "123"})
    gql.execute(UPDATE_MUTATION, {"id": "123", "name": "New"}, raise_on_error=False)
    plugin_probe.EVENTS.append(
        (
            "records",
            [
                (record.name, record.levelno, record.getMessage())
                for record in caplog.records
                if record.name == "pytest_graphql.calls"
            ],
        )
    )
"""

PROBE = "from tests.unit import plugin_probe\n"


def records() -> list[tuple[str, int, str]]:
    found = [event for event in plugin_probe.EVENTS if event[0] == "records"]
    assert len(found) == 1
    result: list[tuple[str, int, str]] = found[0][1]
    return result


def calm(text: str) -> str:
    return re.sub(r"\d+\.\d ms", "N ms", text)


def test_nothing_is_logged_without_the_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, PROBE + CALLS)
    result.assert_outcomes(passed=1)
    assert records() == []


def test_the_summary_is_one_info_line_for_each_call(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, PROBE + CALLS, args=("--gql-log",))
    result.assert_outcomes(passed=1)
    assert [(name, level, calm(text)) for name, level, text in records()] == [
        (
            "pytest_graphql.calls",
            logging.INFO,
            "query user POST http://127.0.0.1:9/graphql -> ok "
            "(status 200, N ms, 0 error(s))",
        ),
        (
            "pytest_graphql.calls",
            logging.INFO,
            "mutation updateUser POST http://127.0.0.1:9/graphql -> errors "
            "(status 200, N ms, 1 error(s))",
        ),
    ]


def test_the_default_level_is_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, PROBE + CALLS, args=("--gql-log",))
    assert all("\n" not in text for _, _, text in records())


def test_the_full_level_adds_the_body_and_never_the_curl_command(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        PROBE + CALLS,
        args=("--gql-log", "--gql-log-level=full"),
    )
    first, second = (text for _, _, text in records())
    assert calm(first).splitlines() == [
        "query user POST http://127.0.0.1:9/graphql -> ok "
        "(status 200, N ms, 0 error(s))",
        "    query user($id: ID!) { user(id: $id) { id name } }",
        '    variables: {"id": "123"}',
        '    data: {"user": {"id": "123", "name": "John"}}',
    ]
    assert '      - CONFLICT at ["updateUser"]: name already taken' in second
    assert "    errors:" in second
    for text in (first, second):
        assert "reproduce" not in text
        assert "curl" not in text
        assert "FAILED HERE" not in text


def test_the_level_alone_logs_nothing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, PROBE + CALLS, args=("--gql-log-level=full",))
    assert records() == []


def test_a_call_that_failed_in_the_transport_is_logged(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        PROBE
        + """
import pytest


def test_calls(gql, caplog):
    with pytest.raises(ConnectionError):
        gql.execute(USER_QUERY, {"id": "1"})
    plugin_probe.EVENTS.append(
        (
            "records",
            [
                (r.name, r.levelno, r.getMessage())
                for r in caplog.records
                if r.name == "pytest_graphql.calls"
            ],
        )
    )
""",
        routes='{"user": ConnectionError("refused")}',
        args=("--gql-log", "--gql-log-level=full"),
    )
    [(_, _, text)] = records()
    assert calm(text).splitlines()[0] == (
        "query user POST http://127.0.0.1:9/graphql -> failed "
        "(status -, N ms, 0 error(s))"
    )
    assert "    failure: ConnectionError: refused" in text


def test_a_passing_tests_log_shows_with_the_captured_log_option(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        "def test_passes(gql):\n    gql.execute(USER_QUERY, {'id': '1'})\n",
        routes=SPEC_ROUTES,
        args=("--gql-log", "-rP"),
    )
    result.stdout.fnmatch_lines(
        [
            "*Captured log call*",
            "INFO *pytest_graphql.calls:*query user POST http://127.0.0.1:9/graphql "
            "-> ok (status 200, *",
        ]
    )


def test_a_failure_in_the_log_never_changes_the_result_of_the_call(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> str:  # noqa: ARG001
        raise RuntimeError("log failed")

    monkeypatch.setattr(reporting, "log_message", broken)
    result = run_scripted(pytester, monkeypatch, PROBE + CALLS, args=("--gql-log",))
    result.assert_outcomes(passed=1)
    assert records() == []


def test_the_call_logger_is_left_as_it_was_when_the_session_ends(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger(reporting.LOGGER_NAME)
    before = logger.level
    run_scripted(pytester, monkeypatch, PROBE + CALLS, args=("--gql-log",))
    assert logger.level == before


def test_the_call_logger_is_not_touched_without_the_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger(reporting.LOGGER_NAME)
    monkeypatch.setattr(logger, "level", logging.ERROR)
    run_scripted(
        pytester,
        monkeypatch,
        PROBE
        + """
import logging


def test_level(gql):
    plugin_probe.EVENTS.append(
        ("level", logging.getLogger("pytest_graphql.calls").level)
    )
""",
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "level"] == [
        ("level", logging.ERROR)
    ]
