"""Failure paths of the M9a fixtures and hooks, against the 9.1 ownership rules.

``test_plugin_lifecycle.py`` proves the M5d guarantees and still runs unchanged.
These tests add the failure points M9a created. The invariant is the same in
every one, and it is checked by object rather than by count: every transport a
run derives is closed exactly once, the root pool is closed exactly once and
after everything else, and a failure that happens before the pool exists opens
no pool at all.

Where a failure falls decides what there is to close. A fixture or hook that runs
before ``gql_transport`` leaves nothing to close. One that runs after it leaves
the pool, which the session fixture's teardown owns. A failure while the per-test
client is built leaves one derived transport, which ``_client_over`` closes. The
new function-scoped inputs, ``gql_headers``, ``gql_auth`` and ``gql_seed``, are
read before the transport is derived, so a failure in them has nothing to close.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NoReturn

import pytest

from pytest_graphql import plugin
from pytest_graphql._core import client as client_module
from pytest_graphql._core.transport import httpx_transport
from pytest_graphql.plugin import fixtures
from tests.schema.resolvers import build_schema
from tests.unit import plugin_probe
from tests.unit.local_http_server import closed_port_url, local_server
from tests.unit.plugin_inner import run_inner
from tests.unit.test_plugin_lifecycle import (  # noqa: F401
    _assert_one_pool_closed_last,
    _closed_at,
    _failing_introspection,
    _GraphQLServer,
    _of,
    events,
)

PASSING = """
def test_one(gql):
    assert gql.query("pingScalar") is True


def test_two(gql):
    assert gql.query("pingScalar") is True
"""


@pytest.fixture
def derives(
    events: list[tuple[Any, ...]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[Any, ...]]]:
    """Record every transport a run derives, on top of the close events."""
    for cls in {client_module.HttpxTransport, httpx_transport.HttpxTransport}:
        real = cls.derive

        def recording(self: Any, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            derived = _real(self, *args, **kwargs)
            plugin_probe.EVENTS.append(("derive", derived))
            return derived

        monkeypatch.setattr(cls, "derive", recording)
    yield events


def _assert_every_derived_transport_closed_once(
    events: list[tuple[Any, ...]],  # noqa: F811
) -> None:
    for _, derived in _of(events, "derive"):
        assert len(_closed_at(events, derived)) == 1


def _run_against_server(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    test: str = PASSING,
    conftest: str = "",
    ini: str = "",
    server: _GraphQLServer | None = None,
) -> pytest.RunResult:
    server = server or _GraphQLServer(build_schema())
    with local_server(server.respond) as url:
        return run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            test=test,
            conftest=conftest,
            ini=ini,
            args=(f"--gql-url={url}",),
        )


# -- failures in the per-test inputs: nothing is derived yet ------------------


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        (
            """
@pytest.fixture
def gql_headers():
    raise RuntimeError("headers fixture failed")
""",
            "headers fixture failed",
        ),
        (
            """
@pytest.fixture
def gql_auth():
    raise RuntimeError("auth fixture failed")
""",
            "auth fixture failed",
        ),
        (
            """
@pytest.fixture
def gql_seed():
    return "not an int"
""",
            "gql_seed fixture must return an int",
        ),
        (
            """
@pytest.fixture
def gql_headers():
    return ["not", "a", "mapping"]
