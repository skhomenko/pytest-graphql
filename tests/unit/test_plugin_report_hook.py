"""``pytest_graphql_report_section`` in a real session (DESIGN section 3).

Every implementation runs, in hook order, and each non-``None`` result is added
to the failed test's report under a heading of its own. The hook is not
``firstresult``, so one plugin cannot hide another. A failure inside a hook or a
value a hook should not show never costs the report of the failure itself.
"""

from __future__ import annotations

import pytest

from tests.unit import plugin_probe
from tests.unit.m9b_support import run_scripted, sections_of

FAILS_AFTER_ONE_CALL = """
def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "123"})
    assert False
"""

ALPHA = """
import pytest


@pytest.hookimpl(trylast=True)
def pytest_graphql_report_section(response, item, config):
    return f"alpha saw status {response.http.status_code} in {item.name}"
"""

BETA = """
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_graphql_report_section(response):
    return "beta saw " + response.request.kind
"""

GAMMA = """
def pytest_graphql_report_section():
    return None
"""


def run_with(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    plugins: dict[str, str],
    test: str = FAILS_AFTER_ONE_CALL,
    conftest: str = "",
    routes: str | None = None,
) -> pytest.RunResult:
    pytester.syspathinsert()
    for name, source in plugins.items():
        pytester.makepyfile(**{name: source})
    arguments: list[str] = []
    for name in plugins:
        arguments += ["-p", name]
    extra = {} if routes is None else {"routes": routes}
    return run_scripted(
        pytester, monkeypatch, test, conftest=conftest, args=arguments, **extra
    )


def titles(nodeid: str = "test_fails") -> list[str]:
    return [
        title for title in sections_of(nodeid) if title.startswith("GraphQL report")
    ]


def test_every_implementation_runs_and_each_has_its_own_heading_in_hook_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(pytester, monkeypatch, {"alpha": ALPHA, "beta": BETA})
    sections = sections_of("test_fails")
    assert titles() == ["GraphQL report: beta", "GraphQL report: alpha"]
    assert sections["GraphQL report: beta"] == "beta saw query"
    assert sections["GraphQL report: alpha"] == "alpha saw status 200 in test_fails"


def test_the_calls_section_comes_before_the_hook_sections(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(pytester, monkeypatch, {"alpha": ALPHA})
    assert list(sections_of("test_fails")) == [
        "GraphQL calls (1)",
        "GraphQL report: alpha",
    ]


def test_a_conftest_implementation_gets_a_section_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        conftest="""
def pytest_graphql_report_section(response):
    return "from the conftest"
""",
    )
    assert sections_of("test_fails")["GraphQL report: conftest"] == "from the conftest"


