"""No secret reaches any new output (DESIGN section 7, "Coverage").

One session plants a known credential in every place a test can put one: an ini
header or an environment header, ``gql_auth``, a per-call header, a cookie, a
variable and a server error message that echoes them all. The session then
produces every output M9b adds, and no credential may appear in any of them:
the header, the failure section, a hook section, the log at both levels, the
schema summary and statistics, and the assertion diff.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.unit import plugin_probe
from tests.unit.m9b_support import reports, run_scripted, sections_of

INI = "ini-header-secret-0123456789"
AUTH = "auth-secret-value-0123456789"
CALL = "call-secret-value-0123456789"
COOKIE = "cookie-secret-value-0123456789"
VARIABLE = "variable-secret-value-0123456789"
SCHEMA = "schema-only-secret-value-0123456789"
CONFIG_COOKIE = "config-cookie-secret-value-0123456789"
PROXY = "proxy-password-secret-0123456789"
SECRETS = {
    "ini-or-env header": INI,
    "gql_auth": AUTH,
    "per-call header": CALL,
    "cookie": COOKIE,
    "variable": VARIABLE,
    "schema_headers": SCHEMA,
    "config cookies": CONFIG_COOKIE,
    "proxy": PROXY,
}
#: What a server can echo back: only what it was sent, so the first response holds
#: the first call's secrets, and the second holds the second call's plus the
#: first call's per-call header, which a server may keep and repeat.
ECHO_FIRST = "the server said: " + " | ".join(
    [INI, AUTH, CALL, COOKIE, f"Bearer {AUTH}", f"sid={COOKIE}"]
)
ECHO_SECOND = "the server said: " + " | ".join(
    [
        INI,
        AUTH,
        CALL,
        COOKIE,
        VARIABLE,
        f"Bearer {AUTH}",
        f"sid={COOKIE}",
        SCHEMA,
        CONFIG_COOKIE,
        PROXY,
    ]
)

ROUTES = f"""{{
    "user": envelope(
        {{"user": {{"id": {ECHO_FIRST!r}, "name": {ECHO_FIRST!r}}}}},
        ({{"message": {ECHO_FIRST!r}, "path": ["user", {ECHO_FIRST!r}],
           "extensions": {{"code": "LEAK", "detail": {ECHO_FIRST!r}}}}},),
    ),
    "updateUser": envelope(
        None, ({{"message": {ECHO_SECOND!r}, "extensions": {{"code": "LEAK"}}}},)
    ),
}}"""

CONFTEST = f"""
import dataclasses
from pytest_graphql import HeaderAuth


@pytest.fixture
def gql_headers():
    return {{"Cookie": "sid={COOKIE}"}}


@pytest.fixture
def gql_auth():
    return HeaderAuth({{"X-Auth-Secret": {AUTH!r}}})


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(
        gql_config,
        redact_variables=(*gql_config.redact_variables, "name"),
        schema_headers={{"Authorization": "Bearer {SCHEMA}"}},
        cookies={{"sid": "{CONFIG_COOKIE}"}},
        proxy="http://agent:{PROXY}@proxy.example.test:3128",
    )


class Source:
    fingerprint = "custom:{INI} {SCHEMA} {CONFIG_COOKIE} {PROXY}"

    def load(self):
        raise AssertionError("the fixture overrides the schema")


@pytest.fixture(scope="session")
def gql_schema_source():
    return Source()


def pytest_graphql_report_section(response):
    return "repr: " + repr(response)

"""

#: A second implementation, in its own module, that shows the server's own text.
RAW_HOOK = """
import pytest


@pytest.hookimpl(trylast=True)
def pytest_graphql_report_section(response):
    return "raw: " + response.errors[0].message
"""

TEST = f"""
from tests.unit import plugin_probe


