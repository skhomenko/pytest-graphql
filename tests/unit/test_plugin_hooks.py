"""The six hooks: declared, classified and delivered (DESIGN section 3).

Every classification below is part of the contract, so each one has a test that
fails when the classification changes:

- observational hooks run every implementation and ignore what they return;
- the two fold hooks run every implementation in pytest hook order and feed each
  non-``None`` result to the next, and are not ``firstresult``;
- a scalar registered twice replaces the first and warns.

Hook order is whatever pluggy gives the same implementations, so the fold tests
compare their order with an observational hook's in the same session. The
sessions register small plugin objects from a conftest, in a fixed order, with
and without ``tryfirst`` and ``trylast``.
"""

from __future__ import annotations

import inspect
import typing
from itertools import pairwise

import pytest

from pytest_graphql.plugin import hookspecs
from tests.unit.plugin_inner import QUIET_INNER, recorded, recorded_config, run_inner

# -- the declarations ---------------------------------------------------------

#: DESIGN section 3, parameter names in order.
SIGNATURES = {
    "pytest_graphql_configure": ["config"],
    "pytest_graphql_schema_loaded": ["schema", "source"],
    "pytest_graphql_before_request": ["request"],
    "pytest_graphql_after_response": ["response"],
    "pytest_graphql_register_scalars": ["registry"],
    "pytest_graphql_report_section": ["response", "item", "config"],
}
FOLDS = ("pytest_graphql_before_request", "pytest_graphql_after_response")


def test_the_six_hooks_are_exactly_the_designed_set() -> None:
    assert set(hookspecs.HOOK_NAMES) == set(SIGNATURES)
    assert len(hookspecs.HOOK_NAMES) == 6


@pytest.mark.parametrize("name", SIGNATURES)
def test_each_hook_has_the_designed_parameters(name: str) -> None:
    parameters = list(inspect.signature(getattr(hookspecs, name)).parameters)
    assert parameters == SIGNATURES[name]


@pytest.mark.parametrize("name", SIGNATURES)
def test_each_hook_signature_is_fully_annotated(name: str) -> None:
    function = getattr(hookspecs, name)
    hints = typing.get_type_hints(function)
    assert set(hints) == {*SIGNATURES[name], "return"}


def test_the_return_types_are_the_designed_ones() -> None:
    returns = {
        name: inspect.signature(getattr(hookspecs, name)).return_annotation
        for name in SIGNATURES
    }
    assert returns == {
        "pytest_graphql_configure": "None",
        "pytest_graphql_schema_loaded": "None",
        "pytest_graphql_before_request": "RequestInfo | None",
        "pytest_graphql_after_response": "GraphQLResponse[Any] | None",
        "pytest_graphql_register_scalars": "None",
        "pytest_graphql_report_section": "str | None",
    }


@pytest.mark.parametrize("name", SIGNATURES)
def test_no_hook_is_firstresult_or_historic(
    pytester: pytest.Pytester, name: str
) -> None:
    # A fold that were firstresult would stop at the first plugin and discard
    # the others' work, which is the defect the fold exists to prevent.
    caller = getattr(pytester.parseconfig(*QUIET_INNER).hook, name)
    assert caller.has_spec()
    assert list(caller.spec.argnames) == SIGNATURES[name]
    assert not caller.spec.opts.get("firstresult")
    assert not caller.spec.opts.get("historic")


# -- ordering -----------------------------------------------------------------

#: Four plugins, registered in this order: a plugin that asks to run last, two
#: plain ones, and a plugin that asks to run first. Plain implementations run in
#: reverse registration order, so the expected order is first, b, a, last.
PLUGINS = """
import dataclasses

import pytest

from tests.unit import plugin_probe


def make(name, **marks):
    class Plugin:
        @pytest.hookimpl(**marks)
        def pytest_graphql_register_scalars(self, registry):
            plugin_probe.EVENTS.append(("observed", name))
            return "ignored"

        @pytest.hookimpl(**marks)
        def pytest_graphql_before_request(self, request):
            plugin_probe.EVENTS.append(("folded", name, tuple(request.headers)))
            return dataclasses.replace(
                request, headers={**request.headers, "X-" + name: "1"}
            )

        @pytest.hookimpl(**marks)
        def pytest_graphql_after_response(self, response):
            plugin_probe.EVENTS.append(("responded", name, response.duration_ms))
            return dataclasses.replace(
                response, duration_ms=response.duration_ms + 1000.0
            )

    return Plugin()


def pytest_configure(config):
    register = config.pluginmanager.register
    register(make("last", trylast=True), "p-last")
    register(make("a"), "p-a")
    register(make("b"), "p-b")
    register(make("first", tryfirst=True), "p-first")
"""
EXPECTED_ORDER = ["first", "b", "a", "last"]

