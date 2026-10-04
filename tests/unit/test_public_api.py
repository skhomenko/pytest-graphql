"""The top-level package surface.

``docs/reference/DESIGN_DECISIONS.md``, "Top-level surface", lists what a user
imports from ``pytest_graphql`` itself. A name that exists in ``_core`` and
not here is not public API yet, whatever the documentation promises, so this
file asserts the promise against the package rather than against the private
module that implements it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import pytest_graphql

#: Every documented top-level name the implemented milestones now owe. The
#: rest of the documented surface (`GraphQLTestCase` and the full exception
#: hierarchy) lands with the milestones that implement it. The matcher helpers
#: arrived with M6, minus `approx`, which D4 dropped. The factory surface
#: (`DeterministicRandom`, `ScalarRegistry`, `ScalarSpec`, `unique` and the
#: `ScalarNotRegisteredError` it raises) arrived with M7. The execution
#: errors, `WaitTimeoutError` and `ExpectedErrorNotRaised` arrived with M8.
IMPLEMENTED_SURFACE = (
    "AUTO",
    "Auth",
    "BaseMiddleware",
    "BearerAuth",
    "ClientConfig",
    "CyclePolicy",
    "DeterministicRandom",
    "DiagnosticSnapshot",
    "ExpectedErrorNotRaised",
    "Field",
    "GraphQLClient",
    "GraphQLExecutionError",
    "GraphQLPartialDataError",
    "GraphQLResponse",
    "HeaderAuth",
    "Matcher",
    "Middleware",
    "Node",
    "NodeList",
    "RequestInfo",
    "ScalarNotRegisteredError",
    "ScalarRegistry",
    "ScalarSpec",
    "SchemaSource",
    "Selection",
    "SelectionPolicy",
    "Transport",
    "WaitTimeoutError",
    "__version__",
    "absent",
    "any_length",
    "any_value",
    "build_client",
    "contains",
    "gt",
    "gte",
    "length",
    "lt",
    "lte",
    "matches",
    "one_of",
    "unique",
    "unordered",
)


@pytest.mark.parametrize("name", IMPLEMENTED_SURFACE)
def test_the_documented_name_is_importable_from_the_package(name: str) -> None:
    assert hasattr(pytest_graphql, name), (
        f"{name} is documented as top-level public API and is not exported."
    )
    assert name in pytest_graphql.__all__


def test_everything_exported_is_reachable() -> None:
    # `__all__` is what `from pytest_graphql import *` promises, so a name in
    # it that the module does not hold is an import error waiting for a user.
    missing = [
        name for name in pytest_graphql.__all__ if not hasattr(pytest_graphql, name)
    ]

    assert missing == []


def test_the_factory_is_usable_through_the_package_alone() -> None:
    # F04's real cost: the principal feature of M5c reachable only through a
    # private module is not delivered. This uses the public path only.
    from pytest_graphql import ClientConfig, build_client

    config = ClientConfig(max_depth=2)

    assert config.max_depth == 2
    assert callable(build_client)


def test_importing_the_package_imports_no_pytest() -> None:
    # C41 in its importable form. The core is pytest-free, so the top-level
    # package it re-exports must import in an interpreter that never loads
    # pytest. Run out of process, because this suite has pytest imported.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pytest_graphql, sys; print('pytest' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"