def test_leaks(gql, caplog):
    first = gql.execute(
        USER_QUERY, {{"id": "123"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    gql.execute(
        UPDATE_MUTATION, {{"id": "123", "name": {VARIABLE!r}}}, raise_on_error=False
    )
    plugin_probe.EVENTS.append(
        (
            "log",
            [
                r.getMessage()
                for r in caplog.records
                if r.name == "pytest_graphql.calls"
            ],
        )
    )
    assert first.data.user == gql.expect.User(id="expected-id", name="expected-name")
"""

INI_SETTINGS = (
    "gql_redact_headers = x-ini-token, x-auth-secret, x-call-secret\n"
    f"gql_headers =\n    X-Ini-Token: {INI}\n"
)
ENV_SETTINGS = {"PYTEST_GQL_HEADERS": f"X-Ini-Token: {INI}"}


def produce(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str,
    level: str,
) -> Mapping[str, str]:
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    ini = "gql_redact_headers = x-ini-token, x-auth-secret, x-call-secret\n"
    env: Mapping[str, str] | None = None
    if source == "ini":
        ini = INI_SETTINGS
    else:
        env = ENV_SETTINGS
    result = run_scripted(
        pytester,
        monkeypatch,
        TEST,
        routes=ROUTES,
        conftest=CONFTEST,
        ini=ini,
        env=env,
        args=(
            f"--gql-url=http://example.test/graphql/{INI}",
            "--gql-log",
            f"--gql-log-level={level}",
            "--gql-show-schema-stats",
            "-p",
            "raw_hook",
        ),
    )
    result.assert_outcomes(failed=1)
    stdout = result.stdout.lines
    sections = sections_of("test_leaks")
    summary_start = next(
        i for i, line in enumerate(stdout) if line.strip("= ") == "GraphQL"
    )
    tail = stdout[summary_start + 1 :]
    end = next((i for i, line in enumerate(tail) if line.startswith("=")), len(tail))
    longrepr = reports("test_leaks", "call")[0][5]
    [log] = [event[1] for event in plugin_probe.EVENTS if event[0] == "log"]
    return {
        "header": "\n".join(line for line in stdout if line.startswith("graphql:")),
        "failure section": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL calls")
        ),
        "hook sections": "\n".join(
            f"{title}\n{text}"
            for title, text in sections.items()
            if title.startswith("GraphQL report")
        ),
        "log": "\n".join(log),
        "schema summary and stats": "\n".join(tail[:end]),
        "assertion diff": "\n".join(
            line for line in longrepr.splitlines() if line.startswith("E")
        ),
    }


@pytest.mark.parametrize("level", ["summary", "full"])
@pytest.mark.parametrize("source", ["ini", "env"])
def test_no_secret_appears_in_any_output(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    level: str,
) -> None:
    outputs = produce(pytester, monkeypatch, source=source, level=level)
    for name, secret in SECRETS.items():
        for output, text in outputs.items():
            assert secret not in text, f"{name} shown in the {output}:\n{text}"


@pytest.mark.parametrize("level", ["summary", "full"])
def test_the_outputs_are_not_empty_so_the_check_above_means_something(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    outputs = produce(pytester, monkeypatch, source="ini", level=level)
    assert outputs["header"].startswith("graphql: endpoint http://example.test/graphql")
    assert "[2] mutation updateUser" in outputs["failure section"]
    assert "FAILED HERE" in outputs["failure section"]
    assert "reproduce:" in outputs["failure section"]
    assert "repr: GraphQLResponse(" in outputs["hook sections"]
    assert "GraphQL report: raw_hook" in outputs["hook sections"]
    assert "-> errors" in outputs["log"]
    assert "schema stats main:" in outputs["schema summary and stats"]
    assert "does not match" in outputs["assertion diff"]


def test_the_raw_server_text_is_withheld_not_dropped(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = produce(pytester, monkeypatch, source="ini", level="full")
    assert (
        "GraphQL report: raw_hook\n[withheld: this text would show a redacted value]"
        in outputs["hook sections"]
    )


# -- a credential only the configuration holds ---------------------------------------
#
# The matrix above echoes several secrets in one line, so a line that is withheld
# for one of them hides how the others were handled. Here the server echoes a
# single credential that the configuration holds and that no call carries.

CONFIG_ONLY = {
    "schema_headers": SCHEMA,
    "cookies": CONFIG_COOKIE,
    "proxy": PROXY,
}

CONFIG_TEST = """
from tests.unit import plugin_probe


def test_leaks(gql, caplog):
    response = gql.execute(USER_QUERY, {"id": "123"}, raise_on_error=False)
    plugin_probe.EVENTS.append(
        (
            "log",
            [
                r.getMessage()
                for r in caplog.records
                if r.name == "pytest_graphql.calls"
            ],
        )
    )
    assert response.data.user == gql.expect.User(id="expected-id", name="x")
"""


@pytest.mark.parametrize("field", sorted(CONFIG_ONLY))
@pytest.mark.parametrize("level", ["summary", "full"])
def test_a_credential_only_the_configuration_holds_appears_in_no_output(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    level: str,
) -> None:
    secret = CONFIG_ONLY[field]
    echo = f"the server said: {secret}"
    routes = (
        f"{{'user': envelope({{'user': {{'id': {echo!r}, 'name': 'x'}}}}, "
        f"({{'message': {echo!r}, 'extensions': {{'code': 'LEAK'}}}},))}}"
    )
    settings = {
        "schema_headers": f'schema_headers={{"Authorization": "Bearer {secret}"}}',
        "cookies": f'cookies={{"sid": "{secret}"}}',
        "proxy": f'proxy="http://agent:{secret}@proxy.example.test:3128"',
    }[field]
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        CONFIG_TEST,
        routes=routes,
        conftest=f"""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, {settings})
""",
        args=("--gql-log", f"--gql-log-level={level}", "-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    sections = sections_of("test_leaks")
    [log] = [event[1] for event in plugin_probe.EVENTS if event[0] == "log"]
    outputs = {
        "failure section": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL calls")
        ),
        "hook sections": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL report")
        ),
        "log": "\n".join(log),
        "assertion diff": "\n".join(
            line
            for line in reports("test_leaks", "call")[0][5].splitlines()
            if line.startswith("E")
        ),
    }
    for name, text in outputs.items():
        assert secret not in text, f"{field} shown in the {name}:\n{text}"
    # The line that held it is withheld or scrubbed, not dropped.
    assert (
        "[withheld" in outputs["failure section"]
        or "[redacted" in outputs["failure section"]
    )


# -- a cut through a credential the configuration holds ------------------------------
#
# A cap can end inside a credential that the server echoed. The cut text is built
# when the call is recorded, from that call's request, so the request must already
# know every credential of the configuration, not only the headers it sent.

ECHO_PREFIX = "the server said: "


@pytest.mark.parametrize("field", sorted(CONFIG_ONLY))
@pytest.mark.parametrize("extra", [4, 14, 30])
def test_a_cut_through_a_configuration_credential_shows_no_part_of_it(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    extra: int,
) -> None:
    secret = CONFIG_ONLY[field]
    echo = f"{ECHO_PREFIX}{secret}"
    routes = (
        f"{{'user': envelope({{'user': {{'id': {echo!r}, 'name': 'x'}}}}, "
        f"({{'message': {echo!r}, 'extensions': {{'code': 'LEAK'}}}},))}}"
    )
    settings = {
        "schema_headers": f'schema_headers={{"Authorization": "Bearer {secret}"}}',
        "cookies": f'cookies={{"sid": "{secret}"}}',
        "proxy": f'proxy="http://agent:{secret}@proxy.example.test:3128"',
    }[field]
    cap = len(ECHO_PREFIX) + extra
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        CONFIG_TEST,
        routes=routes,
        conftest=f"""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(
        gql_config, {settings}, max_diagnostic_bytes={cap}
    )
""",
        args=("--gql-log", "--gql-log-level=full", "-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    sections = sections_of("test_leaks")
    [log] = [event[1] for event in plugin_probe.EVENTS if event[0] == "log"]
    outputs = {
        "failure section": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL calls")
        ),
        "hook sections": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL report")
        ),
        "log": "\n".join(log),
        "assertion diff": "\n".join(
            line
            for line in reports("test_leaks", "call")[0][5].splitlines()
            if line.startswith("E")
        ),
    }
    for name, text in outputs.items():
        assert secret[:8] not in text, f"{field} started in the {name}:\n{text}"


# -- a credential an earlier call sent ------------------------------------------------
#
# The second response of the session above repeats the per-call header of the first
# call. A cap can end inside it. The request of the second call does not carry that
# header, so the client lists what the calls before it sent in the secret set.

CALL_AT = len(ECHO_SECOND.split(CALL)[0])


@pytest.mark.parametrize("extra", range(1, len(CALL), 5))
def test_a_cut_through_an_earlier_calls_header_shows_no_part_of_it(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    extra: int,
) -> None:
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    conftest = CONFTEST.replace(
        "redact_variables=",
        f"max_diagnostic_bytes={CALL_AT + extra}, redact_variables=",
        1,
    )
    result = run_scripted(
        pytester,
        monkeypatch,
        TEST,
        routes=ROUTES,
        conftest=conftest,
        ini=INI_SETTINGS,
        args=("--gql-log", "--gql-log-level=full", "-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    sections = sections_of("test_leaks")
    [log] = [event[1] for event in plugin_probe.EVENTS if event[0] == "log"]
    outputs = {
        "failure section": "\n".join(
            text
            for title, text in sections.items()
            if title.startswith("GraphQL calls")
        ),
        "log": "\n".join(log),
    }
    for name, text in outputs.items():
        assert CALL[:8] not in text, (
            f"the start of the header is in the {name}:\n{text}"
        )


EVICTED_TEST = f"""
def test_leaks(gql):
    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    gql.execute(USER_QUERY, {{"id": "2"}}, raise_on_error=False)
    assert {{"id": {CALL!r}}} == gql.expect.User(id="expected-id")
"""


def test_a_value_in_a_diff_is_scrubbed_of_a_credential_the_record_has_dropped(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One call is recorded, so the first request has left the trace, and the
    # diff still must not print the header that call sent.
    result = run_scripted(
        pytester,
        monkeypatch,
        EVICTED_TEST,
        routes="{'user': envelope({'user': {'id': '1', 'name': 'x'}})}",
        conftest="""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=1)
""",
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=1)
    diff = "\n".join(
        line
        for line in reports("test_leaks", "call")[0][5].splitlines()
        if line.startswith("E")
    )
    assert "expected-id" in diff
    assert CALL not in diff


GAPPED_DIFF_TEST = f"""
def test_leaks(gql, pytestconfig):
    from pytest_graphql._core.diagnostics import MAX_HELD_CREDENTIALS
    from pytest_graphql.plugin.reporting import session_ledger

    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    session_ledger(pytestconfig).add(
        [("filler", f"filler-token-{{i:05d}}-0123456789") for i in range(
            MAX_HELD_CREDENTIALS + 1
        )]
    )
    gql.execute(USER_QUERY, {{"id": "2"}}, raise_on_error=False)
    assert {{"id": {CALL!r}}} == gql.expect.User(id="expected-id")
"""


def test_a_value_in_a_diff_is_withheld_after_the_session_ledger_dropped_a_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        GAPPED_DIFF_TEST,
        routes="{'user': envelope({'user': {'id': '1', 'name': 'x'}})}",
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=1)
    text = "\n".join(
        line
        for line in reports("test_leaks", "call")[0][5].splitlines()
        if line.startswith("E")
    )
    assert "withheld" in text
    assert CALL not in text


TWO_TESTS = f"""
def test_a_sends(gql):
    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    assert False


def test_b_hears_it_again(gql):
    gql.execute(UPDATE_MUTATION, {{"id": "1", "name": "n"}}, raise_on_error=False)
    assert False
"""


@pytest.mark.parametrize("extra", range(1, len(CALL), 5))
def test_a_cut_through_a_header_an_earlier_test_sent_shows_no_part_of_it(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    extra: int,
) -> None:
    # Each test has its own recorder, and a server can repeat a header across
    # tests as well as across calls, so what the calls sent is held per session.
    echo = f"the server said: {CALL}"
    routes = (
        "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
        f"'updateUser': envelope(None, ({{'message': {echo!r}}},))}}"
    )
    cap = len("the server said: ") + extra
    result = run_scripted(
        pytester,
        monkeypatch,
        TWO_TESTS,
        routes=routes,
        conftest=f"""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_diagnostic_bytes={cap})
