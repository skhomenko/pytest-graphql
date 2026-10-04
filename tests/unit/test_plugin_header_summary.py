"""The session header, the schema summary and ``--gql-show-schema-stats`` (SPEC 7.5).

The header prints before any test runs, and the schema loads when the first test
needs it, so the header holds what the sources already say: endpoint, seed, run
id. The schema facts are printed in the GraphQL section at the end of the run,
once for each process that loaded a schema. These sessions pin both, and that
every line passes the same scrub as the rest of the output.
"""

from __future__ import annotations

import re

import pytest
from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLUnionType,
    build_schema,
)

from pytest_graphql.plugin.reporting import SchemaFacts, schema_line, stats_lines
from tests.schema.resolvers import build_schema as hostile_schema
from tests.unit import plugin_probe
from tests.unit.scripted_transport import run_scripted

USES_SCHEMA = "def test_uses_it(gql):\n    pass\n"
URL = "http://127.0.0.1:9/graphql"
HEADER = re.compile(
    r"graphql: endpoint (?P<endpoint>.+), seed (?P<seed>\d+)"
    r"(?P<random> \(chosen by random\))?, run id (?P<run>[0-9a-f]{32})"
)


def header_line(result: pytest.RunResult) -> re.Match[str]:
    for line in result.stdout.lines:
        found = HEADER.fullmatch(line)
        if found:
            return found
    raise AssertionError("no GraphQL header line in\n" + result.stdout.str())


def summary(result: pytest.RunResult) -> list[str]:
    lines = result.stdout.lines
    start = next(i for i, line in enumerate(lines) if line.strip("= ") == "GraphQL")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("=") and i > start
        ),
        len(lines),
    )
    return lines[start + 1 : end]


# -- the header -----------------------------------------------------------------


def test_the_header_names_the_endpoint_the_seed_and_the_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester, monkeypatch, USES_SCHEMA, args=(f"--gql-url={URL}",)
    )
    found = header_line(result)
    assert found["endpoint"] == URL
    assert found["seed"] == "0"
    assert found["random"] is None
    result.stdout.fnmatch_lines(
        [
            "graphql: schema loads when the first test needs it, and is listed "
            "at the end of the run"
        ]
    )


def test_a_url_from_the_environment_or_the_ini_file_is_named(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        ini="gql_url = http://ini.example.test/graphql\n",
    )
    assert header_line(result)["endpoint"] == "http://ini.example.test/graphql"
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        ini="gql_url = http://ini.example.test/graphql\n",
        env={"PYTEST_GQL_URL": "http://env.example.test/graphql"},
    )
    assert header_line(result)["endpoint"] == "http://env.example.test/graphql"


def test_an_endpoint_known_only_to_the_fixture_is_said_so(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, USES_SCHEMA)
    assert header_line(result)["endpoint"] == "set by the gql_url fixture"


def test_the_endpoint_shows_no_userinfo_and_no_query(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        args=(
            "--gql-url=http://agent:user-secret-value@example.test/graphql"
            "?token=query-secret-value",
        ),
    )
    text = result.stdout.str()
    assert "user-secret-value" not in text
    assert "query-secret-value" not in text
    assert header_line(result)["endpoint"] == "http://example.test/graphql"


def test_the_seed_shows_as_given_and_random_says_it_was_chosen(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, USES_SCHEMA, args=("--gql-seed=41",))
    assert header_line(result)["seed"] == "41"
    assert header_line(result)["random"] is None
    result = run_scripted(
        pytester,
        monkeypatch,
        """
from tests.unit import plugin_probe


def test_seed(gql):
    plugin_probe.EVENTS.append(("seed", gql.config.seed))
""",
        args=("--gql-seed=random",),
    )
    found = header_line(result)
    assert found["random"] == " (chosen by random)"
    [(_, used)] = [e for e in plugin_probe.EVENTS if e[0] == "seed"]
    assert int(found["seed"]) == used


