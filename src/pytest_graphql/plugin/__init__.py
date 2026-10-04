"""The pytest layer.

This package is the only place in the distribution that may import pytest. It is
loaded through the ``pytest11`` entry point, which only pytest reads, so the
entry point is inert when pytest is absent.

The package is six modules:

- :mod:`~pytest_graphql.plugin.options` is the option table. Ini options,
  environment variables and CLI flags are generated from it, and so are the
  parsing and the error messages (DESIGN section 2, "Settings precedence").
- :mod:`~pytest_graphql.plugin.hookspecs` declares the six
  ``pytest_graphql_*`` hooks and delivers them (section 3, "pytest hooks").
- :mod:`~pytest_graphql.plugin.fixtures` holds the fixtures and the ownership
  rules of section 9.
- :mod:`~pytest_graphql.plugin.reporting` writes everything the plugin prints:
  the header, the failure section, the matcher diff, the call log and the
  summary, and it hands the xdist workers' facts to the controller (SPEC 7.5
  and 7.6).
- :mod:`~pytest_graphql.plugin.session` holds the run id and the worker id.
- This module is the entry point. It registers the options and the hooks, checks
  the settings once when pytest configures, and exposes the fixtures to pytest.

The fixtures and the reporting hooks are re-exported here because pytest finds
them in the module it registered. ``GraphQLClient`` is imported here too, and the
per-test ``gql`` fixture builds its client through this name, so a test can
substitute the constructor in one place.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core.client import GraphQLClient
from pytest_graphql.plugin import hookspecs, options, reporting
from pytest_graphql.plugin.fixtures import (  # noqa: F401  (pytest reads these here)
    _gql_ready_scalars,
    _gql_ready_schema,
    _gql_session_config,
    _gql_unique_source,
    gql,
    gql_auth,
    gql_config,
    gql_headers,
    gql_scalars,
    gql_schema,
    gql_schema_source,
    gql_seed,
    gql_transport,
    gql_url,
)
from pytest_graphql.plugin.options import Settings, settings_of
from pytest_graphql.plugin.reporting import (  # noqa: F401  (pytest reads these here)
    pytest_assertrepr_compare,
    pytest_configure_node,
    pytest_fixture_setup,
    pytest_report_header,
    pytest_runtest_makereport,
    pytest_runtest_protocol,
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_testnodedown,
    pytest_unconfigure,
)
from pytest_graphql.plugin.session import run_id as _run_id  # noqa: F401
from pytest_graphql.plugin.session import worker_id as _worker_id  # noqa: F401

__all__ = [
    "GraphQLClient",
    "Settings",
    "pytest_addhooks",
    "pytest_addoption",
    "pytest_configure",
    "settings_of",
]


def pytest_addoption(parser: pytest.Parser) -> None:
    options.register(parser)


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspecs)


def pytest_configure(config: pytest.Config) -> None:
    """Read and check every source now, so a bad value stops the run at the start.

    A refusal is a ``UsageError``, which pytest reports without a traceback and
    answers with its usage-error exit code.
    """
    settings_of(config)
    reporting.configure(config)