""",
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=2)
    text = "\n".join(sections_of("test_b_hears_it_again").values())
    assert "the server said" in text
    assert CALL[:8] not in text, f"the header starts in the section:\n{text}"


EVICTING_TESTS = f"""
def test_a_sends(gql):
    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    assert False


def test_b_after_many_tokens(gql, pytestconfig):
    from pytest_graphql._core.diagnostics import MAX_HELD_CREDENTIALS
    from pytest_graphql.plugin.reporting import session_ledger

    session_ledger(pytestconfig).add(
        [("filler", f"filler-token-{{i:05d}}-0123456789") for i in range(
            MAX_HELD_CREDENTIALS + 1
        )]
    )
    gql.execute(UPDATE_MUTATION, {{"id": "1", "name": "n"}}, raise_on_error=False)
    assert False
"""


@pytest.mark.parametrize("cap", [0, 21, 30, 4096])
def test_text_is_withheld_after_the_session_ledger_dropped_a_credential(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
) -> None:
    # A suite that sends more distinct tokens than the ledger holds can no longer
    # name every credential a server may repeat, so the text of a server is
    # withheld, whether a cap cuts it or not.
    echo = f"the server said: {CALL}"
    routes = (
        "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
        f"'updateUser': envelope(None, ({{'message': {echo!r}}},))}}"
    )
    result = run_scripted(
        pytester,
        monkeypatch,
        EVICTING_TESTS,
        routes=routes,
        conftest=f"""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_diagnostic_bytes={cap})
