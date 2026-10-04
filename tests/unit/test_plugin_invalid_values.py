"""A refused value stops the run at the start, and says where it came from.

``test_plugin_options.py`` covers every parser. These sessions cover the three
real sources end to end: an ini file, the process environment and the command
line. A refusal is a pytest usage error, so the run exits with that code before
it collects anything, and the message names the setting and the source.
"""

from __future__ import annotations

import pytest

from tests.unit.plugin_inner import run_inner

SECRET = "sk-live-0123456789abcdef"


def _output(result: pytest.RunResult) -> str:
    return result.stdout.str() + result.stderr.str()


def _assert_refused(result: pytest.RunResult) -> str:
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    output = _output(result)
    # It stopped at configure time: nothing was collected or run.
    assert "collected" not in output
    return output


def test_an_invalid_ini_value_names_the_setting_and_the_ini_option(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, ini="gql_max_depth = deep\n")
    output = _assert_refused(result)
    assert "invalid value for gql_max_depth" in output
    assert "the ini option gql_max_depth" in output
    assert "deep" not in output.replace("gql_max_depth", "")


def test_an_invalid_environment_value_names_the_variable(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, env={"PYTEST_GQL_TIMEOUT": "soon"})
    output = _assert_refused(result)
    assert "invalid value for gql_timeout" in output
    assert "the environment variable PYTEST_GQL_TIMEOUT" in output
    assert "soon" not in output


@pytest.mark.parametrize(
    ("flag", "setting"),
    [
        ("--gql-max-depth=deep", "gql_max_depth"),
        ("--gql-timeout=0", "gql_timeout"),
        ("--gql-timeout=soon", "gql_timeout"),
        ("--gql-seed=lucky", "gql_seed"),
        ("--gql-url=http://a b", "gql_url"),
    ],
)
def test_an_invalid_flag_names_the_setting_and_the_flag(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    setting: str,
) -> None:
    result = run_inner(pytester, monkeypatch, args=(flag,))
    output = _assert_refused(result)
    assert f"invalid value for {setting}" in output
    assert f"the command line option {flag.partition('=')[0]}" in output


def test_an_invalid_log_level_names_its_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, args=("--gql-log-level=loud",))
    output = _assert_refused(result)
    assert "--gql-log-level" in output
    assert "summary, full" in output


def test_a_malformed_header_line_in_the_ini_file_does_not_echo_its_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester, monkeypatch, ini=f"gql_headers =\n    Authorization Bearer {SECRET}\n"
    )
    output = _assert_refused(result)
    assert "the ini option gql_headers" in output
    assert "line 1" in output
    assert SECRET not in output


def test_a_malformed_header_line_in_the_environment_does_not_echo_its_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        env={"PYTEST_GQL_HEADERS": f"X-Fine: ok\nX-Api-Key {SECRET}"},
    )
    output = _assert_refused(result)
    assert "the environment variable PYTEST_GQL_HEADERS" in output
    assert "line 2" in output
    assert SECRET not in output
    assert "X-Fine" not in output


def test_a_url_with_a_credential_is_not_echoed_when_the_flag_is_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester, monkeypatch, args=(f"--gql-url=http://user:{SECRET} @host/graphql",)
    )
    output = _assert_refused(result)
    assert SECRET not in output


def test_a_value_hidden_by_a_higher_source_is_still_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flag wins, so the bad ini value would never take effect. It is a
    # defect in the project's configuration all the same.
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_max_depth = deep\n",
        args=("--gql-max-depth=2",),
    )
    output = _assert_refused(result)
    assert "the ini option gql_max_depth" in output


def test_a_missing_ca_bundle_is_refused_with_the_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester, monkeypatch, env={"PYTEST_GQL_VERIFY": "/no/such/bundle.pem"}
    )
    output = _assert_refused(result)
    assert "PYTEST_GQL_VERIFY" in output
    assert "/no/such/bundle.pem" not in output


def test_valid_values_in_every_source_let_the_run_proceed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_retries = 1\n",
        env={"PYTEST_GQL_MAX_DEPTH": "2"},
        args=("--gql-timeout=5",),
    )
    result.assert_outcomes(passed=1)
