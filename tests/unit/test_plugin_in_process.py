"""The plugin's in-process path: ``gql_app`` (DESIGN_DECISIONS.md section 13.3).

The application is named by the ``gql_app`` ini option or ``PYTEST_GQL_APP``,
as ``module:attribute``, or returned by an overridden ``gql_app`` fixture.
When there is one, the session transport calls it in process, and the default
URL is ``http://testserver/graphql``. An option that only a network uses is a
usage error naming both settings.
"""

from __future__ import annotations

import threading

import pytest

from pytest_graphql._core.transport.httpx_transport import DEFAULT_APP_URL
from tests.unit.plugin_inner import recorded, recorded_config, run_inner

HELLO = """
from tests.unit import plugin_probe


def test_hello(gql):
    plugin_probe.EVENTS.append(("config", gql.config))
    plugin_probe.EVENTS.append(("hello", gql.query("hello")))
    plugin_probe.EVENTS.append(("greeting", gql.query("greeting")))
"""

APP_FIXTURE = """
import pytest

from tests.unit.in_process_apps import AsgiApp
from tests.unit import plugin_probe


@pytest.fixture(scope="session")
def gql_app():
    app = AsgiApp()
    yield app
    plugin_probe.EVENTS.append(("events", list(app.events)))
"""


def _bridge_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "pytest-graphql-asgi"]


def test_the_ini_option_names_the_app_and_the_url_defaults_to_testserver(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "tests.unit.in_process_apps:wsgi_app"
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        ini=f"gql_app = {path}\n",
        test=HELLO,
    )
    result.assert_outcomes(passed=1)
    assert recorded_config().url == DEFAULT_APP_URL
    assert recorded("hello") == [("hello", "world")]


def test_the_environment_variable_names_an_asgi_app(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        env={"PYTEST_GQL_APP": "tests.unit.in_process_apps:asgi_function"},
        test=HELLO,
    )
    result.assert_outcomes(passed=1)
    assert recorded("hello") == [("hello", "world")]
    assert recorded("greeting") == [("greeting", "hello from startup")]
    assert not _bridge_threads()


def test_an_overridden_fixture_supplies_the_app_and_the_session_closes_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester, monkeypatch, fake=False, url=False, conftest=APP_FIXTURE, test=HELLO
    )
    result.assert_outcomes(passed=1)
    assert recorded("greeting") == [("greeting", "hello from startup")]
    # The session transport, torn down before gql_app, sent the shutdown.
    assert recorded("events") == [("events", ["lifespan.startup", "lifespan.shutdown"])]
    assert not _bridge_threads()


def test_a_url_source_still_wins_over_the_default(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        ini=(
            "gql_app = tests.unit.in_process_apps:wsgi_app\n"
            "gql_url = http://testserver/graphql?from=ini\n"
        ),
        test=HELLO,
    )
    result.assert_outcomes(passed=1)
    assert recorded_config().url == "http://testserver/graphql?from=ini"


def test_gql_verify_with_gql_app_is_a_usage_error_naming_both(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        ini="gql_app = tests.unit.in_process_apps:wsgi_app\ngql_verify = false\n",
        test=HELLO,
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*the ini option gql_verify is set together with gql_app, set by the "
            "ini option gql_app.*"
        ]
    )


def test_a_network_field_set_in_code_with_gql_app_is_a_usage_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = (
        APP_FIXTURE
        + """

import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, proxy="http://127.0.0.1:9")
"""
    )
    result = run_inner(
        pytester, monkeypatch, fake=False, url=False, conftest=conftest, test=HELLO
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ClientConfig.proxy is set together with gql_app, set by the "
            "gql_app fixture.*"
        ]
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("no_such_module:app", "*there is no module named 'no_such_module'.*"),
        (
            "tests.unit.in_process_apps:missing",
            "*module 'tests.unit.in_process_apps' has no attribute 'missing'.*",
        ),
        ("tests.unit.in_process_apps:SDL", "*which is not callable.*"),
    ],
)
def test_a_path_that_names_no_app_has_its_own_message(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, path: str, expected: str
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        ini=f"gql_app = {path}\n",
        test=HELLO,
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines([expected])
