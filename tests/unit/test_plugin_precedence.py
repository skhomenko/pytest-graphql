"""Settings precedence (DESIGN section 2), one inner session at a time.

Per setting, from the highest source: per-call argument, CLI flag, fixture,
environment variable, ini option, built-in default. A setting is one of three
classes, and each class gets its own steps:

- a setting with a CLI flag (``max_depth``, ``timeout``, ``validate``, ``seed``,
  ``url``): every step, with the flag above the fixture;
- a setting with no flag (``retries``, ``include_deprecated``, ``cycle_policy``
  and ``exclude``, which are an int, a bool, a choice and a list): every step
  but the flag;
- an object outside the ordering, which is covered where the object is, in
  ``test_plugin_schema_source.py`` and ``test_plugin_identity.py``.

Each setting has one distinct value per layer, so a result names the layer that
won. A step test sets one layer. The ladder sets the layers from the bottom up,
so each step also shows the one below it losing.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pytest_graphql._core.diagnostics import DEFAULT_REDACT_HEADERS
from pytest_graphql.plugin import settings_of  # noqa: F401  (inner sessions import it)
from tests.unit.plugin_inner import (
    recorded,
    recorded_config,
    run_inner,
)

DEFAULT_NAMES = tuple(sorted(DEFAULT_REDACT_HEADERS))


@dataclass(frozen=True)
class Layer:
    """What one precedence layer adds to an inner session, and what it yields."""

    value: Any
    ini: str = ""
    env: dict[str, str] = field(default_factory=dict)
    conftest: str = ""
    args: tuple[str, ...] = ()


def _config_override(assignments: str) -> str:
    """A ``gql_config`` override that starts from the default and changes fields."""
    return f"""
import dataclasses
import pytest


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, {assignments})
"""


@dataclass(frozen=True)
class Setting:
    name: str
    default: Any
    ini: Layer
    env: Layer
    fixture: Layer
    cli: Layer | None = None
    #: The inner session has no ``gql_url`` fixture, because the URL is the
    #: setting under test.
    own_url: bool = False

    def layers(self) -> list[tuple[str, Layer]]:
        found = [("ini", self.ini), ("env", self.env), ("fixture", self.fixture)]
        if self.cli is not None:
            found.append(("cli", self.cli))
        return found


SETTINGS = [
    Setting(
        "max_depth",
        3,
        Layer(4, ini="gql_max_depth = 4\n"),
        Layer(5, env={"PYTEST_GQL_MAX_DEPTH": "5"}),
        Layer(6, conftest=_config_override("max_depth=6")),
        Layer(7, args=("--gql-max-depth=7",)),
    ),
    Setting(
        "timeout",
        30.0,
        Layer(11.0, ini="gql_timeout = 11\n"),
        Layer(12.0, env={"PYTEST_GQL_TIMEOUT": "12"}),
        Layer(13.0, conftest=_config_override("timeout=13.0")),
        Layer(14.0, args=("--gql-timeout=14",)),
    ),
    # A bool has two values, so the layers alternate and the ladder checks the
    # winner against the layer below it, not against the default.
    Setting(
        "validate",
        True,
        Layer(False, ini="gql_validate = false\n"),
        Layer(True, env={"PYTEST_GQL_VALIDATE": "true"}),
        Layer(False, conftest=_config_override("validate=False")),
        Layer(False, args=("--gql-no-validate",)),
    ),
    Setting(
        "seed",
        0,
        Layer(1, ini="gql_seed = 1\n"),
        Layer(2, env={"PYTEST_GQL_SEED": "2"}),
        Layer(
            3,
            conftest="""
import pytest


@pytest.fixture
def gql_seed():
    return 3
""",
        ),
        Layer(4, args=("--gql-seed=4",)),
    ),
    Setting(
        "url",
        None,
        Layer("http://ini.test/graphql", ini="gql_url = http://ini.test/graphql\n"),
        Layer(
            "http://env.test/graphql", env={"PYTEST_GQL_URL": "http://env.test/graphql"}
        ),
        Layer(
            "http://fixture.test/graphql",
            conftest="""
import pytest


@pytest.fixture(scope="session")
def gql_url():
    return "http://fixture.test/graphql"