""",
            "gql_headers fixture must return a mapping",
        ),
    ],
)
def test_a_failing_per_test_input_derives_nothing_for_that_test(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
    fixture: str,
    message: str,
) -> None:
    result = _run_against_server(
        pytester, monkeypatch, conftest="import pytest\n" + fixture
    )
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines([f"*{message}*"])
    # Only the session's own two: the schema probe and the session transport.
    assert len(_of(derives, "derive")) == 2
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


# -- failures before the pool exists ------------------------------------------


@pytest.mark.parametrize(
    ("conftest", "message"),
    [
        (
            "def pytest_graphql_configure(config):\n"
            "    raise RuntimeError('configure hook failed')\n",
            "configure hook failed",
        ),
        (
            "import pytest\n\n\n"
            '@pytest.fixture(scope="session")\n'
            "def gql_config():\n"
            "    return {'not': 'a config'}\n",
            "gql_config fixture must return a ClientConfig",
        ),
        (
            "import pytest\n\n\n"
            '@pytest.fixture(scope="session")\n'
            "def gql_url():\n"
            "    raise RuntimeError('url fixture failed')\n",
            "url fixture failed",
        ),
    ],
)
def test_a_failure_before_the_transport_opens_no_pool(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
    conftest: str,
    message: str,
) -> None:
    result = _run_against_server(pytester, monkeypatch, conftest=conftest)
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert _of(derives, "pool-open") == []
    assert _of(derives, "derive") == []
    assert _of(derives, "pool-close") == []


def test_a_missing_endpoint_opens_no_pool(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
) -> None:
    result = run_inner(pytester, monkeypatch, fake=False, url=False, test=PASSING)
    result.assert_outcomes(errors=2)
    assert _of(derives, "pool-open") == []


@pytest.mark.parametrize(
    ("ini", "module", "message"),
    [
        (
            "gql_schema_source = no_such_module_xyz.SOURCE\n",
            "",
            "there is no module named",
        ),
        (
            "gql_schema_source = my_sources.NOT_THERE\n",
            "SOURCE = 1\n",
            "has no attribute",
        ),
        (
            "gql_schema_source = my_sources.SOURCE\n",
            "SOURCE = 1\n",
            "not a SchemaSource",
        ),
    ],
)
def test_a_schema_source_that_cannot_be_used_opens_no_pool(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
    ini: str,
    module: str,
    message: str,
) -> None:
    pytester.makepyfile(my_sources=module or "pass\n")
    result = _run_against_server(pytester, monkeypatch, ini=ini)
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert _of(derives, "pool-open") == []


# -- failures after the pool exists: the session teardown closes it -----------


@pytest.mark.parametrize(
    ("conftest", "message"),
    [
        (
            "def pytest_graphql_schema_loaded(schema, source):\n"
            "    raise RuntimeError('schema hook failed')\n",
            "schema hook failed",
        ),
        (
            "def pytest_graphql_register_scalars(registry):\n"
            "    raise RuntimeError('scalar hook failed')\n",
            "scalar hook failed",
        ),
        (
            "import pytest\n\n\n"
            '@pytest.fixture(scope="session")\n'
            "def gql_scalars():\n"
            "    return object()\n",
            "gql_scalars fixture must return a ScalarRegistry",
        ),
    ],
)
def test_a_hook_that_fails_after_the_transport_leaves_the_pool_to_its_teardown(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
    conftest: str,
    message: str,
) -> None:
    result = _run_against_server(pytester, monkeypatch, conftest=conftest)
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert len(_of(derives, "derive")) == 2
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


def test_a_custom_source_that_fails_to_load_closes_the_probe_and_the_pool(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
) -> None:
    pytester.makepyfile(
        my_sources="""
from pytest_graphql._core.errors import SchemaError


class Failing:
    fingerprint = "failing"

    def load(self):
        raise SchemaError("the source could not be read")


SOURCE = Failing()
"""
    )
    result = _run_against_server(
        pytester, monkeypatch, ini="gql_schema_source = my_sources.SOURCE\n"
    )
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*the source could not be read*"])
    # The schema fixture closed its probe when the load failed, and the
    # session fixture's teardown closed the pool. Once each, in all.
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


def test_the_session_teardown_is_registered_before_the_pool_opens(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[Any, ...]],  # noqa: F811
) -> None:
    """9.7: the pytest path closes what the opening step returns.

    The opening step sweeps its own list when it raises, so a failure inside
    it shows nothing about where the fixture registers its teardown. This one
    lets the step succeed and then loses the transport it opened, which is the
    boundary 9.7 names: the pool exists, the cleanup list holds it, and no
    frame holds the transport. Only a teardown registered before the step ran,
    over the very list the step was given, can still close the pool.
    """
    passed: list[object] = []
    real = fixtures._open_session_transport

    def losing(config: Any, cleanup: list[Any]) -> NoReturn:
        passed.append(cleanup)
        real(config, cleanup)
        raise RuntimeError("the transport was lost after it was opened")

    monkeypatch.setattr(fixtures, "_open_session_transport", losing)
    result = _run_against_server(pytester, monkeypatch)
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*the transport was lost after it was opened*"])
    assert len(passed) == 1
    assert isinstance(passed[0], list)
    _assert_one_pool_closed_last(events)


# -- a supplied schema is never introspected ----------------------------------

SUPPLIED_SCHEMA = """
import pytest
from graphql import build_schema as parse_sdl


@pytest.fixture(scope="session")
def gql_schema():
    return parse_sdl("type Query { ping: String }")
