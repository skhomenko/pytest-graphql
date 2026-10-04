"""The failure section in a real pytest session (SPEC 7.5, DESIGN section 3).

A failed test that made at least one GraphQL call carries a ``GraphQL calls``
section in its report, built from the calls its client recorded. These sessions
pin what the section holds, which tests get one, and that the state behind it is
bounded and released.
"""

from __future__ import annotations

import re

import pytest

from pytest_graphql.plugin import reporting
from tests.unit import plugin_probe
from tests.unit.m9b_support import reports, run_scripted, sections_of

TWO_CALLS = """
def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "123"})
    gql.execute(UPDATE_MUTATION, {"id": "123", "name": "New"}, raise_on_error=False)
    assert False, "boom"
"""

#: The one thing the section shows that differs between runs.
DURATION = re.compile(r"\d+ms")


def calm(text: str) -> str:
    return DURATION.sub("Nms", text)


def test_a_failed_test_carries_the_calls_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, TWO_CALLS)
    result.assert_outcomes(failed=1)
    sections = sections_of("test_fails")
    assert list(sections) == ["GraphQL calls (2)"]


def test_the_section_is_the_spec_7_5_layout(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, TWO_CALLS)
    text = calm(sections_of("test_fails")["GraphQL calls (2)"])
    lines = text.splitlines()
    assert lines[:5] == [
        "[1] query user  200  Nms",
        "    query user($id: ID!) { user(id: $id) { id name } }",
        '    variables: {"id": "123"}',
        '    data: {"user": {"id": "123", "name": "John"}}',
        "",
    ]
    assert lines[5:12] == [
        "[2] mutation updateUser  200  Nms   <-- FAILED HERE",
        "    mutation updateUser($id: ID!, $name: String) "
        "{ updateUser(id: $id, name: $name) { id name } }",
        '    variables: {"id": "123", "name": "New"}',
        "    errors:",
        '      - CONFLICT at ["updateUser"]: name already taken',
        "    reproduce:",
        lines[11],
    ]
    curl = lines[11].strip()
    assert curl.startswith("curl -sS -X POST http://127.0.0.1:9/graphql --data ")
    assert len(lines) == 12


def test_the_terminal_shows_the_section_under_a_rule_with_its_title(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, TWO_CALLS)
    result.stdout.fnmatch_lines(
        [
            "*- GraphQL calls (2) -*",
            "[[]1[]] query user  200  *ms",
            "*[[]2[]] mutation updateUser  200  *ms   <-- FAILED HERE",
        ]
    )


def test_a_passing_test_carries_no_graphql_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        'def test_passes(gql):\n    gql.execute(USER_QUERY, {"id": "1"})\n',
    )
    assert sections_of("test_passes") == {}


def test_a_failed_test_that_made_no_call_carries_no_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, "def test_fails(gql):\n    assert False\n")
    assert sections_of("test_fails") == {}


def test_a_failed_test_that_does_not_use_the_client_carries_no_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, "def test_fails():\n    assert False\n")
    assert sections_of("test_fails") == {}


def test_a_call_that_failed_in_the_transport_is_shown_with_its_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
import pytest


def test_fails(gql):
    with pytest.raises(ConnectionError):
        gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        routes='{"user": ConnectionError("refused")}',
    )
    text = calm(sections_of("test_fails")["GraphQL calls (1)"])
    assert text.splitlines()[0] == "[1] query user  -  Nms   <-- FAILED HERE"
    assert "    failure: ConnectionError: refused" in text
    assert "    reproduce:" in text


def test_a_setup_error_after_a_call_carries_the_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
import pytest


@pytest.fixture
def seeded(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    raise RuntimeError("seed failed")


def test_uses_it(seeded):
    pass
""",
    )
    assert "GraphQL calls (1)" in sections_of("test_uses_it", "setup")


def test_a_teardown_error_after_a_call_carries_the_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
import pytest


@pytest.fixture
def cleanup(gql):
    yield
    gql.execute(USER_QUERY, {"id": "1"})
    raise RuntimeError("cleanup failed")


def test_uses_it(cleanup):
    pass
""",
    )
    assert sections_of("test_uses_it", "call") == {}
    assert "GraphQL calls (1)" in sections_of("test_uses_it", "teardown")


