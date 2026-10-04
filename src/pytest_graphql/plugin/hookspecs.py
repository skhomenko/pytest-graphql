"""The six pytest hooks, and the code that delivers them (DESIGN section 3).

Each hook has a classification, and the classification is part of the
contract.

Observational: ``pytest_graphql_configure``, ``pytest_graphql_schema_loaded``
and ``pytest_graphql_register_scalars``. Every implementation runs, and a
return value is ignored. ``pytest_graphql_configure`` receives a mutable
``ClientConfig``, so an implementation changes the configuration in place.

Ordered folds: ``pytest_graphql_before_request`` and
``pytest_graphql_after_response``. Every implementation runs in pytest hook
order, and a return value that is not ``None`` becomes the input of the next
implementation, so two plugins cannot discard each other's work. They are not
``firstresult``. pluggy calls every implementation with the same arguments, so
it cannot fold. :func:`fold` therefore walks the implementations itself, in the
order pluggy would call them, and a hook wrapper has no place in that walk and
is refused.

``pytest_graphql_report_section`` is delivered by the reporting module when the
report of a failed test is made. Every implementation runs in hook order, and each
non-``None`` result is added under a heading of its own.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, TypeVar, cast

import pytest
from graphql import GraphQLSchema
from pluggy import HookCaller, HookspecMarker

from pytest_graphql._core.client import ClientConfig
from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.factory import ScalarRegistry, ScalarSpec
from pytest_graphql._core.response import GraphQLResponse
from pytest_graphql._core.schema.source import SchemaSource

if TYPE_CHECKING:
    from pytest_graphql.plugin.reporting import CallTrace

T = TypeVar("T")

#: pytest's own marker, which is a pluggy marker for the project name "pytest".
#: Built here so the module does not depend on pytest re-exporting it.
hookspec = HookspecMarker("pytest")

#: The six names, in the order the design document lists them.
HOOK_NAMES = (
    "pytest_graphql_configure",
    "pytest_graphql_schema_loaded",
    "pytest_graphql_before_request",
    "pytest_graphql_after_response",
    "pytest_graphql_register_scalars",
    "pytest_graphql_report_section",
)


@hookspec
def pytest_graphql_configure(config: ClientConfig) -> None:
    """Observational. Adjust the effective configuration, once per session.

    It runs after the ``gql_config`` fixture and the CLI flags were applied and
    before the session transport is built, so what it sets is final.
    """


@hookspec
def pytest_graphql_schema_loaded(schema: GraphQLSchema, source: SchemaSource) -> None:
    """Observational. The schema was loaded, once per session."""


@hookspec
def pytest_graphql_before_request(request: RequestInfo) -> RequestInfo | None:
    """Ordered fold. Return a replacement request, or ``None`` for no change."""


@hookspec
def pytest_graphql_after_response(
    response: GraphQLResponse[Any],
) -> GraphQLResponse[Any] | None:
    """Ordered fold. Return a replacement response, or ``None`` for no change."""


@hookspec
def pytest_graphql_register_scalars(registry: ScalarRegistry) -> None:
    """Observational. Register custom scalars, once per session.

    A name registered twice replaces the first registration and warns.
    """


@hookspec
def pytest_graphql_report_section(
    response: GraphQLResponse[Any], item: pytest.Item, config: pytest.Config
) -> str | None:
    """Each implementation adds a section to the failure report of a test.

    It runs once for each failed test that made a GraphQL call and received a
    response, at the first phase that failed, with the last response the test
    received. Return the text of a
    section, or ``None`` for no section. Every implementation runs and each
    result is added under its own heading, in hook order. Text that shows a
    value the request redacted is withheld.
    """


def fold(caller: HookCaller, name: str, value: T) -> T:
    """Run every implementation of a fold hook, each on the last one's result.

    ``name`` is the one argument the hook takes. An implementation that
    declares fewer arguments than the hook is called without it, as pluggy does.
    An exception raised by an implementation propagates unchanged.
    """
    for implementation in reversed(caller.get_hookimpls()):
        # ``wrapper`` exists from pluggy 1.2. Older pluggy has ``hookwrapper`` only.
        if getattr(implementation, "wrapper", False) or implementation.hookwrapper:
            raise pytest.UsageError(
                f"pytest-graphql: {caller.name} does not support hook wrappers, "
                f"because each implementation must receive the result of the "
                f"one before it. Remove the wrapper from "
                f"{implementation.plugin_name}."
            )
        arguments = {name: value} if name in implementation.argnames else {}
        replacement = implementation.function(**arguments)
        if replacement is not None:
            value = cast("T", replacement)
    return value


class HookMiddleware:
    """The one middleware that delivers the two request hooks (DESIGN section 1).

    The plugin appends it to the client's chain after any middleware, so
    user middleware runs before the pytest hooks on the way out and after them
    on the way back.

    Given a trace, it also tells the trace which request was sent and which
    response came back, once the hooks have run, so a failure report can read the
    last response and a failed assertion can scrub with the secrets of the
    requests the test sent. The trace is bounded and goes when the test ends.
    """

    def __init__(self, config: pytest.Config, trace: CallTrace | None = None) -> None:
        self._hook = config.hook
        self._trace = trace

    def before_request(self, request: RequestInfo) -> RequestInfo | None:
        sent = fold(self._hook.pytest_graphql_before_request, "request", request)
        if self._trace is not None:
            self._trace.note_request(sent)
        return sent

    def after_response(
        self, response: GraphQLResponse[Any]
    ) -> GraphQLResponse[Any] | None:
        received = fold(self._hook.pytest_graphql_after_response, "response", response)
        if self._trace is not None:
            self._trace.note_response(received)
        return received


class ReplacingRegistry(ScalarRegistry):
    """A view of a registry that replaces a duplicate and warns, for the hooks.

    ``ScalarRegistry.register`` refuses a name that is already taken, which is
    right for one author and wrong for two plugins that cannot know about each
    other. The hook receives this view instead, over the same storage, so what
    an implementation registers lands in the session's registry. A caller that
    passes ``replace=True`` says it means to replace, so it gets no warning.
    """

    __slots__ = ()

    def __init__(self, target: ScalarRegistry) -> None:
        # One dict, shared, so the view and the registry can never disagree.
        self._specs = target._specs

    def register(self, spec: ScalarSpec, *, replace: bool = False) -> None:
        if not replace and isinstance(spec, ScalarSpec) and spec.name in self._specs:
            warnings.warn(
                f"scalar {spec.name!r} was registered twice. The later "
                "registration replaces the earlier one.",
                pytest.PytestWarning,
                stacklevel=2,
            )
        super().register(spec, replace=True)