""",
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=2)
    text = "\n".join(sections_of("test_b_after_many_tokens").values())
    assert CALL not in text, f"the header is in the section:\n{text}"
    assert CALL[:8] not in text, f"the header starts in the section:\n{text}"
    assert "withheld" in text or cap == 0


def test_a_hook_section_is_withheld_when_it_shows_a_header_an_earlier_test_sent(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    echo = f"the server said: {CALL}"
    routes = (
        "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
        f"'updateUser': envelope(None, ({{'message': {echo!r}}},))}}"
    )
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        TWO_TESTS,
        routes=routes,
        ini="gql_redact_headers = x-call-secret\n",
        args=("-p", "raw_hook"),
    )
    result.assert_outcomes(failed=2)
    sections = sections_of("test_b_hears_it_again")
    hook = "\n".join(
        text for title, text in sections.items() if title.startswith("GraphQL report")
    )
    assert hook
    assert CALL not in hook


HOOK_AFTER_EVICTION = f"""
def test_leaks(gql, pytestconfig):
    from pytest_graphql._core.diagnostics import MAX_HELD_CREDENTIALS
    from pytest_graphql.plugin.reporting import session_ledger

    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    session_ledger(pytestconfig).add(
        [("filler", f"filler-token-{{i:05d}}-0123456789") for i in range(
            MAX_HELD_CREDENTIALS + 1
        )]
    )
    gql.execute(UPDATE_MUTATION, {{"id": "1", "name": "n"}}, raise_on_error=False)
    assert False
