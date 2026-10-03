"""The ``gql`` fixture supplies ``gql.fake`` with its per-test inputs.

The node id is the test's pytest node id. The run id and worker id belong to
the session, so one ``UniqueSource`` serves every test of a session in a
process. Under xdist the worker id is the worker's own, and without it the
worker id is ``"main"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pytest_graphql import plugin
from pytest_graphql._core.factory import FakeNamespace, UniqueSource
from tests.factory.schemas import SCHEMA
from tests.unit import plugin_probe

CONFTEST = """
import pytest

from tests.factory.schemas import SCHEMA
from tests.schema.fake_transport import FakeGraphQLTransport


@pytest.fixture(scope="session")
def gql_transport():
    return FakeGraphQLTransport(SCHEMA)
"""

INNER = """
from pytest_graphql import unique
from tests.unit import plugin_probe


def record(gql, test):
    plugin_probe.EVENTS.append(
        (
            "fake",
            test,
            gql.fake.CreateUserInput(),
            gql.fake.CreateUserInput(name=unique())["name"],
            gql.fake.CreateUserInput(name=unique())["name"],
        )
    )


def test_a(gql):
    record(gql, "a")


def test_b(gql):
    record(gql, "b")
"""


def _run(pytester: pytest.Pytester) -> dict[str, tuple[Any, ...]]:
    plugin_probe.EVENTS.clear()
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner=INNER)
    result = pytester.runpytest("--gql-url=http://127.0.0.1:9/graphql")
    result.assert_outcomes(passed=2)
    found = {event[1]: event[2:] for event in plugin_probe.EVENTS if event[0] == "fake"}
    plugin_probe.EVENTS.clear()
    return found


def test_fake_is_seeded_from_the_nodeid_of_each_test(pytester: pytest.Pytester) -> None:
    found = _run(pytester)
    for test in ("a", "b"):
        expected = FakeNamespace(
            SCHEMA,
            None,
            global_seed=0,
            node_id=f"test_inner.py::test_{test}",
            unique_source=UniqueSource("r"),
        ).CreateUserInput()
        assert found[test][0] == expected
    assert found["a"][0] != found["b"][0]


def test_seeded_data_repeats_across_runs_and_unique_does_not(
    pytester: pytest.Pytester,
) -> None:
    first = _run(pytester)
    second = _run(pytester)
    for test in ("a", "b"):
        assert first[test][0] == second[test][0]
        assert not set(first[test][1:]) & set(second[test][1:])


def test_unique_values_never_repeat_within_a_session(pytester: pytest.Pytester) -> None:
    found = _run(pytester)
    values = [value for test in found.values() for value in test[1:]]
    assert len(values) == 4
    assert len(set(values)) == 4


# -- the session inputs -----------------------------------------------------


def _config(**workerinput: Any) -> Any:
    if not workerinput:
        return SimpleNamespace()
    return SimpleNamespace(workerinput=workerinput)


def test_the_worker_id_is_main_outside_xdist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert plugin._worker_id(_config()) == "main"


def test_the_worker_id_comes_from_the_xdist_worker_input() -> None:
    assert plugin._worker_id(_config(workerid="gw3", testrunuid="u")) == "gw3"


def test_the_worker_id_falls_back_to_the_xdist_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    assert plugin._worker_id(_config()) == "gw7"


@pytest.mark.parametrize("bad", ["", 3, None])
def test_a_malformed_worker_input_falls_back(
    bad: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert plugin._worker_id(_config(workerid=bad)) == "main"


def test_the_run_id_is_shared_by_every_xdist_worker() -> None:
    assert plugin._run_id(_config(testrunuid="abc123")) == "abc123"
    assert plugin._run_id(_config(testrunuid="abc123", workerid="gw1")) == "abc123"


def test_the_run_id_is_new_for_each_session_without_xdist() -> None:
    assert plugin._run_id(_config()) != plugin._run_id(_config())
    assert plugin._run_id(_config()) != ""


@pytest.mark.parametrize("bad", ["", 3, None])
def test_a_malformed_run_id_input_falls_back_to_a_fresh_one(bad: object) -> None:
    assert plugin._run_id(_config(testrunuid=bad)) != ""


def test_every_test_of_a_session_shares_one_unique_source(
    pytester: pytest.Pytester,
) -> None:
    # One source per session and process keeps one counter. A source made for
    # each test would restart it, so a rerun of one node id, which a rerun
    # plugin makes in one process, would repeat its first run's values.
    plugin_probe.EVENTS.clear()
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        test_inner="""
from pytest_graphql import unique
from tests.unit import plugin_probe


def record(gql):
    gql.fake.CreateUserInput(name=unique())
    plugin_probe.EVENTS.append(("source", gql._fake_context.unique_source))


def test_a(gql):
    record(gql)


def test_b(gql):
    record(gql)
"""
    )
    result = pytester.runpytest("--gql-url=http://127.0.0.1:9/graphql")
    result.assert_outcomes(passed=2)
    sources = [event[1] for event in plugin_probe.EVENTS if event[0] == "source"]
    plugin_probe.EVENTS.clear()
    assert len(sources) == 2
    assert sources[0] is sources[1]
    assert sources[0].issued == 2