def test_a_none_result_adds_no_section_and_does_not_stop_the_others(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(pytester, monkeypatch, {"gamma": GAMMA, "alpha": ALPHA})
    assert titles() == ["GraphQL report: alpha"]


def test_an_implementation_may_declare_fewer_arguments_than_the_hook(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(pytester, monkeypatch, {"beta": BETA, "gamma": GAMMA})
    assert titles() == ["GraphQL report: beta"]


def test_the_hook_gets_the_last_response_the_item_and_the_config(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test="""
def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    gql.execute(UPDATE_MUTATION, {"id": "2", "name": "x"}, raise_on_error=False)
    assert False
""",
        conftest="""
from pytest_graphql import GraphQLResponse


def pytest_graphql_report_section(response, item, config):
    plugin_probe.EVENTS.append(
        (
            "args",
            isinstance(response, GraphQLResponse),
            response.request.operation,
            item.name,
            config is item.config,
        )
    )
""",
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "args"] == [
        ("args", True, "updateUser", "test_fails", True)
    ]


def test_it_runs_once_for_a_failed_test_and_not_for_a_passing_one(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test="""
def test_passes(gql):
    gql.execute(USER_QUERY, {"id": "1"})


def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    gql.execute(USER_QUERY, {"id": "2"})
    assert False
""",
        conftest="""
def pytest_graphql_report_section(item):
    plugin_probe.EVENTS.append(("called", item.name))
""",
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "called"] == [
        ("called", "test_fails")
    ]


def test_it_is_not_called_when_the_test_received_no_response(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test="""
import pytest


def test_fails(gql):
    with pytest.raises(ConnectionError):
        gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        conftest="""
def pytest_graphql_report_section(item):
    plugin_probe.EVENTS.append(("called", item.name))
""",
        routes='{"user": ConnectionError("refused")}',
    )
    assert not [e for e in plugin_probe.EVENTS if e[0] == "called"]
    assert list(sections_of("test_fails")) == ["GraphQL calls (1)"]


def test_an_implementation_that_raises_is_named_and_the_others_still_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {
            "broken": """
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_graphql_report_section():
    raise RuntimeError("a message that must not be shown")
""",
            "alpha": ALPHA,
        },
    )
    sections = sections_of("test_fails")
    assert sections["GraphQL report: broken"] == "the hook raised RuntimeError"
    assert "a message that must not be shown" not in str(sections)
    assert "GraphQL report: alpha" in sections


def test_a_result_that_is_not_text_is_reported_as_such(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {"number": "def pytest_graphql_report_section():\n    return 5\n"},
    )
    assert (
        sections_of("test_fails")["GraphQL report: number"]
        == "the hook returned int, not a string"
    )


def test_a_hook_wrapper_is_ignored_and_does_not_break_the_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {
            "wrapper": """
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_graphql_report_section():
    yield
""",
            "alpha": ALPHA,
        },
    )
    assert titles() == ["GraphQL report: alpha"]


def test_control_characters_in_a_section_are_escaped(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {
            "escapes": (
                "def pytest_graphql_report_section():\n"
                '    return "a\\x1b[31mred\\x07 ok\\nsecond line"\n'
            )
        },
    )
    text = sections_of("test_fails")["GraphQL report: escapes"]
    assert text == "a\\x1b[31mred\\x07 ok\nsecond line"


def test_a_section_that_shows_a_redacted_value_is_withheld(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "hook-secret-value-0123456789"
    run_with(
        pytester,
        monkeypatch,
        {},
        test=f"""
def test_fails(gql):
    gql.with_headers(authorization="Bearer {secret}").execute(
        USER_QUERY, {{"id": "1"}}, raise_on_error=False
    )
    assert False
""",
        routes=(
            "{'user': envelope(None, ({'message': 'denied for Bearer "
            + secret
            + "', 'extensions': {'code': 'AUTH'}},))}"
        ),
        conftest="""
def pytest_graphql_report_section(response):
    return "the server said: " + response.errors[0].message
""",
    )
    sections = sections_of("test_fails")
    assert secret not in str(sections)
    assert (
        sections["GraphQL report: conftest"]
        == "[withheld: this text would show a redacted value]"
    )


# -- a test that fails in more than one phase ------------------------------------------

CALLS_THEN_TEARDOWN_FAILS = """
import pytest


@pytest.fixture
def cleanup(gql):
    yield
    raise RuntimeError("cleanup failed")


def test_fails(cleanup, gql):
    gql.execute(USER_QUERY, {"id": "1"})
    assert False
"""

COUNTING_HOOK = """
def pytest_graphql_report_section(item):
    plugin_probe.EVENTS.append(("called", item.name))
    return "ran"
"""


def test_the_hook_runs_once_when_a_test_fails_in_the_call_and_the_teardown(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test=CALLS_THEN_TEARDOWN_FAILS,
        conftest=COUNTING_HOOK,
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "called"] == [
        ("called", "test_fails")
    ]


def test_the_hook_sections_go_to_the_first_failed_phase_and_the_calls_to_each(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test=CALLS_THEN_TEARDOWN_FAILS,
        conftest=COUNTING_HOOK,
    )
    call = sections_of("test_fails", "call")
    teardown = sections_of("test_fails", "teardown")
    assert list(call) == ["GraphQL calls (1)", "GraphQL report: conftest"]
    assert list(teardown) == ["GraphQL calls (1)"]


def test_the_hook_runs_once_when_the_setup_and_the_teardown_both_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test="""
import pytest


@pytest.fixture
def broken(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    yield
    raise RuntimeError("cleanup failed")


@pytest.fixture
def seeded(broken):
    raise RuntimeError("seed failed")


def test_fails(seeded):
    pass
""",
        conftest=COUNTING_HOOK,
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "called"] == [
        ("called", "test_fails")
    ]
    assert "GraphQL report: conftest" in sections_of("test_fails", "setup")


def test_each_test_gets_its_own_hook_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_with(
        pytester,
        monkeypatch,
        {},
        test="""
def test_one(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    assert False


def test_two(gql):
    gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        conftest=COUNTING_HOOK,
    )
    assert [e for e in plugin_probe.EVENTS if e[0] == "called"] == [
        ("called", "test_one"),
        ("called", "test_two"),
    ]