"""

#: Both calls are one test's, and the recorder keeps only the last, so nothing
#: but the session ledger could still name the first call's header.
ONE_CALL_KEPT = """
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=1)
"""


#: The same one-call recorder, from a suite that opted out of value redaction.
ONE_CALL_UNREDACTED = """
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=1, redact_values=False)
"""


def test_a_hook_section_is_shown_after_a_drop_without_redaction(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # redact_values=False scrubs nothing, so it has nothing to withhold either.
    routes = "{'updateUser': envelope(None, ({'message': 'plain words'},))}"
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        HOOK_AFTER_EVICTION,
        routes="{'user': envelope({'user': {'id': '1', 'name': 'x'}}), " + routes[1:],
        conftest=ONE_CALL_UNREDACTED,
        ini="gql_redact_headers = x-call-secret\n",
        args=("-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    hook = "\n".join(
        text
        for title, text in sections_of("test_leaks").items()
        if title.startswith("GraphQL report")
    )
    assert "raw: plain words" in hook
    assert "withheld" not in hook


def test_a_hook_section_is_withheld_after_the_session_ledger_dropped_a_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook returns the message exactly as the server sent it, and the
    # call that sent the header is no longer in the recorder, so no snapshot
    # can vouch for the text.
    echo = f"the server said: {CALL}"
    routes = (
        "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
        f"'updateUser': envelope(None, ({{'message': {echo!r}}},))}}"
    )
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        HOOK_AFTER_EVICTION,
        routes=routes,
        conftest=ONE_CALL_KEPT,
        ini="gql_redact_headers = x-call-secret\n",
        args=("-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    sections = sections_of("test_leaks")
    hook = "\n".join(
        text for title, text in sections.items() if title.startswith("GraphQL report")
    )
    assert hook
    assert "withheld" in hook
    assert CALL not in hook
    assert CALL[:8] not in "\n".join(sections.values())


def test_a_hook_section_is_shown_while_no_credential_was_dropped(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.syspathinsert()
    pytester.makepyfile(raw_hook=RAW_HOOK)
    result = run_scripted(
        pytester,
        monkeypatch,
        "def test_leaks(gql):\n"
        "    gql.execute(UPDATE_MUTATION, {'id': '1', 'name': 'n'}, "
        "raise_on_error=False)\n"
        "    assert False\n",
        routes="{'updateUser': envelope(None, ({'message': 'plain words'},))}",
        args=("-p", "raw_hook"),
    )
    result.assert_outcomes(failed=1)
    hook = "\n".join(
        text
        for title, text in sections_of("test_leaks").items()
        if title.startswith("GraphQL report")
    )
    assert "raw: plain words" in hook


RAW_MATCHER_AFTER_EVICTION = f"""
def test_leaks(gql, pytestconfig):
    from pytest_graphql import Matcher
    from pytest_graphql._core.diagnostics import MAX_HELD_CREDENTIALS
    from pytest_graphql.plugin.reporting import session_ledger

    class Raw(Matcher):
        __slots__ = ("text",)

        def __init__(self, text):
            self.text = text

        def _check(self, actual, walk):
            return walk.leaf(False, actual, self)

        def describe(self, show, depth=2):
            return "raw: " + self.text

    gql.execute(
        USER_QUERY, {{"id": "1"}}, headers={{"X-Call-Secret": {CALL!r}}},
        raise_on_error=False,
    )
    session_ledger(pytestconfig).add(
        [("filler", f"filler-token-{{i:05d}}-0123456789") for i in range(
            MAX_HELD_CREDENTIALS + 1
        )]
    )
    response = gql.execute(
        UPDATE_MUTATION, {{"id": "1", "name": "n"}}, raise_on_error=False
    )
    assert {{"id": "x"}} == Raw(response.errors[0].message)