"""


def _refusing_server() -> tuple[list[bytes], Any]:
    """An endpoint that refuses introspection, and the requests it received."""
    received: list[bytes] = []

    def respond(body: bytes, _headers: dict[str, str]) -> Any:
        received.append(body)
        return _failing_introspection(body, _headers)

    return received, respond


def test_a_supplied_gql_schema_sends_no_introspection_request(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
) -> None:
    """The ``gql_schema`` override must prevent the work its default does.

    The endpoint refuses introspection, so a default transport that loads the
    schema before the override is read fails here, and so does any other
    fixture that asks the endpoint for a schema the project already supplied.
    """
    received, respond = _refusing_server()
    with local_server(respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            conftest=SUPPLIED_SCHEMA,
            test="""
def test_one(gql):
    assert "ping" in gql.schema.query_type.fields


def test_two(gql):
    assert "ping" in gql.schema.query_type.fields
""",
            args=(f"--gql-url={url}",),
        )
    result.assert_outcomes(passed=2)
    assert received == []
    # The session transport and one per test. There is no schema probe.
    assert len(_of(derives, "derive")) == 3
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


def test_a_supplied_gql_schema_needs_no_reachable_endpoint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        url=False,
        conftest=SUPPLIED_SCHEMA,
        test="""
def test_one(gql):
    assert gql.schema.query_type.name == "Query"
""",
        args=(f"--gql-url={closed_port_url()}",),
    )
    result.assert_outcomes(passed=1)


def test_a_failed_default_introspection_still_closes_the_probe_and_the_pool(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
) -> None:
    received, respond = _refusing_server()
    with local_server(respond) as url:
        result = run_inner(
            pytester,
            monkeypatch,
            fake=False,
            url=False,
            test=PASSING,
            args=(f"--gql-url={url}",),
        )
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*introspection disabled*"])
    assert len(received) == 1
    # The session transport, and the probe that failed to load the schema.
    assert len(_of(derives, "derive")) == 2
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


# -- failures inside a test: its transport still closes -----------------------


@pytest.mark.parametrize(
    ("hook", "argument"),
    [("before_request", "request"), ("after_response", "response")],
)
def test_a_request_hook_that_raises_fails_its_test_and_still_closes_its_transport(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
    hook: str,
    argument: str,
) -> None:
    result = _run_against_server(
        pytester,
        monkeypatch,
        # It fails on its first call only, which is the first test's.
        conftest=f"""
FAILED = []


def pytest_graphql_{hook}({argument}):
    if not FAILED:
        FAILED.append(1)
        raise RuntimeError("{hook} hook failed")
""",
    )
    result.assert_outcomes(failed=1, passed=1)
    result.stdout.fnmatch_lines([f"*{hook} hook failed*"])
    assert len(_of(derives, "derive")) == 4
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


def test_a_client_construction_failure_still_closes_the_derived_transport(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    derives: list[tuple[Any, ...]],
) -> None:
    """The M5d guarantee, with the new inputs all present and in use."""

    def failing_client(**_: Any) -> NoReturn:
        raise RuntimeError("client construction failed")

    monkeypatch.setattr(plugin, "GraphQLClient", failing_client)
    result = _run_against_server(
        pytester,
        monkeypatch,
        conftest="""
import pytest

from pytest_graphql import BearerAuth


@pytest.fixture
def gql_auth():
    return BearerAuth("token-0123456789")


@pytest.fixture
def gql_headers():
    return {"X-A": "1"}
""",
    )
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*client construction failed*"])
    assert len(_of(derives, "derive")) == 4
    _assert_every_derived_transport_closed_once(derives)
    _assert_one_pool_closed_last(derives)


# -- a shared transport is closed by nobody here ------------------------------


def test_a_shared_transport_survives_clones_hooks_headers_and_auth(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_probe.EVENTS.clear()
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest

from pytest_graphql import BearerAuth
from tests.unit import plugin_probe


@pytest.fixture
def gql_auth():
    return BearerAuth("token-0123456789")


@pytest.fixture
def gql_headers():
    return {"X-A": "1"}


def pytest_graphql_before_request(request):
    return None
""",
        test="""
from tests.unit import plugin_probe


def test_one(gql):
    plugin_probe.EVENTS.append(("shared", gql.transport))
    with gql.as_("other-0123456789") as clone:
        assert clone.owns_transport is False
        assert clone.query("pingScalar") is True
    assert gql.owns_transport is False
    assert gql.query("pingScalar") is True


def test_two(gql):
    plugin_probe.EVENTS.append(("shared", gql.transport))
    assert gql.with_headers({"X-B": "2"}).query("pingScalar") is True
""",
    )
    result.assert_outcomes(passed=2)
    first, second = [e[1] for e in plugin_probe.EVENTS if e[0] == "shared"]
    assert first is second
    assert first.close_calls == 0