""",
        ),
        Layer("http://cli.test/graphql", args=("--gql-url=http://cli.test/graphql",)),
        own_url=True,
    ),
    Setting(
        "retries",
        2,
        Layer(3, ini="gql_retries = 3\n"),
        Layer(4, env={"PYTEST_GQL_RETRIES": "4"}),
        Layer(5, conftest=_config_override("retries=5")),
    ),
    Setting(
        "include_deprecated",
        False,
        Layer(True, ini="gql_include_deprecated = true\n"),
        Layer(False, env={"PYTEST_GQL_INCLUDE_DEPRECATED": "false"}),
        Layer(True, conftest=_config_override("include_deprecated=True")),
    ),
    Setting(
        "cycle_policy",
        "shallow",
        Layer("stop", ini="gql_cycle_policy = stop\n"),
        Layer("id_only", env={"PYTEST_GQL_CYCLE_POLICY": "id_only"}),
        Layer("stop", conftest=_config_override("cycle_policy='stop'")),
    ),
    Setting(
        "exclude",
        (),
        Layer(("User.name",), ini="gql_exclude =\n    User.name\n"),
        Layer(("*.id", "Post.*"), env={"PYTEST_GQL_EXCLUDE": "*.id\nPost.*"}),
        Layer(("Team.*",), conftest=_config_override("exclude=('Team.*',)")),
    ),
]
BY_NAME = {setting.name: setting for setting in SETTINGS}
WITH_FLAG = [setting.name for setting in SETTINGS if setting.cli is not None]
WITHOUT_FLAG = [setting.name for setting in SETTINGS if setting.cli is None]
STEPS = ("ini", "env", "fixture", "cli")


def _run(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    setting: Setting,
    layers: list[Layer],
) -> pytest.RunResult:
    return run_inner(
        pytester,
        monkeypatch,
        ini="".join(layer.ini for layer in layers),
        env={key: value for layer in layers for key, value in layer.env.items()},
        conftest="".join(layer.conftest for layer in layers),
        args=tuple(arg for layer in layers for arg in layer.args),
        url=not setting.own_url,
    )


def _effective(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    setting: Setting,
    layers: list[Layer],
) -> Any:
    result = _run(pytester, monkeypatch, setting, layers)
    result.assert_outcomes(passed=1)
    return getattr(recorded_config(), setting.name)


# -- the default --------------------------------------------------------------


@pytest.mark.parametrize("name", [s.name for s in SETTINGS if not s.own_url])
def test_with_no_source_a_setting_has_its_built_in_default(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    setting = BY_NAME[name]
    assert _effective(pytester, monkeypatch, setting, []) == setting.default


def test_with_no_url_anywhere_the_run_names_every_place_to_set_one(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, BY_NAME["url"], [])
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["*no GraphQL endpoint*--gql-url=URL*PYTEST_GQL_URL*gql_url ini option*"]
    )


# -- one step at a time -------------------------------------------------------


def _steps(names: list[str], steps: tuple[str, ...]) -> list[tuple[str, str]]:
    """The (setting, step) pairs whose layer differs from the built-in default.

    A bool has two values. Where a layer equals the default, alone it shows
    nothing, and the ladder is what shows that layer winning.
    """
    pairs = []
    for name in names:
        setting = BY_NAME[name]
        for step, layer in setting.layers():
            if step in steps and layer.value != setting.default:
                pairs.append((name, step))
    return pairs


@pytest.mark.parametrize(("name", "step"), _steps(WITH_FLAG, STEPS))
def test_each_step_alone_beats_the_default_for_a_setting_with_a_flag(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    step: str,
) -> None:
    setting = BY_NAME[name]
    layer = dict(setting.layers())[step]
    assert _effective(pytester, monkeypatch, setting, [layer]) == layer.value


@pytest.mark.parametrize(
    ("name", "step"), _steps(WITHOUT_FLAG, ("ini", "env", "fixture"))
)
def test_each_step_alone_beats_the_default_for_a_setting_without_a_flag(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    step: str,
) -> None:
    setting = BY_NAME[name]
    layer = dict(setting.layers())[step]
    assert _effective(pytester, monkeypatch, setting, [layer]) == layer.value


# -- the ladder: each source loses to the one above it ------------------------


@pytest.mark.parametrize("name", [setting.name for setting in SETTINGS])
def test_each_lower_source_loses_to_the_one_above_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    setting = BY_NAME[name]
    ladder = setting.layers()
    for height in range(2, len(ladder) + 1):
        layers = [layer for _, layer in ladder[:height]]
        winner = ladder[height - 1][1]
        loser = ladder[height - 2][1]
        got = _effective(pytester, monkeypatch, setting, layers)
        assert got == winner.value, (
            f"{name}: {ladder[height - 1][0]} should beat {ladder[height - 2][0]}"
        )
        if winner.value != loser.value:
            assert got != loser.value


@pytest.mark.parametrize("name", WITH_FLAG)
def test_the_flag_wins_over_every_other_source_at_once(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    setting = BY_NAME[name]
    layers = [layer for _, layer in setting.layers()]
    assert _effective(pytester, monkeypatch, setting, layers) == layers[-1].value


def test_the_flag_beats_a_fixture_that_asks_for_the_opposite(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ladder cannot show this for a bool whose fixture and flag agree.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=_config_override("validate=True"),
        args=("--gql-no-validate",),
    )
    result.assert_outcomes(passed=1)
    assert recorded_config().validate is False


@pytest.mark.parametrize("flag", ["--gql-retries=1", "--gql-exclude=A.b"])
def test_a_setting_without_a_flag_has_no_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    result = run_inner(pytester, monkeypatch, args=(flag,))
    assert result.ret == pytest.ExitCode.USAGE_ERROR


# -- a fixture that is a whole configuration ----------------------------------


def test_a_fully_replaced_gql_config_still_yields_to_a_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest
from pytest_graphql import ClientConfig


@pytest.fixture(scope="session")
def gql_config():
    return ClientConfig(max_depth=6, timeout=13.0, retries=8)
""",
        env={"PYTEST_GQL_RETRIES": "4"},
        args=("--gql-max-depth=7",),
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    # The flag beats the fixture. The fixture beats the environment, which the
    # replaced configuration never read.
    assert (config.max_depth, config.timeout, config.retries) == (7, 13.0, 8)