"""


def test_a_matcher_diff_is_withheld_after_the_session_ledger_dropped_a_credential(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A matcher a project wrote can print text without the renderer's scrub.
    echo = f"the server said: {CALL}"
    routes = (
        "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
        f"'updateUser': envelope(None, ({{'message': {echo!r}}},))}}"
    )
    result = run_scripted(
        pytester,
        monkeypatch,
        RAW_MATCHER_AFTER_EVICTION,
        routes=routes,
        conftest=ONE_CALL_KEPT,
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=1)
    text = "\n".join(
        line
        for line in reports("test_leaks", "call")[0][5].splitlines()
        if line.startswith("E")
    )
    assert "withheld" in text
    assert CALL[:8] not in text


def test_a_matcher_diff_is_shown_after_a_drop_without_redaction(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        RAW_MATCHER_AFTER_EVICTION,
        routes=(
            "{'user': envelope({'user': {'id': '1', 'name': 'x'}}), "
            "'updateUser': envelope(None, ({'message': 'plain words'},))}"
        ),
        conftest=ONE_CALL_UNREDACTED,
        ini="gql_redact_headers = x-call-secret\n",
    )
    result.assert_outcomes(failed=1)
    text = "\n".join(
        line
        for line in reports("test_leaks", "call")[0][5].splitlines()
        if line.startswith("E")
    )
    assert "raw: plain words" in text
    assert "withheld" not in text