def test_the_recorder_bound_decides_how_many_calls_the_section_shows(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
def test_fails(gql):
    for _ in range(10):
        gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        conftest="""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=3)
""",
    )
    sections = sections_of("test_fails")
    assert list(sections) == ["GraphQL calls (last 3 of 10)"]
    numbers = re.findall(r"^\[(\d+)\]", sections["GraphQL calls (last 3 of 10)"], re.M)
    assert numbers == ["8", "9", "10"]


def test_the_trace_is_released_when_the_test_ends(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slot holds the trace of the running test and nothing else: the next
    # test starts with it empty, and so does the session's end.
    run_scripted(
        pytester,
        monkeypatch,
        """
from pytest_graphql.plugin.reporting import CURRENT
from tests.unit import plugin_probe


def test_a_uses_the_client(gql, request):
    gql.execute(USER_QUERY, {"id": "1"})
    plugin_probe.EVENTS.append(("slot", "a", request.config.stash.get(CURRENT, None)))


def test_b_starts_empty(request):
    plugin_probe.EVENTS.append(("slot", "b", request.config.stash.get(CURRENT, None)))


def test_c_uses_the_client_again(gql, request):
    plugin_probe.EVENTS.append(("slot", "c", request.config.stash.get(CURRENT, None)))
""",
        conftest="""
def pytest_sessionfinish(session):
    from pytest_graphql.plugin.reporting import CURRENT

    plugin_probe.EVENTS.append(("slot", "end", session.config.stash.get(CURRENT, None)))
""",
    )
    slots = {event[1]: event[2] for event in plugin_probe.EVENTS if event[0] == "slot"}
    assert slots["a"] is not None
    assert slots["a"].nodeid.endswith("test_a_uses_the_client")
    assert slots["b"] is None
    assert slots["c"].nodeid.endswith("test_c_uses_the_client_again")
    assert slots["c"] is not slots["a"]
    assert slots["end"] is None


def test_a_trace_never_holds_more_than_the_recorder_bound(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
from pytest_graphql.plugin.reporting import CURRENT
from tests.unit import plugin_probe


def test_many_calls(gql, request):
    for _ in range(40):
        gql.execute(USER_QUERY, {"id": "1"})
    trace = request.config.stash[CURRENT]
    plugin_probe.EVENTS.append(
        ("held", len(trace.recorder.calls), len(trace.requests), trace.recorder.total)
    )
""",
        conftest="""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=5)
""",
    )
    held = [event for event in plugin_probe.EVENTS if event[0] == "held"]
    assert held == [("held", 5, 5, 40)]


def test_every_test_of_a_session_gets_its_own_calls(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
def test_first(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    assert False


def test_second(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    gql.execute(USER_QUERY, {"id": "2"})
    assert False
""",
    )
    assert list(sections_of("test_first")) == ["GraphQL calls (1)"]
    assert list(sections_of("test_second")) == ["GraphQL calls (2)"]


def test_clones_of_the_client_record_into_the_same_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    gql.as_("token-for-someone").execute(USER_QUERY, {"id": "2"})
    assert False
""",
    )
    assert "GraphQL calls (2)" in sections_of("test_fails")


def test_a_fault_in_the_report_does_not_hide_the_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> str:  # noqa: ARG001
        raise RuntimeError("secret detail")

    monkeypatch.setattr(reporting, "render_calls", explode)
    result = run_scripted(pytester, monkeypatch, TWO_CALLS)
    result.assert_outcomes(failed=1)
    sections = sections_of("test_fails")
    assert sections == {"GraphQL calls": "the report failed with RuntimeError"}
    assert "boom" in reports("test_fails", "call")[0][5]