ORDER_TEST = """
from tests.unit import plugin_probe


def test_call(gql):
    response = gql.query("pingScalar", raw=True)
    plugin_probe.EVENTS.append(("headers", tuple(gql.transport.sent[-1].headers)))
    plugin_probe.EVENTS.append(("duration", response.duration_ms))
"""


def test_every_implementation_of_an_observational_hook_runs_and_returns_are_ignored(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, conftest=PLUGINS, test=ORDER_TEST)
    result.assert_outcomes(passed=1)
    # Each returned a string, which would end a firstresult call at the first.
    assert [event[1] for event in recorded("observed")] == EXPECTED_ORDER


def test_a_fold_runs_every_implementation_in_the_order_of_an_observational_hook(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, conftest=PLUGINS, test=ORDER_TEST)
    result.assert_outcomes(passed=1)
    observed = [event[1] for event in recorded("observed")]
    assert [event[1] for event in recorded("folded")] == observed
    assert [event[1] for event in recorded("responded")] == observed


def test_each_fold_step_receives_the_result_of_the_one_before(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, conftest=PLUGINS, test=ORDER_TEST)
    result.assert_outcomes(passed=1)
    seen = [event[2] for event in recorded("folded")]
    # The first sees the request as built. Every later one also sees the
    # header each earlier plugin added, so nothing was discarded.
    assert "X-first" not in seen[0]
    for index, name in enumerate(EXPECTED_ORDER[:-1]):
        assert f"X-{name}" in seen[index + 1]
        assert all(
            f"X-{earlier}" in seen[index + 1] for earlier in EXPECTED_ORDER[: index + 1]
        )
    (_, final_headers) = recorded("headers")[0]
    assert all(f"X-{name}" in final_headers for name in EXPECTED_ORDER)


def test_the_response_fold_accumulates_across_implementations(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, conftest=PLUGINS, test=ORDER_TEST)
    result.assert_outcomes(passed=1)
    durations = [event[2] for event in recorded("responded")]
    # Each saw 1000 more than the one before, then the test saw all four added.
    assert [round(b - a) for a, b in pairwise(durations)] == [
        1000,
        1000,
        1000,
    ]
    assert round(recorded("duration")[0][1] - durations[0]) == 4000


def test_none_means_no_change_and_an_exception_propagates_unchanged(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=PLUGINS
        + """

class Silent:
    def pytest_graphql_before_request(self, request):
        return None


class Stops:
    def pytest_graphql_before_request(self, request):
        if request.operation == "stop":
            raise ValueError("stopped by a hook")


def pytest_configure(config, _extra=pytest_configure):
    _extra(config)
    config.pluginmanager.register(Silent(), "silent")
    config.pluginmanager.register(Stops(), "stops")
""",
        test="""
import pytest

from tests.unit import plugin_probe


def test_none_and_exceptions(gql):
    gql.query("pingScalar")
    plugin_probe.EVENTS.append(("headers", tuple(gql.transport.sent[-1].headers)))
    before = len(gql.transport.sent)
    with pytest.raises(ValueError, match="stopped by a hook") as raised:
        gql.query("pingScalar", operation_name="stop")
    plugin_probe.EVENTS.append(("raised", type(raised.value).__name__))
    plugin_probe.EVENTS.append(("sent", len(gql.transport.sent) - before))
""",
    )
    result.assert_outcomes(passed=1)
    headers = recorded("headers")[0][1]
    assert all(f"X-{name}" in headers for name in EXPECTED_ORDER)
    assert recorded("raised")[0][1] == "ValueError"
    # The call was aborted before the transport, so nothing was sent.
    assert recorded("sent")[0][1] == 0


