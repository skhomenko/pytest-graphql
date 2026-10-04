"""``gql_schema_source`` as a dotted path (DESIGN section 2, SPEC 7.2).

The path is an ini option or an environment variable, and it names a
``SchemaSource`` instance. Importing it runs user code, so it happens when a
test first needs the schema, and each of the three ways it can fail has its own
message. The source is an object, not a scalar setting, so the CLI-over-fixture
order does not apply to it. A fixture override replaces whatever the option
named, and between the two options the environment wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.plugin_inner import recorded, run_inner

SDL_FILE = Path(__file__).resolve().parents[1] / "schema" / "sdl.graphql"

SOURCES = f"""
from pathlib import Path

from pytest_graphql._core.schema.source import SDLFileSource

SOURCE = SDLFileSource(Path({str(SDL_FILE)!r}))
"""

CHECK_USER_TYPE = """
from tests.unit import plugin_probe


def test_schema(gql):
    plugin_probe.EVENTS.append(("types", sorted(gql.schema.type_map)))
"""


def _write_sdl(pytester: pytest.Pytester, name: str, type_name: str) -> Path:
    path = pytester.path / f"{name}.graphql"
    path.write_text(
        f"type Query {{ ping: {type_name} }}\ntype {type_name} {{ id: ID }}\n",
        encoding="utf-8",
    )
    return path


def _source_module(pytester: pytest.Pytester, module: str, sdl: Path) -> None:
    pytester.makepyfile(
        **{
            module: f"""
from pathlib import Path

from pytest_graphql._core.schema.source import SDLFileSource

SOURCE = SDLFileSource(Path({str(sdl)!r}))
"""
        }
    )


def test_a_dotted_path_in_the_ini_file_loads_the_schema_from_that_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources=SOURCES)
    # The real transport and no server: loading from SDL sends no request, so
    # a run that passes proves the endpoint was never asked.
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        ini="gql_schema_source = my_sources.SOURCE\n",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    assert "User" in recorded("types")[0][1]


def test_the_environment_variable_names_the_source_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources=SOURCES)
    result = run_inner(
        pytester,
        monkeypatch,
        fake=False,
        env={"PYTEST_GQL_SCHEMA_SOURCE": "my_sources.SOURCE"},
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    assert "User" in recorded("types")[0][1]


def test_the_environment_beats_the_ini_option(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source_module(pytester, "from_ini", _write_sdl(pytester, "ini", "FromIni"))
    _source_module(pytester, "from_env", _write_sdl(pytester, "env", "FromEnv"))
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_schema_source = from_ini.SOURCE\n",
        env={"PYTEST_GQL_SCHEMA_SOURCE": "from_env.SOURCE"},
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    types = recorded("types")[0][1]
    assert "FromEnv" in types
    assert "FromIni" not in types


def test_a_fixture_override_beats_the_ini_option_and_the_environment(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source_module(pytester, "from_ini", _write_sdl(pytester, "ini", "FromIni"))
    _source_module(pytester, "from_env", _write_sdl(pytester, "env", "FromEnv"))
    fixture = _write_sdl(pytester, "fixture", "FromFixture")
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_schema_source = from_ini.SOURCE\n",
        env={"PYTEST_GQL_SCHEMA_SOURCE": "from_env.SOURCE"},
        conftest=f"""
from pathlib import Path

import pytest

from pytest_graphql._core.schema.source import SDLFileSource


@pytest.fixture(scope="session")
def gql_schema_source():
    return SDLFileSource(Path({str(fixture)!r}))
""",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    types = recorded("types")[0][1]
    assert "FromFixture" in types
    assert not {"FromIni", "FromEnv"} & set(types)


def test_a_fixture_override_means_the_ini_path_is_never_imported(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        broken_on_import="raise RuntimeError('imported although overridden')\n"
    )
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_schema_source = broken_on_import.SOURCE\n",
        conftest=f"""
from pathlib import Path

import pytest

from pytest_graphql._core.schema.source import SDLFileSource


@pytest.fixture(scope="session")
def gql_schema_source():
    return SDLFileSource(Path({str(SDL_FILE)!r}))
