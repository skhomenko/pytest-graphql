"""Header precedence and cross-test isolation (DESIGN sections 2 and 8, C4).

Header layers, lowest to highest: ``ClientConfig.headers`` (which the ini option
or the environment variable fill), the ``gql_headers`` fixture, ``Auth.apply``
(the ``gql_auth`` fixture), ``with_headers()`` in clone order, then per-call
``headers=``. Names compare case-insensitively, and a later layer replaces an
earlier one for the same name rather than adding a second line.

The chain is read from the request the transport received, so a duplicate line
under another spelling would show. Isolation is read from a real server that
sets a cookie, because only a real transport has a cookie jar.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core.headers import normalize_name
from tests.schema.resolvers import build_schema
from tests.unit.local_http_server import local_server
from tests.unit.plugin_inner import recorded, run_inner
from tests.unit.test_plugin_lifecycle import _GraphQLServer

LAYERS = """
import pytest

from pytest_graphql import HeaderAuth


@pytest.fixture
def gql_headers():
    return {"x-layer": "fixture", "X-Only-Fixture": "f"}


@pytest.fixture
def gql_auth():
    return HeaderAuth({"X-Layer": "auth", "X-Only-Auth": "a"})
"""

CHAIN = """
from tests.unit import plugin_probe


def test_chain(gql):
    gql.query("pingScalar")
    plugin_probe.EVENTS.append(("plain", gql.transport.sent[-1].headers))
    gql.anonymous().query("pingScalar")
    plugin_probe.EVENTS.append(("anonymous", gql.transport.sent[-1].headers))
    gql.with_headers({"X-LAYER": "clone"}).query("pingScalar")
    plugin_probe.EVENTS.append(("clone", gql.transport.sent[-1].headers))
    gql.with_headers({"X-LAYER": "clone"}).query(
        "pingScalar", headers={"x-Layer": "call"}
    )
    plugin_probe.EVENTS.append(("call", gql.transport.sent[-1].headers))
"""


def _layer_lines(headers: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in headers.items()
        if normalize_name(name) == "x-layer"
    ]


def test_the_ini_headers_are_the_lowest_layer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_headers =\n    X-Layer: ini\n    X-Only-Ini: i\n",
        test=CHAIN,
    )
    result.assert_outcomes(passed=1)
    plain = recorded("plain")[0][1]
    assert dict(plain)["X-Layer"] == "ini"
    assert dict(plain)["X-Only-Ini"] == "i"


def test_the_environment_headers_replace_the_ini_headers_as_a_whole(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_headers =\n    X-Layer: ini\n    X-Only-Ini: i\n",
        env={"PYTEST_GQL_HEADERS": "X-Layer: env"},
        test=CHAIN,
    )
    result.assert_outcomes(passed=1)
    plain = dict(recorded("plain")[0][1])
    assert plain["X-Layer"] == "env"
    assert "X-Only-Ini" not in plain


def test_every_layer_replaces_the_one_below_it_under_the_c4_rule(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_headers =\n    X-Layer: ini\n    X-Only-Ini: i\n",
        conftest=LAYERS,
        test=CHAIN,
    )
    result.assert_outcomes(passed=1)
    plain = recorded("plain")[0][1]
    anonymous = recorded("anonymous")[0][1]
    clone = recorded("clone")[0][1]
    call = recorded("call")[0][1]

    # The auth layer is above the fixture, which is above the ini file, and
    # every other name survives from its own layer.
    assert _layer_lines(plain) == [("X-Layer", "auth")]
    assert {"X-Only-Ini", "X-Only-Fixture", "X-Only-Auth"} <= set(plain)
    # Without auth, the fixture shows through, in its own spelling.
    assert _layer_lines(anonymous) == [("x-layer", "fixture")]
    assert "X-Only-Auth" not in anonymous
    assert {"X-Only-Ini", "X-Only-Fixture"} <= set(anonymous)
    # A clone is above auth, and a per-call value above the clone. Each takes
    # its own spelling and leaves one line, not two.
    assert _layer_lines(clone) == [("X-LAYER", "clone")]
    assert _layer_lines(call) == [("x-Layer", "call")]


def test_gql_headers_never_reach_schema_loading_but_the_ini_headers_do(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The schema is session-scoped, so a function-scoped identity cannot be its
    # identity (C4, B20). The ini headers are `ClientConfig.headers`, which is.
    server = _GraphQLServer(build_schema())
    with local_server(server.respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            ini="gql_headers =\n    X-Ini: for-everything\n",
            conftest=LAYERS,
            test="""