def test_the_seed_of_the_ini_file_is_the_one_shown(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, USES_SCHEMA, ini="gql_seed = 9\n")
    assert header_line(result)["seed"] == "9"


def test_the_run_id_is_the_one_unique_values_carry(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        """
from tests.unit import plugin_probe


def test_run(gql):
    plugin_probe.EVENTS.append(("run", gql._fake_context.unique_source.run_id))
""",
    )
    [(_, used)] = [e for e in plugin_probe.EVENTS if e[0] == "run"]
    assert header_line(result)["run"] == used


def test_every_session_has_its_own_run_id(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = header_line(run_scripted(pytester, monkeypatch, USES_SCHEMA))["run"]
    second = header_line(run_scripted(pytester, monkeypatch, USES_SCHEMA))["run"]
    assert first != second


def test_a_quiet_run_prints_no_header_but_still_the_schema_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, USES_SCHEMA, args=("-q",))
    assert "graphql: endpoint" not in result.stdout.str()
    assert any(line.startswith("schema main:") for line in summary(result))


def test_a_header_value_in_the_url_or_the_config_is_scrubbed_from_the_header(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "path-secret-value-0123456789"
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        ini=f"gql_headers =\n    X-Api-Key: {secret}\n",
        args=(f"--gql-url=http://example.test/graphql/{secret}",),
    )
    assert secret not in result.stdout.str()


# -- the schema summary ------------------------------------------------------------


def counts_of(schema: object) -> dict[str, int]:
    types = [
        type_
        for name, type_ in schema.type_map.items()  # type: ignore[attr-defined]
        if not name.startswith("__")
    ]
    return {
        "types": len(types),
        "objects": sum(isinstance(t, GraphQLObjectType) for t in types),
        "interfaces": sum(isinstance(t, GraphQLInterfaceType) for t in types),
        "unions": sum(isinstance(t, GraphQLUnionType) for t in types),
        "enums": sum(isinstance(t, GraphQLEnumType) for t in types),
        "inputs": sum(isinstance(t, GraphQLInputObjectType) for t in types),
        "scalars": sum(isinstance(t, GraphQLScalarType) for t in types),
        "fields": sum(
            len(t.fields)
            for t in types
            if isinstance(t, (GraphQLObjectType, GraphQLInterfaceType))
        ),
        "queries": len(schema.query_type.fields),  # type: ignore[attr-defined]
        "mutations": len(schema.mutation_type.fields),  # type: ignore[attr-defined]
    }


def test_the_summary_lists_the_schema_the_session_loaded(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, USES_SCHEMA)
    counts = counts_of(hostile_schema())
    [line] = summary(result)
    match = re.fullmatch(
        r"schema main: introspection:endpoint, (\d+) types, (\d+) queries, "
        r"(\d+) mutations, (\d+) subscriptions, loaded in (\d+\.\d{3})s",
        line,
    )
    assert match is not None, line
    assert [int(match[i]) for i in (1, 2, 3, 4)] == [
        counts["types"],
        counts["queries"],
        counts["mutations"],
        0,
    ]


def test_the_schema_is_loaded_once_for_the_whole_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        "def test_a(gql):\n    pass\n\n\ndef test_b(gql):\n    pass\n",
    )
    assert len([line for line in summary(result) if line.startswith("schema ")]) == 1


def test_a_session_that_never_loaded_a_schema_prints_no_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(pytester, monkeypatch, "def test_plain():\n    pass\n")
    assert "GraphQL" not in "\n".join(
        line for line in result.stdout.lines if line.startswith("=")
    )


def test_an_overridden_schema_fixture_is_listed_and_timed_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        conftest="""
from graphql import build_schema


@pytest.fixture(scope="session")
def gql_schema():
    return build_schema("type Query { ping: Boolean }")
""",
    )
    [line] = summary(result)
    assert re.fullmatch(
        r"schema main: introspection:endpoint, 3 types, 1 query, 0 mutations, "
        r"0 subscriptions, loaded in \d+\.\d{3}s",
        line,
    ), line