def test_a_hook_wrapper_on_a_fold_is_refused_with_a_clear_message(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_graphql_before_request(request):
    yield
""",
        test="""
def test_call(gql):
    gql.query("pingScalar")
""",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*pytest_graphql_before_request does not support hook wrappers*"]
    )


def test_the_hooks_reach_clones_and_raw_documents(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
from tests.unit import plugin_probe


def pytest_graphql_before_request(request):
    plugin_probe.EVENTS.append(("hooked", request.operation))
""",
        test="""
def test_calls(gql):
    gql.query("pingScalar")
    gql.as_("token-0123456789").query("pingScalar")
    gql.with_headers({"X-A": "1"}).query("pingScalar")
    gql.anonymous().execute("query Raw { pingScalar }")
""",
    )
    result.assert_outcomes(passed=1)
    assert [event[1] for event in recorded("hooked")] == [
        "pingScalar",
        "pingScalar",
        "pingScalar",
        "Raw",
    ]


def test_an_implementation_may_take_fewer_arguments_than_the_hook(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
from tests.unit import plugin_probe


def pytest_graphql_before_request():
    plugin_probe.EVENTS.append(("bare", "before"))


def pytest_graphql_after_response():
    plugin_probe.EVENTS.append(("bare", "after"))
""",
        test="""
def test_call(gql):
    gql.query("pingScalar")
""",
    )
    result.assert_outcomes(passed=1)
    assert [event[1] for event in recorded("bare")] == ["before", "after"]


def test_an_implementation_that_does_not_match_the_hook_is_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The signature in the design document is the contract: pluggy refuses an
    # implementation that names an argument the hook does not have.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
def pytest_graphql_before_request(req):
    return None
""",
    )
    assert result.ret != 0
    output = result.stdout.str() + result.stderr.str()
    assert "pytest_graphql_before_request" in output
    assert "req" in output


def test_report_section_is_declared_and_collects_every_implementation(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its implementations are called at M9b. Here it must exist, validate an
    # implementation against its signature, and call every one of them.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
def pytest_graphql_report_section(response, item, config):
    return "section one"


class Second:
    def pytest_graphql_report_section(self, response, item):
        return "section two"


def pytest_configure(config):
    config.pluginmanager.register(Second(), "second")
""",
        test="""
from tests.unit import plugin_probe


def test_sections(gql, request):
    found = request.config.hook.pytest_graphql_report_section(
        response=None, item=request.node, config=request.config
    )
    plugin_probe.EVENTS.append(("sections", sorted(found)))
""",
    )
    result.assert_outcomes(passed=1)
    assert recorded("sections")[0][1] == ["section one", "section two"]


# -- the observational hooks, once per session --------------------------------


def test_configure_runs_once_per_session_and_sees_the_flags_already_applied(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
from tests.unit import plugin_probe


def pytest_graphql_configure(config):
    plugin_probe.EVENTS.append(("configure", type(config).__name__, config.max_depth))
""",
        test="""
def test_one(gql):
    pass


def test_two(gql):
    pass


def test_three(gql):
    pass
""",
        args=("--gql-max-depth=9",),
    )
    result.assert_outcomes(passed=3)
    assert recorded("configure") == [("configure", "ClientConfig", 9)]


def test_a_configure_implementation_changes_the_configuration_in_place(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hooks sit above the flags: a hook is code the project installed, and it
    # sees the flags already applied.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
def pytest_graphql_configure(config):
    config.timeout = 77.0
    config.headers = {**config.headers, "X-From-Hook": "yes"}
""",
        args=("--gql-timeout=5",),
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    assert config.timeout == 77.0
    assert config.headers["X-From-Hook"] == "yes"


def test_every_configure_implementation_runs_and_sees_the_one_before(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
from tests.unit import plugin_probe


class Plugin:
    def __init__(self, name):
        self.name = name

    def pytest_graphql_configure(self, config):
        plugin_probe.EVENTS.append(("seen", self.name, config.retries))
        config.retries += 1


def pytest_configure(config):
    config.pluginmanager.register(Plugin("a"), "a")
    config.pluginmanager.register(Plugin("b"), "b")
""",
        test="""
from tests.unit import plugin_probe


def test_retries(gql):
    plugin_probe.EVENTS.append(("final", gql.config.retries))
""",
    )
    result.assert_outcomes(passed=1)
    assert [event[1:] for event in recorded("seen")] == [("b", 2), ("a", 3)]
    assert recorded("final")[0][1] == 4