def test_a_gql_config_override_that_asks_for_the_original_keeps_the_sources(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=_config_override("max_depth=6"),
        ini="gql_retries = 3\n",
        env={"PYTEST_GQL_TIMEOUT": "12"},
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    assert (config.max_depth, config.retries, config.timeout) == (6, 3, 12.0)


def test_gql_seed_follows_gql_config_when_only_the_config_is_replaced(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        conftest=_config_override("seed=9"),
        test="""
from tests.unit import plugin_probe


def test_seed(gql, gql_seed):
    plugin_probe.EVENTS.append(("seed", gql.config.seed, gql_seed))
""",
    )
    result.assert_outcomes(passed=1)
    assert recorded("seed")[0][1:] == (9, 9)


# -- per-call arguments sit above the flag ------------------------------------

_PER_CALL = """
import pytest

from pytest_graphql import GraphQLExecutionError
from pytest_graphql._core.errors import SelectionError
from tests.unit import plugin_probe


def test_per_call(gql):
    fake = gql.transport
    gql.query("users")
    from_flag = fake.sent[-1].document
    gql.query("users", max_depth=1)
    shallow = fake.sent[-1].document
    gql.query("users", max_depth=3)
    explicit = fake.sent[-1].document
    plugin_probe.EVENTS.append(("depth", from_flag, shallow, explicit))

    gql.query("pingScalar")
    gql.query("pingScalar", timeout=15)
    plugin_probe.EVENTS.append(("timeout", tuple(fake.timeouts[-2:])))

    skipped = gql.execute("{ nope }", raise_on_error=False)
    plugin_probe.EVENTS.append(("skipped", bool(skipped.errors)))
    with pytest.raises(SelectionError):
        gql.execute("{ nope }", validate=True)
"""


def test_a_per_call_argument_beats_the_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test=_PER_CALL,
        args=("--gql-max-depth=3", "--gql-timeout=14", "--gql-no-validate"),
    )
    result.assert_outcomes(passed=1)

    _, from_flag, shallow, explicit = recorded("depth")[0]
    assert from_flag == explicit
    assert len(shallow) < len(from_flag)
    # Without a per-call value the flag's timeout is the ceiling. A per-call
    # value is the one the client hands the transport. Whether it can loosen a
    # configured phase is the transport's rule, not the plugin's.
    assert recorded("timeout")[0][1] == (14.0, 15.0)
    # With the flag, validation is off: the document reached the server, which
    # answered with errors. A per-call validate=True turned it back on.
    assert recorded("skipped")[0][1] is True


def test_a_per_call_timeout_that_is_tighter_than_the_flag_wins(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test="""
from tests.unit import plugin_probe


def test_tight(gql):
    gql.query("pingScalar", timeout=5)
    plugin_probe.EVENTS.append(("timeout", gql.transport.timeouts[-1]))
""",
        args=("--gql-timeout=14",),
    )
    result.assert_outcomes(passed=1)
    assert recorded("timeout")[0][1] == 5.0


# -- every ini option, every environment variable, every flag -----------------


