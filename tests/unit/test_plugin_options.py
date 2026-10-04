"""The option table and its parsers, without a pytest session.

``resolve`` reads a ``pytest.Config``, and the parts of it that matter here are
``getini``, ``getoption``, ``inipath``, ``rootpath`` and ``invocation_params``,
so a small stand-in drives it. The sessions that exercise the same code through
real ini files, environments and command lines are in
``test_plugin_precedence.py`` and ``test_plugin_invalid_values.py``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pytest_graphql._core.client import ClientConfig
from pytest_graphql._core.diagnostics import DEFAULT_REDACT_HEADERS
from pytest_graphql.plugin import options
from pytest_graphql.plugin.options import OPTIONS, Option, resolve

#: SPEC 7.2 and 7.3 with the D4 cuts removed, written out so a row added to or
#: dropped from the table is a deliberate change to this test too.
EXPECTED_INI = {
    "gql_url",
    "gql_headers",
    "gql_timeout",
    "gql_retries",
    "gql_verify",
    "gql_max_depth",
    "gql_cycle_policy",
    "gql_per_type_depth_cap",
    "gql_include_deprecated",
    "gql_max_fields",
    "gql_exclude",
    "gql_relay_aware",
    "gql_validate",
    "gql_seed",
    "gql_redact_headers",
    "gql_schema_source",
}
#: The names redacted whatever a project lists, in the order the option yields them.
DEFAULT_NAMES = tuple(sorted(DEFAULT_REDACT_HEADERS))
EXPECTED_FLAGS = {
    "--gql-url",
    "--gql-seed",
    "--gql-no-validate",
    "--gql-max-depth",
    "--gql-timeout",
}


class StandIn:
    """Just enough of ``pytest.Config`` for ``resolve``."""

    def __init__(
        self,
        tmp_path: Path,
        ini: dict[str, Any] | None = None,
        flags: dict[str, Any] | None = None,
    ) -> None:
        self._ini = ini or {}
        self._flags = flags or {}
        self.inipath = None
        self.rootpath = tmp_path
        self.invocation_params = SimpleNamespace(dir=tmp_path)

    def getini(self, name: str) -> Any:
        default: Any = [] if _option(name).kind == "linelist" else ""
        return self._ini.get(name, default)

    def getoption(self, dest: str, default: Any = None) -> Any:
        return self._flags.get(dest, default)


def _option(ini: str) -> Option:
    return next(option for option in OPTIONS if option.ini == ini)


def _resolve(
    tmp_path: Path,
    *,
    ini: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    flags: dict[str, Any] | None = None,
) -> options.Settings:
    return resolve(StandIn(tmp_path, ini, flags), env or {})  # type: ignore[arg-type]


# -- the table ----------------------------------------------------------------


def test_the_table_is_spec_7_2_without_the_cache_options() -> None:
    assert {option.ini for option in OPTIONS} == EXPECTED_INI
    assert not any("cache" in option.ini for option in OPTIONS)


def test_the_flags_are_spec_7_3_without_refresh_schema() -> None:
    flags = {option.flag.name for option in OPTIONS if option.flag is not None}
    assert flags == EXPECTED_FLAGS


def test_every_environment_variable_is_generated_from_its_ini_name() -> None:
    assert _option("gql_max_depth").env == "PYTEST_GQL_MAX_DEPTH"
    assert _option("gql_per_type_depth_cap").env == "PYTEST_GQL_PER_TYPE_DEPTH_CAP"
    for option in OPTIONS:
        assert option.env == "PYTEST_GQL_" + option.ini.removeprefix("gql_").upper()


def test_the_names_are_unique() -> None:
    assert len({option.ini for option in OPTIONS}) == len(OPTIONS)
    assert len({option.env for option in OPTIONS}) == len(OPTIONS)
    dests = [option.flag.dest for option in OPTIONS if option.flag is not None]
    assert len(set(dests)) == len(dests)


def test_no_environment_variable_collides_with_the_curl_placeholder_names() -> None:
    # The rendered curl command names a header variable PYTEST_GQL_HEADER_<NAME>.
    # An option variable must never be one of those.
    for option in OPTIONS:
        assert not option.env.startswith("PYTEST_GQL_HEADER_")


def test_every_option_sets_a_real_client_config_field_or_none() -> None:
    fields = {field.name for field in dataclasses.fields(ClientConfig)}
    for option in OPTIONS:
        assert option.field is None or option.field in fields
    assert [option.ini for option in OPTIONS if option.field is None] == [
        "gql_schema_source"
    ]


def test_a_default_is_whatever_client_config_holds(tmp_path: Path) -> None:
    settings = _resolve(tmp_path)
    assert settings.base_config() == ClientConfig()
    assert settings.where_is("max_depth") == "the built-in default"


# -- parsing ------------------------------------------------------------------

GOOD = [
    ("gql_url", "http://example.test/graphql", "url", "http://example.test/graphql"),
    ("gql_timeout", "2.5", "timeout", 2.5),
    ("gql_timeout", "inf", "timeout", float("inf")),
    ("gql_retries", "0", "retries", 0),
    ("gql_max_depth", " 4 ", "max_depth", 4),
    ("gql_max_fields", "10", "max_fields", 10),
    ("gql_cycle_policy", "id_only", "cycle_policy", "id_only"),
    ("gql_include_deprecated", "YES", "include_deprecated", True),
    ("gql_relay_aware", "off", "relay_aware", False),
    ("gql_validate", "0", "validate", False),
    ("gql_seed", "-7", "seed", -7),
    ("gql_verify", "false", "verify", False),
    ("gql_verify", "True", "verify", True),
    (
        "gql_headers",
        "X-A: 1\nx-b:  two words ",
        "headers",
        {"X-A": "1", "x-b": "two words"},
    ),
    ("gql_headers", "X-A: 1\nX-A: 2", "headers", {"X-A": "2"}),
    ("gql_headers", "X-Empty:", "headers", {"X-Empty": ""}),
    (
        "gql_per_type_depth_cap",
        "User=2\nPost=0",
        "per_type_depth_cap",
        {"User": 2, "Post": 0},
    ),
    ("gql_exclude", "*.secret\nUser.*", "exclude", ("*.secret", "User.*")),
    (
        "gql_redact_headers",
        "x-one, x-two\nx-three",
        "redact_headers",
        (*DEFAULT_NAMES, "x-one", "x-two", "x-three"),
    ),
]


@pytest.mark.parametrize(("ini", "text", "field", "expected"), GOOD)
def test_a_valid_value_parses_from_every_source(
    tmp_path: Path, ini: str, text: str, field: str, expected: object
) -> None:
    option = _option(ini)
    raw: Any = text.splitlines() if option.kind == "linelist" else text
    from_ini = _resolve(tmp_path, ini={ini: raw})
    from_env = _resolve(tmp_path, env={option.env: text})
    assert from_ini.file_values[field] == expected
    assert from_env.file_values[field] == expected


BAD = [
    ("gql_timeout", "soon"),
    ("gql_timeout", "0"),
    ("gql_timeout", "-1"),
    ("gql_timeout", "nan"),
    ("gql_retries", "-1"),
    ("gql_retries", "two"),
    ("gql_max_depth", "0"),
    ("gql_max_depth", "1.5"),
    ("gql_max_fields", "0"),
    ("gql_cycle_policy", "loop"),
    ("gql_include_deprecated", "maybe"),
    ("gql_relay_aware", "2"),
    ("gql_validate", "nope"),
    ("gql_seed", "random"),
    ("gql_seed", "1.0"),
    ("gql_verify", "no-such-bundle.pem"),
    ("gql_headers", "no colon here"),
    ("gql_headers", "bad name: v"),
    ("gql_headers", "X-A: line\x07break"),
    ("gql_per_type_depth_cap", "User"),
    ("gql_per_type_depth_cap", "User=two"),
    ("gql_per_type_depth_cap", "User=-1"),
    ("gql_per_type_depth_cap", "1User=2"),
    ("gql_exclude", "NoDot"),
    ("gql_exclude", "A.b.c"),
    ("gql_redact_headers", "bad name"),
    ("gql_schema_source", "nodots"),
    ("gql_schema_source", "a..b"),
    ("gql_url", "http://exa mple.test"),
]


@pytest.mark.parametrize(("ini", "text"), BAD)
def test_an_invalid_value_names_the_option_and_the_source(
    tmp_path: Path, ini: str, text: str
) -> None:
    option = _option(ini)
    raw: Any = text.splitlines() if option.kind == "linelist" else text
    with pytest.raises(pytest.UsageError) as from_ini:
        _resolve(tmp_path, ini={ini: raw})
    with pytest.raises(pytest.UsageError) as from_env:
        _resolve(tmp_path, env={option.env: text})
    assert ini in str(from_ini.value)
    assert f"the ini option {ini}" in str(from_ini.value)
    assert ini in str(from_env.value)
    assert f"the environment variable {option.env}" in str(from_env.value)


@pytest.mark.parametrize(("ini", "text"), BAD)
def test_a_refusal_never_shows_the_value(tmp_path: Path, ini: str, text: str) -> None:
    option = _option(ini)
    marked = text.splitlines()[0] if option.kind == "linelist" else text
    raw: Any = text.splitlines() if option.kind == "linelist" else text
    with pytest.raises(pytest.UsageError) as refused:
        _resolve(tmp_path, ini={ini: raw})
    # The value, or its first line for a list, is nowhere in the message.
    assert marked.strip() not in str(refused.value)


@pytest.mark.parametrize(
    "secret_line",
    [
        "Authorization Bearer sk-live-0123456789",
        "X-Api-Key sk-live-0123456789",
    ],
)
def test_a_malformed_header_line_does_not_echo_its_credential(
    tmp_path: Path, secret_line: str
) -> None:
    with pytest.raises(pytest.UsageError) as refused:
        _resolve(tmp_path, env={"PYTEST_GQL_HEADERS": secret_line})
    assert "sk-live-0123456789" not in str(refused.value)
    assert "line 1" in str(refused.value)


def test_a_url_with_credentials_is_not_echoed_when_it_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(pytest.UsageError) as refused:
        _resolve(tmp_path, flags={"gql_url": "http://user:pa ss-0123456789@h/"})
    assert "pa ss" not in str(refused.value)
    assert "0123456789" not in str(refused.value)


def test_an_empty_value_is_not_set_in_any_source(tmp_path: Path) -> None:
    settings = _resolve(
        tmp_path,
        ini={"gql_url": "  ", "gql_headers": []},
        env={"PYTEST_GQL_MAX_DEPTH": " ", "PYTEST_GQL_URL": ""},
        flags={"gql_url": "", "gql_max_depth": " "},
    )
    assert settings.file_values == {}
    assert settings.cli_values == {}


def test_the_environment_beats_the_ini_inside_the_file_layer(tmp_path: Path) -> None:
    settings = _resolve(
        tmp_path,
        ini={"gql_max_depth": "4", "gql_retries": "9"},
        env={"PYTEST_GQL_MAX_DEPTH": "5"},
    )
    assert settings.file_values == {"max_depth": 5, "retries": 9}
    assert settings.where_is("max_depth") == (
        "the environment variable PYTEST_GQL_MAX_DEPTH"
    )
    assert settings.where_is("retries") == "the ini option gql_retries"


def test_a_shadowed_source_is_still_checked(tmp_path: Path) -> None:
    # The ini value is invalid and the environment hides it. It is reported
    # anyway, because a broken file is a defect whether or not it wins today.
    with pytest.raises(pytest.UsageError, match="the ini option gql_max_depth"):
        _resolve(
            tmp_path,
            ini={"gql_max_depth": "zero"},
            env={"PYTEST_GQL_MAX_DEPTH": "5"},
        )


def test_the_flags_go_to_cli_values_and_leave_file_values_alone(
    tmp_path: Path,
) -> None:
    settings = _resolve(
        tmp_path,
        env={"PYTEST_GQL_MAX_DEPTH": "5"},
        flags={
            "gql_max_depth": "7",
            "gql_timeout": "9",
            "gql_url": "http://x.test/g",
            "gql_no_validate": True,
            "gql_seed": "12",
        },
    )
    assert settings.file_values == {"max_depth": 5}
    assert settings.cli_values == {
        "max_depth": 7,
        "timeout": 9.0,
        "url": "http://x.test/g",
        "validate": False,
        "seed": 12,
    }
    assert not settings.seed_is_random


def test_seed_random_chooses_a_seed_and_records_that_it_did(tmp_path: Path) -> None:
    settings = _resolve(tmp_path, flags={"gql_seed": "random"})
    seed = settings.cli_values["seed"]
    assert isinstance(seed, int)
    assert 0 <= seed < 2**32
    assert settings.seed_is_random
    assert settings.where_is("seed") == "the command line option --gql-seed"


def test_apply_cli_outranks_the_config_it_is_applied_to(tmp_path: Path) -> None:
    settings = _resolve(tmp_path, flags={"gql_max_depth": "7"})
    fixture = ClientConfig(max_depth=6, retries=1)
    applied = settings.apply_cli(fixture)
    assert (applied.max_depth, applied.retries) == (7, 1)
    assert fixture.max_depth == 6


def test_a_relative_ca_bundle_resolves_against_the_ini_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "ca.pem").write_text("x", encoding="utf-8")
    settings = _resolve(tmp_path, ini={"gql_verify": "ca.pem"})
    assert settings.file_values["verify"] == str(tmp_path / "ca.pem")


def test_the_schema_source_default_label_means_introspection(tmp_path: Path) -> None:
    assert (
        _resolve(
            tmp_path, ini={"gql_schema_source": options.DEFAULT_SCHEMA_SOURCE}
        ).schema_source
        is None
    )
    chosen = _resolve(tmp_path, ini={"gql_schema_source": "pkg.mod.source"})
    assert chosen.schema_source == "pkg.mod.source"
    assert chosen.schema_source_where == "the ini option gql_schema_source"
    assert "schema_source" not in chosen.file_values


def test_log_flags_are_stored_and_checked(tmp_path: Path) -> None:
    settings = _resolve(
        tmp_path,
        flags={
            "gql_log": True,
            "gql_log_level": "full",
            "gql_show_schema_stats": True,
        },
    )
    assert (settings.log, settings.log_level, settings.show_schema_stats) == (
        True,
        "full",
        True,
    )
    assert _resolve(tmp_path).log_level == "summary"
    with pytest.raises(pytest.UsageError, match="--gql-log-level"):
        _resolve(tmp_path, flags={"gql_log_level": "loud"})


def test_settings_repr_shows_names_and_no_values(tmp_path: Path) -> None:
    settings = _resolve(
        tmp_path,
        env={"PYTEST_GQL_HEADERS": "Authorization: Bearer tok-0123456789"},
    )
    assert "tok-0123456789" not in repr(settings)
    assert "headers" in repr(settings)


def test_base_config_copies_so_a_caller_cannot_change_a_source(
    tmp_path: Path,
) -> None:
    settings = _resolve(tmp_path, env={"PYTEST_GQL_HEADERS": "X-A: 1"})
    first = settings.base_config()
    first.headers["X-B"] = "2"  # type: ignore[index]
    assert settings.base_config().headers == {"X-A": "1"}


def test_listed_redact_headers_extend_the_default_names(tmp_path: Path) -> None:
    settings = _resolve(tmp_path, ini={"gql_redact_headers": ["X-Tenant-Token"]})
    names = settings.file_values["redact_headers"]
    assert names == (*DEFAULT_NAMES, "X-Tenant-Token")
    assert settings.where_is("redact_headers") == ("the ini option gql_redact_headers")


def test_a_listed_default_name_is_not_added_twice_whatever_its_case(
    tmp_path: Path,
) -> None:
    settings = _resolve(
        tmp_path,
        env={"PYTEST_GQL_REDACT_HEADERS": "Authorization, X-One\nCOOKIE\nx-one"},
    )
    assert settings.file_values["redact_headers"] == (*DEFAULT_NAMES, "X-One")


def test_the_environment_list_replaces_the_ini_list_and_both_keep_the_defaults(
    tmp_path: Path,
) -> None:
    # One source wins per setting, so the lists are not merged across sources.
    # Each source's list extends the default, so the defaults survive either way.
    settings = _resolve(
        tmp_path,
        ini={"gql_redact_headers": ["x-ini"]},
        env={"PYTEST_GQL_REDACT_HEADERS": "x-env"},
    )
    assert settings.file_values["redact_headers"] == (*DEFAULT_NAMES, "x-env")


def test_a_default_name_cannot_be_removed_through_the_option(tmp_path: Path) -> None:
    # There is no spelling that un-redacts a default. A project that needs a
    # smaller list builds a ClientConfig with exactly the names it wants.
    settings = _resolve(tmp_path, ini={"gql_redact_headers": ["x-one"]})
    assert set(DEFAULT_NAMES) <= set(settings.file_values["redact_headers"])
