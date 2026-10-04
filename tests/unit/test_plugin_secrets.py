"""Header values from every plugin source join the secret set (DESIGN section 7).

A header value is secret when its name is in ``redact_headers``. That rule is
the same for a value from the ini file, from the environment, from the
``gql_headers`` fixture, from ``gql_auth`` and from a per-call ``headers=``,
because by the time redaction runs they are one merged header map on the
request. These sessions plant a known value in each source and check every
rendering of the request the transport received.

``gql_redact_headers`` extends the default list, so a project that adds one
name keeps redacting ``authorization``, ``cookie``, ``x-api-key`` and
``proxy-authorization``. One source still replaces another: the environment
list replaces the ini list, and each extends the default.
"""

from __future__ import annotations

import pytest

from tests.unit.plugin_inner import recorded, recorded_config, run_inner

INI_SECRET = "ini-secret-value-0123"
ENV_SECRET = "env-secret-value-0123"
FIXTURE_SECRET = "fixture-secret-value-0123"
AUTH_SECRET = "auth-secret-value-0123"
CALL_SECRET = "call-secret-value-0123"

INSPECT = """
from tests.unit import plugin_probe


def test_inspect(gql):
    gql.query("pingScalar", headers={"X-Call-Secret": "call-secret-value-0123"})
    info = gql.transport.sent[-1]
    secrets = [
        "ini-secret-value-0123",
        "env-secret-value-0123",
        "fixture-secret-value-0123",
        "auth-secret-value-0123",
        "call-secret-value-0123",
        "plain-value-0123456",
    ]
    echo = "the server said: " + " | ".join(secrets)
    plugin_probe.EVENTS.append(("scrubbed", info.scrub(echo)))
    plugin_probe.EVENTS.append(("repr", repr(info) + str(info)))
    plugin_probe.EVENTS.append(("curl", info.as_curl()))
    plugin_probe.EVENTS.append(("config", repr(gql.config)))
"""

CONFTEST = """
import pytest

from pytest_graphql import HeaderAuth


@pytest.fixture
def gql_headers():
    return {"X-Fixture-Secret": "fixture-secret-value-0123"}


@pytest.fixture
def gql_auth():
    return HeaderAuth({"X-Auth-Secret": "auth-secret-value-0123"})
"""

LISTED = "x-ini-secret, x-env-secret, x-fixture-secret, x-auth-secret, x-call-secret"


def _run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, listed: str = LISTED
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini=(
            f"gql_headers =\n    X-Ini-Secret: {INI_SECRET}\n"
            f"    X-Plain: plain-value-0123456\n"
            f"gql_redact_headers = {listed}\n"
        ),
        conftest=CONFTEST,
        test=INSPECT,
    )
    result.assert_outcomes(passed=1)


def test_a_redacted_header_value_from_every_source_is_scrubbed_from_free_text(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(pytester, monkeypatch)
    scrubbed = recorded("scrubbed")[0][1]
    for secret in (INI_SECRET, FIXTURE_SECRET, AUTH_SECRET, CALL_SECRET):
        assert secret not in scrubbed


def test_an_environment_header_joins_the_secret_set_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini=f"gql_redact_headers = {LISTED}\n",
        env={"PYTEST_GQL_HEADERS": f"X-Env-Secret: {ENV_SECRET}"},
        test=INSPECT,
    )
    result.assert_outcomes(passed=1)
    assert ENV_SECRET not in recorded("scrubbed")[0][1]
    assert ENV_SECRET not in recorded("curl")[0][1]
    assert ENV_SECRET not in recorded("repr")[0][1]


def test_no_rendering_of_the_request_shows_a_secret_from_any_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(pytester, monkeypatch)
    for kind in ("repr", "curl", "config"):
        text = recorded(kind)[0][1]
        for secret in (INI_SECRET, FIXTURE_SECRET, AUTH_SECRET, CALL_SECRET):
            assert secret not in text, f"{secret} in {kind}"


def test_a_header_that_is_not_listed_is_not_redacted_as_for_a_per_call_header(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(pytester, monkeypatch)
    # Redaction is by name, for every source alike. X-Plain was not listed.
    assert "plain-value-0123456" in recorded("scrubbed")[0][1]


def test_the_default_list_redacts_authorization_from_the_ini_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_headers =\n    Authorization: Bearer ini-secret-value-0123\n",
        test=INSPECT,
    )
    result.assert_outcomes(passed=1)
    assert INI_SECRET not in recorded("scrubbed")[0][1]
    assert INI_SECRET not in recorded("curl")[0][1]


def test_a_listed_name_extends_the_default_so_authorization_stays_redacted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini=(
            "gql_headers =\n    Authorization: Bearer ini-secret-value-0123\n"
            "    X-Listed: env-secret-value-0123\n"
            "gql_redact_headers = x-listed\n"
        ),
        test=INSPECT,
    )
    result.assert_outcomes(passed=1)
    # A project that adds one header does not stop redacting the defaults.
    for kind in ("scrubbed", "curl", "repr"):
        text = recorded(kind)[0][1]
        assert INI_SECRET not in text
        assert ENV_SECRET not in text


def test_a_listed_name_extends_the_default_for_cookie_and_the_api_key_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        env={
            "PYTEST_GQL_HEADERS": (
                "Cookie: sid=ini-secret-value-0123\n"
                "X-Api-Key: env-secret-value-0123\n"
                "Proxy-Authorization: Basic fixture-secret-value-0123"
            ),
            "PYTEST_GQL_REDACT_HEADERS": "x-something-else",
        },
        test=INSPECT,
    )
    result.assert_outcomes(passed=1)
    scrubbed = recorded("scrubbed")[0][1]
    for secret in (INI_SECRET, ENV_SECRET, FIXTURE_SECRET):
        assert secret not in scrubbed


def test_a_gql_config_a_project_builds_keeps_its_own_exact_list(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Extending is the rule of the ini option and the environment variable. A
    # ClientConfig a project builds itself holds exactly what it says.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest

from pytest_graphql import ClientConfig


@pytest.fixture(scope="session")
def gql_config():
    return ClientConfig(redact_headers=("x-only-this",))
""",
        ini="gql_redact_headers = x-ignored\n",
    )
    result.assert_outcomes(passed=1)
    assert tuple(recorded_config().redact_headers) == ("x-only-this",)


def test_the_configuration_repr_leaves_out_the_headers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(pytester, monkeypatch)
    assert "X-Ini-Secret" not in recorded("config")[0][1]
