"""Helpers for the plugin tests that drive an inner pytest session.

``pytester.runpytest()`` runs the inner session in this process, so the inner
files import ``plugin_probe`` and append to the list the outer test reads. The
default inner project overrides ``gql_transport`` with a fake that opens no
socket, and ``gql_url`` with a label, so a test that is about one setting does
not need a server and does not set the URL as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from pytest_graphql._core.client import ClientConfig
from tests.unit import plugin_probe

LABEL_URL = "http://127.0.0.1:9/graphql"

#: Every inner session loads the plugins installed in the environment. The
#: hypothesis plugin is one, and loading it again in each of hundreds of
#: in-process sessions slows each one by about a second on CPython 3.10 and
#: ends the interpreter with ``none_dealloc`` at exit. The crash reproduces with
#: no pytest-graphql plugin at all, so it is not this project's. The inner
#: tests never use hypothesis, so they do not load it.
QUIET_INNER = ("-p", "no:hypothesispytest")

#: The session transport is a fake. It is the one object the inner tests read
#: back: its ``sent`` list holds every request, and ``timeouts`` the ceiling
#: each call was given.
FAKE_TRANSPORT = """
import pytest

from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema


@pytest.fixture(scope="session")
def gql_transport():
    return FakeGraphQLTransport(build_schema())
"""

URL_FIXTURE = f"""
import pytest


@pytest.fixture(scope="session")
def gql_url():
    return {LABEL_URL!r}
"""

#: An inner test that records the configuration its client received.
RECORD_CONFIG = """
from tests.unit import plugin_probe


def test_record(gql):
    plugin_probe.EVENTS.append(("config", gql.config))
"""


def run_inner(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    test: str = RECORD_CONFIG,
    conftest: str = "",
    ini: str = "",
    env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    fake: bool = True,
    url: bool = True,
) -> pytest.RunResult:
    """One inner session. ``conftest`` is appended to the default project's."""
    plugin_probe.EVENTS.clear()
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    pytester.makeconftest(
        ("" if not fake else FAKE_TRANSPORT)
        + ("" if not url else URL_FIXTURE)
        + "\n"
        + conftest
    )
    if ini:
        pytester.makeini("[pytest]\n" + ini)
    pytester.makepyfile(test_inner=test)
    return pytester.runpytest(*QUIET_INNER, *args)


def recorded(kind: str) -> list[tuple[Any, ...]]:
    """The inner events of one kind, in order."""
    return [event for event in plugin_probe.EVENTS if event[0] == kind]


def recorded_config() -> ClientConfig:
    """The configuration the last ``RECORD_CONFIG`` test saw."""
    found = recorded("config")
    assert found, "the inner test recorded no configuration"
    config = found[-1][1]
    assert isinstance(config, ClientConfig)
    return config