def test_schema_loaded_runs_once_per_session_with_the_schema_and_the_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
from graphql import GraphQLSchema

from tests.unit import plugin_probe


def pytest_graphql_schema_loaded(schema, source):
    plugin_probe.EVENTS.append(
        ("loaded", isinstance(schema, GraphQLSchema), source.fingerprint)
    )
""",
        test="""
def test_one(gql):
    pass


def test_two(gql):
    pass
""",
    )
    result.assert_outcomes(passed=2)
    assert recorded("loaded") == [("loaded", True, "introspection:endpoint")]


def test_schema_loaded_runs_when_the_project_overrides_gql_schema(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest
from graphql import build_schema as parse_sdl

from tests.unit import plugin_probe


@pytest.fixture(scope="session")
def gql_schema():
    return parse_sdl("type Query { ping: String }")


def pytest_graphql_schema_loaded(schema, source):
    plugin_probe.EVENTS.append(("loaded", sorted(schema.type_map)[-1]))
""",
        test="""
def test_one(gql):
    assert "ping" in gql.schema.query_type.fields
""",
    )
    result.assert_outcomes(passed=1)
    assert len(recorded("loaded")) == 1


def test_register_scalars_runs_once_per_session_even_for_an_overridden_registry(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest

from pytest_graphql import ScalarRegistry, ScalarSpec
from tests.unit import plugin_probe


@pytest.fixture(scope="session")
def gql_scalars():
    registry = ScalarRegistry()
    registry.register(ScalarSpec("Own", serialize=str, fake=lambda rng: "own"))
    return registry


def pytest_graphql_register_scalars(registry):
    plugin_probe.EVENTS.append(("register", sorted(registry)))
    registry.register(ScalarSpec("FromHook", serialize=str, fake=lambda rng: "hook"))
""",
        test="""
from tests.unit import plugin_probe


def test_one(gql):
    plugin_probe.EVENTS.append(("names", sorted(gql.scalars)))


def test_two(gql):
    pass
""",
    )
    result.assert_outcomes(passed=2)
    assert recorded("register") == [("register", ["Own"])]
    assert recorded("names")[0][1] == ["FromHook", "Own"]


# -- a scalar registered twice replaces and warns -----------------------------

TWO_PLUGINS = """
import pytest

from pytest_graphql import ScalarSpec


def make(name, label, **register):
    class Plugin:
        def pytest_graphql_register_scalars(self, registry):
            spec = ScalarSpec(
                "DateTime", serialize=lambda v: label, fake=lambda rng: label
            )
            registry.register(spec, **register)

    return Plugin()


def pytest_configure(config):
    config.pluginmanager.register(make("a", "from-a", **REGISTER), "a")
    config.pluginmanager.register(make("b", "from-b", **REGISTER), "b")


REGISTER = {}
"""

WHICH_SPEC = """
from tests.unit import plugin_probe


def test_which(gql):
    spec = gql.scalars.get("DateTime")
    plugin_probe.EVENTS.append(("spec", spec.serialize(None)))
"""


def test_a_scalar_registered_twice_replaces_the_first_and_warns(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, conftest=TWO_PLUGINS, test=WHICH_SPEC)
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*scalar 'DateTime' was registered twice*"])
    # Plugin b runs first and a second, so a's registration is the later one.
    assert recorded("spec")[0][1] == "from-a"


def test_an_explicit_replace_is_not_a_warning(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=TWO_PLUGINS.replace("REGISTER = {}", "REGISTER = {'replace': True}"),
        test=WHICH_SPEC,
    )
    result.assert_outcomes(passed=1, warnings=0)
    assert recorded("spec")[0][1] == "from-a"


def test_a_hook_replaces_a_scalar_the_fixture_already_registered(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest

from pytest_graphql import ScalarRegistry, ScalarSpec


@pytest.fixture(scope="session")
def gql_scalars():
    registry = ScalarRegistry()
    registry.register(
        ScalarSpec("DateTime", serialize=lambda v: "from-fixture", fake=lambda r: 1)
    )
    return registry


def pytest_graphql_register_scalars(registry):
    registry.register(
        ScalarSpec("DateTime", serialize=lambda v: "from-hook", fake=lambda r: 1)
    )
""",
        test=WHICH_SPEC,
    )
    result.assert_outcomes(passed=1, warnings=1)
    assert recorded("spec")[0][1] == "from-hook"