def test_call(gql):
    gql.query("pingScalar")
""",
            args=(f"--gql-url={url}",),
        )
    result.assert_outcomes(passed=1)

    introspection, call = [headers for _, headers in server.seen]
    assert introspection["x-ini"] == "for-everything"
    assert "x-layer" not in introspection
    assert "x-only-fixture" not in introspection
    assert "x-only-auth" not in introspection
    assert call["x-ini"] == "for-everything"
    assert call["x-layer"] == "auth"
    assert call["x-only-fixture"] == "f"


# -- isolation between tests --------------------------------------------------

ISOLATION = """
import pytest

from pytest_graphql import BearerAuth


@pytest.fixture(scope="session")
def gql_config(gql_config):
    # A jar that lives as long as one logical client, so a cookie that leaked
    # would be sent again. The default clears the jar after every response.
    import dataclasses

    return dataclasses.replace(gql_config, cookie_scope="client")


@pytest.fixture
def gql_auth(request):
    return BearerAuth("token-of-" + request.node.name)


@pytest.fixture
def gql_headers(request):
    return {"X-Test": request.node.name}


def test_alpha(gql):
    gql.query("pingScalar")
    gql.query("pingScalar")


def test_beta(gql):
    gql.query("pingScalar")


def test_gamma(gql):
    gql.query("pingScalar")
    gql.query("pingScalar")
"""


def test_two_tests_with_different_identities_never_see_each_others_cookies_or_headers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _GraphQLServer(build_schema(), set_cookie=True)
    with local_server(server.respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            test=ISOLATION,
            args=(f"--gql-url={url}",),
        )
    result.assert_outcomes(passed=3)

    calls = [headers for name, headers in server.seen if name != "IntrospectionQuery"]
    alpha1, alpha2, beta, gamma1, gamma2 = calls
    # Inside one test the jar is kept, so the second call sends the cookie.
    assert "cookie" not in alpha1
    assert "session=m5d-cookie-value-0123" in alpha2["cookie"]
    # The next test starts with an empty jar, and its own identity.
    assert "cookie" not in beta
    assert beta["authorization"] == "Bearer token-of-test_beta"
    assert beta["x-test"] == "test_beta"
    assert "cookie" not in gamma1
    assert gamma1["authorization"] == "Bearer token-of-test_gamma"
    assert gamma1["x-test"] == "test_gamma"
    assert "session=m5d-cookie-value-0123" in gamma2["cookie"]
    # And nothing of an earlier test's identity is on a later one's request.
    for later, earlier in ((beta, "alpha"), (gamma1, "beta"), (gamma2, "alpha")):
        assert earlier not in later["authorization"]
        assert earlier not in later["x-test"]


def test_the_introspection_cookie_reaches_no_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _GraphQLServer(build_schema(), set_cookie=True)
    with local_server(server.respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            test=ISOLATION,
            args=(f"--gql-url={url}", "-k", "alpha"),
        )
    result.assert_outcomes(passed=1)
    first_call = next(h for name, h in server.seen if name != "IntrospectionQuery")
    assert "cookie" not in first_call


def test_a_changed_gql_auth_gives_a_test_a_different_identity(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _GraphQLServer(build_schema())
    with local_server(server.respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            test="""
import pytest


@pytest.fixture(params=["one-0123456789", "two-0123456789"])
def gql_auth(request):
    return request.param


def test_call(gql):
    gql.query("pingScalar")
""",
            args=(f"--gql-url={url}",),
        )
    result.assert_outcomes(passed=2)
    tokens = [
        headers["authorization"]
        for name, headers in server.seen
        if name != "IntrospectionQuery"
    ]
    assert tokens == ["Bearer one-0123456789", "Bearer two-0123456789"]


def test_the_schema_identity_is_pinned_so_a_per_test_header_is_never_part_of_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_headers =\n    X-Ini: for-everything\n",
        conftest=LAYERS,
        test="""
from tests.unit import plugin_probe


def test_config(gql):
    plugin_probe.EVENTS.append(
        ("identity", dict(gql.config.schema_headers), dict(gql.config.headers))
    )
""",
    )
    result.assert_outcomes(passed=1)
    _, schema_headers, headers = recorded("identity")[0]
    assert schema_headers == {"X-Ini": "for-everything"}
    # The per-test header is in the request headers and not in the schema's.
    assert headers["x-layer"] == "fixture"
    assert "x-layer" not in {name.lower() for name in schema_headers}