""",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)


def test_the_default_label_means_introspection_of_the_endpoint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_schema_source = pytest_graphql.IntrospectionSource\n",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    assert "User" in recorded("types")[0][1]


def test_the_hook_receives_the_instance_the_path_names(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources=SOURCES)
    result = run_inner(
        pytester,
        monkeypatch,
        ini="gql_schema_source = my_sources.SOURCE\n",
        conftest="""
from tests.unit import plugin_probe


def pytest_graphql_schema_loaded(schema, source):
    import my_sources

    same = source is my_sources.SOURCE
    plugin_probe.EVENTS.append(("loaded", same, source.fingerprint))
""",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(passed=1)
    ((_, same, fingerprint),) = recorded("loaded")
    assert same is True
    assert fingerprint.startswith("sdl:")


# -- the three refusals, and a failure that is the user's own -----------------


def _run_with_path(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, path: str
) -> str:
    result = run_inner(
        pytester,
        monkeypatch,
        ini=f"gql_schema_source = {path}\n",
        test=CHECK_USER_TYPE,
    )
    result.assert_outcomes(errors=1)
    return result.stdout.str()


def test_a_missing_module_is_a_clear_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _run_with_path(pytester, monkeypatch, "no_such_module_xyz.SOURCE")
    assert "gql_schema_source" in output
    assert "the ini option gql_schema_source" in output
    assert "there is no module named 'no_such_module_xyz'" in output


def test_a_missing_attribute_is_a_clear_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources=SOURCES)
    output = _run_with_path(pytester, monkeypatch, "my_sources.NOT_THERE")
    assert "has no attribute 'NOT_THERE'" in output
    assert "module 'my_sources'" in output


@pytest.mark.parametrize(
    ("definition", "detail"),
    [
        ("SOURCE = 'a string'", "not a SchemaSource"),
        ("SOURCE = 42", "not a SchemaSource"),
        ("SOURCE = object()", "not a SchemaSource"),
        (
            "class SOURCE:\n    fingerprint = 'x'\n    def load(self): ...\n",
            "It is a class. Point at an instance of it.",
        ),
        (
            "class _S:\n    fingerprint = 'x'\nSOURCE = _S()\n",
            "not a SchemaSource",
        ),
        (
            "class _S:\n    def load(self): ...\nSOURCE = _S()\n",
            "not a SchemaSource",
        ),
        (
            "class _S:\n    fingerprint = 3\n    def load(self): ...\nSOURCE = _S()\n",
            "not a SchemaSource",
        ),
    ],
)
def test_an_object_that_is_not_a_schema_source_is_a_clear_error(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    definition: str,
    detail: str,
) -> None:
    pytester.makepyfile(my_sources=definition + "\n")
    output = _run_with_path(pytester, monkeypatch, "my_sources.SOURCE")
    assert "'my_sources.SOURCE'" in output
    assert detail in output


def test_an_exception_raised_by_the_imported_module_is_the_users_own(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources="raise RuntimeError('boom from the user module')\n")
    output = _run_with_path(pytester, monkeypatch, "my_sources.SOURCE")
    assert "RuntimeError: boom from the user module" in output
    assert "there is no module named" not in output


def test_a_missing_dependency_of_the_module_is_not_reported_as_a_missing_module(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(my_sources="import dependency_that_does_not_exist_xyz\n")
    output = _run_with_path(pytester, monkeypatch, "my_sources.SOURCE")
    assert "dependency_that_does_not_exist_xyz" in output
    assert "there is no module named 'my_sources'" not in output


def test_a_source_whose_load_fails_is_reported_and_stops_the_session_fixtures(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
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
    output = _run_with_path(pytester, monkeypatch, "my_sources.SOURCE")
    assert "SchemaError" in output
    assert "the source could not be read" in output


def test_a_malformed_path_is_refused_at_configure_time_with_its_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester, monkeypatch, env={"PYTEST_GQL_SCHEMA_SOURCE": "no-dots-here"}
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    output = result.stdout.str() + result.stderr.str()
    assert "the environment variable PYTEST_GQL_SCHEMA_SOURCE" in output
    assert "dotted path" in output