def test_the_label_of_a_custom_source_is_the_fingerprint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        conftest="""
from graphql import build_schema


class Source:
    fingerprint = "sdl:schema.graphql"

    def load(self):
        return build_schema("type Query { ping: Boolean }")


@pytest.fixture(scope="session")
def gql_schema_source():
    return Source()


@pytest.fixture(scope="session")
def gql_schema(gql_schema_source):
    return gql_schema_source.load()
""",
    )
    assert summary(result)[0].startswith("schema main: sdl:schema.graphql, ")


def test_a_fingerprint_holding_a_credential_is_scrubbed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "label-secret-value-0123456789"
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        ini=f"gql_headers =\n    X-Api-Key: {secret}\n",
        conftest=f"""
from graphql import build_schema


class Source:
    fingerprint = "custom:{secret}\\x1b[31m"

    def load(self):
        return build_schema("type Query {{ ping: Boolean }}")


@pytest.fixture(scope="session")
def gql_schema_source():
    return Source()


@pytest.fixture(scope="session")
def gql_schema(gql_schema_source):
    return gql_schema_source.load()
""",
        args=("--gql-show-schema-stats",),
    )
    text = result.stdout.str()
    assert secret not in text
    assert "\x1b[31m" not in text
    assert "custom:" in text


# -- --gql-show-schema-stats ---------------------------------------------------------


def test_the_stats_block_appears_only_with_the_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = run_scripted(pytester, monkeypatch, USES_SCHEMA)
    assert "schema stats" not in without.stdout.str()
    with_flag = run_scripted(
        pytester, monkeypatch, USES_SCHEMA, args=("--gql-show-schema-stats",)
    )
    assert "schema stats main:" in with_flag.stdout.str()


def many(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def test_the_stats_block_breaks_the_schema_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester, monkeypatch, USES_SCHEMA, args=("--gql-show-schema-stats",)
    )
    counts = counts_of(hostile_schema())
    lines = summary(result)
    start = lines.index("schema stats main:")
    block = lines[start : start + 6]
    assert block[0] == "schema stats main:"
    assert block[1] == "  fingerprint: introspection:endpoint"
    assert block[2] == (
        f"  types: {counts['types']} ({many(counts['objects'], 'object')}, "
        f"{many(counts['interfaces'], 'interface')}, "
        f"{many(counts['unions'], 'union')}, {many(counts['enums'], 'enum')}, "
        f"{many(counts['inputs'], 'input object')}, "
        f"{many(counts['scalars'], 'scalar')})"
    )
    assert block[3] == f"  fields: {counts['fields']}"
    assert block[4] == (
        f"  operations: {counts['queries']} queries, "
        f"{counts['mutations']} mutations, 0 subscriptions"
    )
    assert re.fullmatch(r"  load time: \d+\.\d{3}s", block[5])


# -- the facts themselves ----------------------------------------------------------


def test_schema_facts_count_types_and_operations_and_skip_introspection_types() -> None:
    schema = build_schema(
        """
        interface Named { name: String }
        type A implements Named { name: String id: ID }
        union U = A
        enum E { X }
        input I { a: Int }
        scalar S
        type Query { a: A }
        type Mutation { m: Int n: Int }
        """
    )
    facts = SchemaFacts.of(schema, "label", None)
    assert (facts.objects, facts.interfaces, facts.unions) == (3, 1, 1)
    assert (facts.enums, facts.inputs) == (1, 1)
    assert facts.scalars == 5  # S, and String, ID, Int and Boolean, which it uses
    assert facts.fields == 2 + 1 + 2 + 1  # Named, A, Query, Mutation
    assert (facts.queries, facts.mutations, facts.subscriptions) == (1, 2, 0)
    assert facts.types == 3 + 1 + 1 + 1 + 1 + facts.scalars


def test_schema_facts_round_trip_through_a_worker_report() -> None:
    facts = SchemaFacts.of(build_schema("type Query { a: Int }"), "label", 0.25)
    assert SchemaFacts.from_dict(facts.as_dict()) == facts