def test_outside_the_hook_a_duplicate_is_still_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The replace-and-warn rule belongs to the hook. A client's own registry
    # keeps the core rule, so one author's mistake is not hidden.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=TWO_PLUGINS.replace("REGISTER = {}", "REGISTER = {'replace': True}"),
        test="""
import pytest

from pytest_graphql import ScalarSpec


def test_refused(gql):
    spec = ScalarSpec("DateTime", serialize=str, fake=lambda rng: 1)
    with pytest.raises(ValueError, match="already registered"):
        gql.scalars.register(spec)
""",
    )
    result.assert_outcomes(passed=1)


# -- pluggy versions ----------------------------------------------------------
#
# ``HookImpl.wrapper`` exists from pluggy 1.2. pytest 7.4 can run on an older
# pluggy, where only ``hookwrapper`` exists, and ``fold`` must not read an
# attribute that is not there. These tests give ``fold`` stand-ins for the hook
# caller, so the two shapes are checked on every row of the CI matrix. The row
# for the oldest supported pytest also runs the real thing, in the sessions above.


class _OldImpl:
    """An implementation as an old pluggy builds it: no ``wrapper`` attribute."""

    def __init__(self, name: str, log: list[str], *, hookwrapper: bool = False) -> None:
        self.plugin_name = name
        self.hookwrapper = hookwrapper
        self.argnames = ("request",)
        self._log = log

    def function(self, request: str) -> str:
        self._log.append(self.plugin_name)
        return request + "+" + self.plugin_name


class _NewImpl(_OldImpl):
    """An implementation as a recent pluggy builds it: both attributes."""

    def __init__(
        self,
        name: str,
        log: list[str],
        *,
        wrapper: bool = False,
        hookwrapper: bool = False,
    ) -> None:
        super().__init__(name, log, hookwrapper=hookwrapper)
        self.wrapper = wrapper


class _Caller:
    name = "pytest_graphql_before_request"

    def __init__(self, implementations: list[object]) -> None:
        self._implementations = implementations

    def get_hookimpls(self) -> list[object]:
        return list(self._implementations)


def test_fold_works_on_a_pluggy_without_the_wrapper_attribute() -> None:
    log: list[str] = []
    caller = _Caller([_OldImpl("a", log), _OldImpl("b", log)])
    # pluggy lists implementations last-called first, and fold walks them reversed.
    assert hookspecs.fold(caller, "request", "r") == "r+b+a"  # type: ignore[arg-type]
    assert log == ["b", "a"]


@pytest.mark.parametrize(
    "wrapper",
    [
        _OldImpl("w", [], hookwrapper=True),
        _NewImpl("w", [], hookwrapper=True),
        _NewImpl("w", [], wrapper=True),
    ],
    ids=["old-hookwrapper", "new-hookwrapper", "new-wrapper"],
)
def test_fold_refuses_every_kind_of_wrapper_on_either_pluggy(wrapper: _OldImpl) -> None:
    caller = _Caller([_OldImpl("plain", []), wrapper])
    with pytest.raises(pytest.UsageError, match="does not support hook wrappers"):
        hookspecs.fold(caller, "request", "r")  # type: ignore[arg-type]


def test_fold_names_the_plugin_that_holds_the_wrapper() -> None:
    caller = _Caller([_NewImpl("the-wrapping-plugin", [], wrapper=True)])
    with pytest.raises(pytest.UsageError, match="the-wrapping-plugin"):
        hookspecs.fold(caller, "request", "r")  # type: ignore[arg-type]


def test_fold_reads_no_pluggy_attribute_it_does_not_need() -> None:
    # An implementation that declares no argument is called without one.
    log: list[str] = []
    bare = _OldImpl("bare", log)
    bare.argnames = ()
    bare.function = lambda: log.append("bare") or None  # type: ignore[method-assign,assignment,return-value]
    assert hookspecs.fold(_Caller([bare]), "request", "r") == "r"  # type: ignore[arg-type]
    assert log == ["bare"]