def test_every_ini_option_reaches_the_client_configuration(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (pytester.path / "ca.pem").write_text("bundle", encoding="utf-8")
    result = run_inner(
        pytester,
        monkeypatch,
        url=False,
        ini="""gql_url = http://ini.test/graphql
gql_headers =
    X-Ini: one
    X-Two: two
gql_timeout = 21.5
gql_retries = 6
gql_verify = ca.pem
gql_max_depth = 8
gql_cycle_policy = id_only
gql_per_type_depth_cap =
    User=2
    Post=1
gql_include_deprecated = true
gql_max_fields = 99
gql_exclude =
    User.name
    *.id
gql_relay_aware = false
gql_validate = false
gql_seed = 31
gql_redact_headers =
    x-ini-secret
""",
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    assert config.url == "http://ini.test/graphql"
    assert dict(config.headers) == {"X-Ini": "one", "X-Two": "two"}
    assert config.timeout == 21.5
    assert config.retries == 6
    assert Path(str(config.verify)).resolve() == (pytester.path / "ca.pem").resolve()
    assert config.max_depth == 8
    assert config.cycle_policy == "id_only"
    assert dict(config.per_type_depth_cap) == {"User": 2, "Post": 1}
    assert config.include_deprecated is True
    assert config.max_fields == 99
    assert tuple(config.exclude) == ("User.name", "*.id")
    assert config.relay_aware is False
    assert config.validate is False
    assert config.seed == 31
    assert tuple(config.redact_headers) == (*DEFAULT_NAMES, "x-ini-secret")


def test_every_environment_variable_reaches_the_client_configuration(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = pytester.path / "env-ca.pem"
    bundle.write_text("bundle", encoding="utf-8")
    result = run_inner(
        pytester,
        monkeypatch,
        url=False,
        env={
            "PYTEST_GQL_URL": "http://env.test/graphql",
            "PYTEST_GQL_HEADERS": "X-Env: one\nX-Two: two",
            "PYTEST_GQL_TIMEOUT": "41.5",
            "PYTEST_GQL_RETRIES": "7",
            "PYTEST_GQL_VERIFY": str(bundle),
            "PYTEST_GQL_MAX_DEPTH": "9",
            "PYTEST_GQL_CYCLE_POLICY": "stop",
            "PYTEST_GQL_PER_TYPE_DEPTH_CAP": "User=3\nPost=2",
            "PYTEST_GQL_INCLUDE_DEPRECATED": "yes",
            "PYTEST_GQL_MAX_FIELDS": "77",
            "PYTEST_GQL_EXCLUDE": "Team.*\n*.secret",
            "PYTEST_GQL_RELAY_AWARE": "no",
            "PYTEST_GQL_VALIDATE": "off",
            "PYTEST_GQL_SEED": "51",
            "PYTEST_GQL_REDACT_HEADERS": "x-env-secret, x-other",
        },
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    assert config.url == "http://env.test/graphql"
    assert dict(config.headers) == {"X-Env": "one", "X-Two": "two"}
    assert config.timeout == 41.5
    assert config.retries == 7
    assert config.verify == str(bundle)
    assert config.max_depth == 9
    assert config.cycle_policy == "stop"
    assert dict(config.per_type_depth_cap) == {"User": 3, "Post": 2}
    assert config.include_deprecated is True
    assert config.max_fields == 77
    assert tuple(config.exclude) == ("Team.*", "*.secret")
    assert config.relay_aware is False
    assert config.validate is False
    assert config.seed == 51
    assert tuple(config.redact_headers) == (*DEFAULT_NAMES, "x-env-secret", "x-other")


def test_every_flag_reaches_the_client_configuration(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        url=False,
        args=(
            "--gql-url=http://cli.test/graphql",
            "--gql-seed=61",
            "--gql-no-validate",
            "--gql-max-depth=2",
            "--gql-timeout=3.5",
        ),
    )
    result.assert_outcomes(passed=1)
    config = recorded_config()
    assert config.url == "http://cli.test/graphql"
    assert config.seed == 61
    assert config.validate is False
    assert config.max_depth == 2
    assert config.timeout == 3.5


def test_the_ini_and_environment_value_of_a_list_replace_one_another(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One setting has one winning source. A list is not merged across sources.
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_exclude =\n    User.name\n    User.email\n",
        env={"PYTEST_GQL_EXCLUDE": "Post.*"},
    )
    result.assert_outcomes(passed=1)
    assert tuple(recorded_config().exclude) == ("Post.*",)


# -- the three settings that exist only as flags ------------------------------


def test_the_log_flags_are_registered_and_stored_and_do_nothing_else(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test="""
from pytest_graphql.plugin import settings_of
from tests.unit import plugin_probe


def test_flags(gql, request):
    s = settings_of(request.config)
    plugin_probe.EVENTS.append(("log", s.log, s.log_level, s.show_schema_stats))
""",
        args=("--gql-log", "--gql-log-level=full", "--gql-show-schema-stats"),
    )
    result.assert_outcomes(passed=1)
    assert recorded("log")[0][1:] == (True, "full", True)


def test_the_log_flags_default_to_off_and_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test="""
from pytest_graphql.plugin import settings_of
from tests.unit import plugin_probe


def test_flags(gql, request):
    s = settings_of(request.config)
    plugin_probe.EVENTS.append(("log", s.log, s.log_level, s.show_schema_stats))
""",
    )
    result.assert_outcomes(passed=1)
    assert recorded("log")[0][1:] == (False, "summary", False)


def test_seed_random_chooses_one_seed_per_session_and_it_can_be_read_back(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = """
from pytest_graphql.plugin import settings_of
from tests.unit import plugin_probe


def test_one(gql, gql_seed, request):
    s = settings_of(request.config)
    chosen = s.cli_values["seed"]
    plugin_probe.EVENTS.append(
        ("seed", gql.config.seed, gql_seed, chosen, s.seed_is_random)
    )


def test_two(gql):
    plugin_probe.EVENTS.append(("seed2", gql.config.seed))
"""
    seeds = []
    for _ in range(2):
        result = run_inner(
            pytester, monkeypatch, test=test, args=("--gql-seed=random",)
        )
        result.assert_outcomes(passed=2)
        used, fixture, chosen, is_random = recorded("seed")[0][1:]
        assert used == fixture == chosen
        assert is_random is True
        assert recorded("seed2")[0][1] == used
        seeds.append(used)
    assert seeds[0] != seeds[1]


def test_a_literal_seed_is_not_random(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test="""
from pytest_graphql.plugin import settings_of
from tests.unit import plugin_probe


def test_one(gql, request):
    plugin_probe.EVENTS.append(("seed", settings_of(request.config).seed_is_random))
""",
        args=("--gql-seed=5",),
    )
    result.assert_outcomes(passed=1)
    assert recorded("seed")[0][1] is False


def test_the_seed_changes_the_data_the_fake_makes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = """
from tests.unit import plugin_probe


def test_data(gql):
    plugin_probe.EVENTS.append(("data", gql.fake.CreatePostInput()))
"""
    found = []
    for flag in ("--gql-seed=1", "--gql-seed=1", "--gql-seed=2"):
        run_inner(pytester, monkeypatch, test=test, args=(flag,)).assert_outcomes(
            passed=1
        )
        found.append(recorded("data")[0][1])
    assert found[0] == found[1]
    assert found[0] != found[2]


def test_a_relative_ca_bundle_in_the_environment_resolves_against_the_cwd(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (pytester.path / "rel.pem").write_text("bundle", encoding="utf-8")
    result = run_inner(pytester, monkeypatch, env={"PYTEST_GQL_VERIFY": "rel.pem"})
    result.assert_outcomes(passed=1)
    verify = recorded_config().verify
    assert Path(str(verify)).resolve() == (pytester.path / "rel.pem").resolve()


def test_the_session_config_is_a_copy_of_the_fixture_value(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A configure hook edits the session's copy. The user's own object, which a
    # fixture may share, is not the one edited.
    result = run_inner(
        pytester,
        monkeypatch,
        conftest="""
import pytest
from pytest_graphql import ClientConfig

OWN = ClientConfig(max_depth=6)


@pytest.fixture(scope="session")
def gql_config():
    return OWN


def pytest_graphql_configure(config):
    config.max_depth = 99
""",
        test="""
import conftest
from tests.unit import plugin_probe


def test_copy(gql):
    plugin_probe.EVENTS.append(("copy", gql.config.max_depth, conftest.OWN.max_depth))
""",
    )
    result.assert_outcomes(passed=1)
    assert recorded("copy")[0][1:] == (99, 6)


def test_dataclass_replace_is_how_the_documented_override_changes_a_field() -> None:
    # The docstring of ``gql_config`` shows ``dataclasses.replace``, so a frozen
    # or slotted ClientConfig would break it. Pin the assumption.
    from pytest_graphql import ClientConfig

    assert dataclasses.replace(ClientConfig(), max_depth=5).max_depth == 5