@pytest.mark.parametrize(
    "bad",
    [None, "text", {}, {"fingerprint": "x"}, {"fingerprint": "x", "types": "3"}],
)
def test_malformed_facts_from_a_worker_are_dropped(bad: object) -> None:
    assert SchemaFacts.from_dict(bad) is None


def test_a_boolean_is_not_a_count() -> None:
    facts = SchemaFacts.of(build_schema("type Query { a: Int }"), "label", None)
    data = facts.as_dict()
    data["types"] = True
    assert SchemaFacts.from_dict(data) is None


def test_an_unmeasured_load_says_so() -> None:
    facts = SchemaFacts.of(build_schema("type Query { a: Int }"), "label", None)
    assert schema_line("main", facts).endswith("load time not measured")
    assert "  load time: not measured" in stats_lines("main", facts)


def test_counts_are_worded_in_the_singular_at_one() -> None:
    facts = SchemaFacts.of(build_schema("type Query { a: Int }"), "label", 0.5)
    assert schema_line("main", facts) == (
        "schema main: label, 4 types, 1 query, 0 mutations, 0 subscriptions, "
        "loaded in 0.500s"
    )


# -- every credential the configuration holds -----------------------------------------
#
# A configuration carries four fields that hold a credential: ``headers``,
# ``schema_headers``, ``cookies`` and ``proxy``. Text that no call produced is
# scrubbed with all four, not only with the headers.

SCHEMA_SECRET = "schema-only-secret-value-0123456789"
COOKIE_SECRET = "config-cookie-secret-value-0123456789"
PROXY_SECRET = "proxy-password-secret-0123456789"

CREDENTIAL_FIELDS = {
    "schema_headers": f'schema_headers={{"Authorization": "Bearer {SCHEMA_SECRET}"}}',
    "cookies": f'cookies={{"sid": "{COOKIE_SECRET}"}}',
    "proxy": f'proxy="http://agent:{PROXY_SECRET}@proxy.example.test:3128"',
}
CREDENTIALS = {
    "schema_headers": SCHEMA_SECRET,
    "cookies": COOKIE_SECRET,
    "proxy": PROXY_SECRET,
}


def label_conftest(field: str, limit: int | None = None) -> str:
    cap = "" if limit is None else f", max_diagnostic_bytes={limit}"
    return f"""
import dataclasses
from graphql import build_schema


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, {CREDENTIAL_FIELDS[field]}{cap})


class Source:
    fingerprint = "custom:{CREDENTIALS[field]}"

    def load(self):
        return build_schema("type Query {{ ping: Boolean }}")


@pytest.fixture(scope="session")
def gql_schema_source():
    return Source()


@pytest.fixture(scope="session")
def gql_schema(gql_schema_source):
    return gql_schema_source.load()
"""


@pytest.mark.parametrize("field", sorted(CREDENTIALS))
def test_a_credential_held_by_any_field_is_scrubbed_from_the_schema_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        conftest=label_conftest(field),
        args=("--gql-show-schema-stats",),
    )
    text = result.stdout.str()
    assert CREDENTIALS[field] not in text, field
    assert "schema main: custom:" in text


@pytest.mark.parametrize("field", sorted(CREDENTIALS))
@pytest.mark.parametrize("extra", [3, 12, 20, 33])
def test_a_cut_through_a_credential_shows_no_part_of_it_in_the_schema_summary(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    extra: int,
) -> None:
    # The label is "custom:" and then the credential. A cap that ends inside the
    # credential must not leave its start in the output, whichever field holds it.
    result = run_scripted(
        pytester,
        monkeypatch,
        USES_SCHEMA,
        conftest=label_conftest(field, len("custom:") + extra),
        args=("--gql-show-schema-stats",),
    )
    text = result.stdout.str()
    secret = CREDENTIALS[field]
    assert secret[:8] not in text, (field, extra)
    assert "schema main: custom:" in text
