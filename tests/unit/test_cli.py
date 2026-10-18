"""The ``pytest-graphql`` command (DESIGN 13.7).

The schema comes from project-authored SDL, written below. The endpoint tests
run against a loopback server built from ``graphql-core`` and the standard
library. A header value and a URL credential are sentinels, and every output
of every path is searched for them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from graphql import build_schema, graphql_sync

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - pytest itself requires tomli on Python 3.10
    import tomli as tomllib

from pytest_graphql._cli import FORMAT, OPERATIONS_FORMAT, main
from pytest_graphql._core.graphql_compat import LINE_3_3
from tests.unit.local_http_server import (
    PlannedResponse,
    closed_port_url,
    fixed,
    local_server,
)

SDL = """
type Query {
  user(id: ID!): User
  users(first: Int = 10, after: String): [User!]!
  ping: Boolean!
  both(id: ID!): User
}

type Mutation {
  createUser(input: CreateUserInput!): User!
  both(id: ID!): User
  rename(fields: String!): User
}

type Subscription {
  userCreated: User!
}

input CreateUserInput {
  name: String!
  age: Int
}

type User {
  id: ID!
  name: String!
}
"""

HEADER_SENTINEL = "hdr-sentinel-5c1e7a"
URL_SENTINEL = "url-sentinel-8b3d2f"


@pytest.fixture
def schema_file(tmp_path: Path) -> Path:
    path = tmp_path / "schema.graphql"
    path.write_text(SDL, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PYTEST_GQL_URL", "PYTEST_GQL_HEADERS"):
        monkeypatch.delenv(name, raising=False)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


@contextmanager
def graphql_server(
    seen: list[dict[str, str]] | None = None,
) -> Iterator[str]:
    """A loopback endpoint that answers introspection from ``SDL``."""
    schema = build_schema(SDL)

    def respond(body: bytes, headers: dict[str, str]) -> PlannedResponse:
        if seen is not None:
            seen.append(headers)
        result = graphql_sync(schema, json.loads(body)["query"])
        return PlannedResponse(
            200,
            (("Content-Type", "application/json"),),
            json.dumps({"data": result.data}).encode(),
        )

    with local_server(respond) as url:
        yield url


def with_credentials(url: str) -> str:
    """The same URL with a password and a query value that are sentinels."""
    parts = urlsplit(url)
    return urlunsplit(
        parts._replace(
            netloc=f"user:{URL_SENTINEL}@{parts.netloc}",
            query=f"key={URL_SENTINEL}",
        )
    )


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        assert HEADER_SENTINEL not in text
        assert URL_SENTINEL not in text


# -- operations ----------------------------------------------------------------


def test_operations_text_lists_every_root(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, err = run(capsys, "operations", "--schema", str(schema_file))
    assert (status, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].split(None, 3) == ["query", "user", "user", "id: ID!"]
    kinds = [line.split()[0] for line in lines]
    assert kinds == sorted(kinds, key=("query", "mutation", "subscription").index)
    assert [line.split()[1] for line in lines if line.startswith("mutation")] == [
        "create_user",
        "both",
        "rename",
    ]
    create = next(line for line in lines if " create_user " in line)
    assert create.split(None, 3)[2:] == ["createUser", "input: CreateUserInput!"]
    ping = next(line for line in lines if " ping " in line)
    assert ping.endswith("  -")
    assert any(line.startswith("subscription") for line in lines)


def test_operations_json(capsys: pytest.CaptureFixture[str], schema_file: Path) -> None:
    status, out, err = run(capsys, "operations", "--json", "--schema", str(schema_file))
    assert (status, err) == (0, "")
    payload = json.loads(out)
    assert payload["format"] == OPERATIONS_FORMAT == "pytest-graphql/operations/1"
    assert FORMAT == "pytest-graphql/describe/1"
    create = next(
        op for op in payload["operations"] if op["schema_name"] == "createUser"
    )
    assert create == {
        "kind": "mutation",
        "name": "create_user",
        "schema_name": "createUser",
        "arguments": [
            {
                "name": "input",
                "schema_name": "input",
                "type": "CreateUserInput!",
                "required": True,
                "default": None,
            }
        ],
    }


# -- describe ------------------------------------------------------------------


def test_describe_text(capsys: pytest.CaptureFixture[str], schema_file: Path) -> None:
    status, out, err = run(
        capsys, "describe", "createUser", "--schema", str(schema_file)
    )
    assert (status, err) == (0, "")
    assert out == (
        "mutation create_user\n"
        "  schema name: createUser\n"
        "Arguments:\n"
        "  input (schema name input): CreateUserInput!, required\n"
        "Returns: User!\n"
        "Document:\n"
        "  mutation createUser($input: CreateUserInput!) {\n"
        "    createUser(input: $input) {\n"
        "      __typename\n"
        "      id\n"
        "      name\n"
        "    }\n"
        "  }\n"
        "Sample call:\n"
        '  gql.mutation("create_user", input=gql.fake.CreateUserInput())\n'
    )


def test_describe_accepts_the_snake_form(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, _ = run(
        capsys, "describe", "create_user", "--schema", str(schema_file)
    )
    assert status == 0
    assert out.startswith("mutation create_user\n")


def test_describe_json(capsys: pytest.CaptureFixture[str], schema_file: Path) -> None:
    status, out, err = run(
        capsys, "describe", "users", "--json", "--schema", str(schema_file)
    )
    assert (status, err) == (0, "")
    payload = json.loads(out)
    assert next(iter(payload)) == "format"
    assert payload["format"] == FORMAT
    assert payload["kind"] == "query"
    assert payload["name"] == "users"
    assert payload["returns"] == "[User!]!"
    assert payload["arguments"][0] == {
        "name": "first",
        "schema_name": "first",
        "type": "Int",
        "required": False,
        "default": "10",
    }
    assert payload["document"].startswith("query users {")
    assert payload["document_error"] is None
    assert payload["sample"] == 'gql.query("users")'


CLIENT_CALLS = (
    ("user", {"id": "u1"}),
    ("users", {}),
    ("ping", {}),
    ("createUser", {"input": {"name": "Ada"}}),
    ("rename", {"variables": {"fields": "x"}}),
    ("userCreated", None),
)


@pytest.mark.parametrize(("name", "kwargs"), CLIENT_CALLS, ids=lambda c: str(c)[:12])
def test_describe_document_is_the_clients_document_apart_from_values(
    capsys: pytest.CaptureFixture[str],
    schema_file: Path,
    name: str,
    kwargs: dict[str, object] | None,
) -> None:
    """Only the required arguments are supplied, so the documents match exactly.

    The client coerces and checks the values it is given. The command supplies
    none, so the two documents may differ in nothing else.
    """
    from graphql import print_ast

    from pytest_graphql import ClientConfig
    from pytest_graphql._core.operation import assemble_operation
    from pytest_graphql._core.selection.builder import SelectionBuilder

    schema = build_schema(SDL)
    kind = (
        "subscription"
        if kwargs is None
        else ("mutation" if name in ("createUser", "rename") else "query")
    )
    policy = ClientConfig().selection_policy()
    sent = assemble_operation(
        schema=schema,
        kind=kind,
        name=name,
        kwargs=kwargs or {},
        policy=policy,
        builder=SelectionBuilder(schema),
    )
    _, out, _ = run(
        capsys, "describe", name, "--kind", kind, "--json", "--schema", str(schema_file)
    )
    assert json.loads(out)["document"] == print_ast(sent.document)


def test_declare_only_is_off_by_default() -> None:
    import inspect

    from pytest_graphql._core.operation import assemble_operation

    parameter = inspect.signature(assemble_operation).parameters["declare_only"]
    assert parameter.default is False
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    import pytest_graphql

    assert "declare_only" not in dir(pytest_graphql)


def test_a_missing_argument_still_raises_in_the_client() -> None:
    """``declare_only`` does not loosen the client's own check."""
    from pytest_graphql import ArgumentError, ClientConfig
    from pytest_graphql._core.operation import assemble_operation
    from pytest_graphql._core.selection.builder import SelectionBuilder

    schema = build_schema(SDL)
    with pytest.raises(ArgumentError):
        assemble_operation(
            schema=schema,
            kind="query",
            name="user",
            kwargs={},
            policy=ClientConfig().selection_policy(),
            builder=SelectionBuilder(schema),
        )


EMPTY_SDL = """
type Query {
  odd: Odd
}

type Odd {
  secret(key: String!, mode: String = "default-sentinel-3e9a"): String
}
"""


def test_document_error_follows_the_message_shape_and_shows_no_value(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "odd.graphql"
    path.write_text(EMPTY_SDL, encoding="utf-8")
    status, out, err = run(capsys, "describe", "odd", "--json", "--schema", str(path))
    assert (status, err) == (0, "")
    payload = json.loads(out)
    assert payload["document"] is None
    error = payload["document_error"]
    lines = error.splitlines()
    assert lines[0].endswith(".")
    assert lines[-1].startswith("  Fix: ")
    assert "default-sentinel-3e9a" not in error
    # The schema's own default is a schema fact. It is listed as the argument's
    # default elsewhere, and a message never repeats it.
    assert "default-sentinel-3e9a" not in out.replace(
        json.dumps(payload["arguments"]), ""
    )

    status, out, err = run(capsys, "describe", "odd", "--schema", str(path))
    assert (status, err) == (0, "")
    assert "  not available: " + lines[0] in out
    assert all(line in out for line in lines[1:])


def test_describe_sample_uses_variables_for_an_argument_named_like_an_option(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    _, out, _ = run(
        capsys, "describe", "rename", "--json", "--schema", str(schema_file)
    )
    assert json.loads(out)["sample"] == (
        'gql.mutation("rename", variables={"fields": ...})'
    )


def test_describe_a_subscription_has_a_document_and_no_sample(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, _ = run(
        capsys, "describe", "userCreated", "--json", "--schema", str(schema_file)
    )
    payload = json.loads(out)
    assert status == 0
    assert payload["kind"] == "subscription"
    assert payload["document"].startswith("subscription userCreated {")
    assert payload["sample"] is None


def test_a_name_on_two_roots_needs_kind(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, err = run(capsys, "describe", "both", "--schema", str(schema_file))
    assert (status, out) == (2, "")
    assert "more than one root: query, mutation" in err
    assert "Fix: pass --kind" in err

    status, out, _ = run(
        capsys, "describe", "both", "--kind", "mutation", "--schema", str(schema_file)
    )
    assert status == 0
    assert out.startswith("mutation both\n")


def test_kind_must_be_a_kind(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, err = run(
        capsys, "describe", "both", "--kind", "bogus", "--schema", str(schema_file)
    )
    assert (status, out) == (2, "")
    assert "must be query, mutation or subscription" in err
    assert "bogus" not in err


# -- unknown name --------------------------------------------------------------


def test_unknown_name_suggests_and_exits_one(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, out, err = run(
        capsys, "describe", "createUsr", "--schema", str(schema_file)
    )
    assert (status, out) == (1, "")
    assert err.splitlines() == [
        "pytest-graphql: no operation named 'createUsr'.",
        "  This schema has 8 operations.",
        "  Did you mean: createUser, create_user?",
        "  Fix: pass a name that pytest-graphql operations lists",
    ]


def test_unknown_name_with_a_kind_counts_that_kind(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    status, _, err = run(
        capsys, "describe", "ping", "--kind", "mutation", "--schema", str(schema_file)
    )
    assert status == 1
    assert "no mutation named 'ping'." in err
    assert "This schema has 3 mutations." in err


# -- the schema source ---------------------------------------------------------


def test_no_source_is_a_usage_error_that_names_the_flag_and_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, err = run(capsys, "operations")
    assert (status, out) == (2, "")
    assert "--schema" in err
    assert "PYTEST_GQL_URL" in err
    assert "Fix:" in err


def test_whitespace_only_url_counts_as_not_set(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_URL", "   ")
    status, _, err = run(capsys, "operations")
    assert status == 2
    assert "PYTEST_GQL_URL" in err


def test_schema_file_outranks_the_endpoint(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    schema_file: Path,
) -> None:
    monkeypatch.setenv("PYTEST_GQL_URL", closed_port_url())
    status, out, _ = run(capsys, "operations", "--schema", str(schema_file))
    assert status == 0
    assert "create_user" in out


def test_endpoint_source_sends_the_headers_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []
    with graphql_server(seen) as url:
        monkeypatch.setenv("PYTEST_GQL_URL", url)
        monkeypatch.setenv(
            "PYTEST_GQL_HEADERS",
            f"Authorization: Bearer {HEADER_SENTINEL}\nX-Env: test",
        )
        status, out, err = run(capsys, "describe", "createUser")
    assert (status, err) == (0, "")
    assert out.startswith("mutation create_user\n")
    assert seen
    assert seen[0]["authorization"] == f"Bearer {HEADER_SENTINEL}"
    assert seen[0]["x-env"] == "test"
    assert_no_secrets(out, err)


def test_endpoint_source_json_holds_no_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with graphql_server() as url:
        monkeypatch.setenv("PYTEST_GQL_URL", with_credentials(url))
        monkeypatch.setenv("PYTEST_GQL_HEADERS", f"X-Api-Key: {HEADER_SENTINEL}")
        for command in (["operations"], ["describe", "ping"]):
            status, out, err = run(capsys, *command, "--json")
            assert (status, err) == (0, "")
            assert json.loads(out)["format"] in (FORMAT, OPERATIONS_FORMAT)
            assert_no_secrets(out, err)


def test_endpoint_is_read_only_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another ``PYTEST_GQL_*`` variable, such as a URL spelling, is ignored."""
    with graphql_server() as url:
        monkeypatch.setenv("PYTEST_GQL_ENDPOINT", url)
        monkeypatch.setenv("PYTEST_GQL_SCHEMA_URL", url)
        status, _, err = run(capsys, "operations")
        assert status == 2
        assert "PYTEST_GQL_URL" in err


def test_other_settings_are_ignored(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    schema_file: Path,
) -> None:
    """Every setting but the URL and the headers is ``ClientConfig``'s default."""
    seen: list[dict[str, str]] = []
    with graphql_server(seen) as url:
        monkeypatch.setenv("PYTEST_GQL_URL", url)
        monkeypatch.setenv("PYTEST_GQL_MAX_DEPTH", "1")
        monkeypatch.setenv("PYTEST_GQL_TIMEOUT", "not a number")
        monkeypatch.setenv("PYTEST_GQL_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("PYTEST_GQL_VERIFY", "/no/such/file")
        status, out, err = run(capsys, "describe", "users", "--json")
        assert (status, err) == (0, "")
        assert seen
    with_default = run(
        capsys, "describe", "users", "--json", "--schema", str(schema_file)
    )
    assert json.loads(out) == json.loads(with_default[1])


def test_headers_are_not_read_from_the_command_line(
    capsys: pytest.CaptureFixture[str], schema_file: Path
) -> None:
    for flag in ("--header", "--headers", "--url", "--endpoint"):
        argv = ["operations", "--schema", str(schema_file), flag, HEADER_SENTINEL]
        status, out, err = run(capsys, *argv)
        assert (status, out) == (2, "")
        assert flag in err
        assert_no_secrets(err)
        # A flag with the value joined to it is named by the flag alone.
        status, _, err = run(capsys, *argv[:-1], f"{flag}={URL_SENTINEL}")
        assert status == 2
        assert_no_secrets(err)


def test_argparse_messages_never_echo_an_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (
        [HEADER_SENTINEL],
        ["describe", "--kind"],
        ["--url", f"http://u:{URL_SENTINEL}@h/"],
        ["describe", "name", "--kind", URL_SENTINEL],
    ):
        status, out, err = run(capsys, *argv)
        assert (status, out) == (2, "")
        assert_no_secrets(err)


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--help")
    assert status == 0
    assert "PYTEST_GQL_URL" in out
    assert "--url" not in out
    assert "--header" not in out


# -- schema load failures ------------------------------------------------------


def test_missing_schema_file_exits_three(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    status, out, err = run(
        capsys, "operations", "--schema", str(tmp_path / "missing.graphql")
    )
    assert (status, out) == (3, "")
    assert "could not read schema file" in err
    assert "Fix:" in err


def test_invalid_sdl_exits_three(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broken = tmp_path / "broken.graphql"
    broken.write_text("type Query { ping: ", encoding="utf-8")
    status, out, err = run(capsys, "operations", "--schema", str(broken))
    assert (status, out) == (3, "")
    assert "is not valid SDL" in err


def test_unreachable_endpoint_exits_three_without_the_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = closed_port_url()
    monkeypatch.setenv("PYTEST_GQL_URL", with_credentials(url))
    monkeypatch.setenv("PYTEST_GQL_HEADERS", f"Authorization: {HEADER_SENTINEL}")
    status, out, err = run(capsys, "operations")
    assert (status, out) == (3, "")
    assert "PYTEST_GQL_URL" in err
    assert "Cause: " in err
    assert_no_secrets(err)
    host = urlsplit(url).netloc
    assert host not in err
    assert str(urlsplit(url).port) not in err


def test_server_error_that_echoes_a_header_exits_three_without_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(
        {"errors": [{"message": f"bad token {HEADER_SENTINEL} at {URL_SENTINEL}"}]}
    ).encode()
    for responder in (
        fixed(200, content_type="application/json", body=body),
        fixed(500, content_type="text/plain", body=body),
        fixed(200, content_type="application/json", body=b"{not json " + body),
    ):
        with local_server(responder) as url:
            monkeypatch.setenv("PYTEST_GQL_URL", with_credentials(url))
            monkeypatch.setenv(
                "PYTEST_GQL_HEADERS", f"Authorization: {HEADER_SENTINEL}"
            )
            status, out, err = run(capsys, "describe", "ping")
        assert (status, out) == (3, "")
        assert_no_secrets(err)
        assert urlsplit(url).netloc not in err


# -- usage errors and headers --------------------------------------------------


def test_invalid_headers_variable_is_refused_without_its_value(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    schema_file: Path,
) -> None:
    monkeypatch.setenv(
        "PYTEST_GQL_HEADERS", f"X-Ok: {HEADER_SENTINEL}\nno separator {HEADER_SENTINEL}"
    )
    status, out, err = run(capsys, "operations", "--schema", str(schema_file))
    assert (status, out) == (2, "")
    assert "invalid value for PYTEST_GQL_HEADERS" in err
    assert "line 2" in err
    assert "The value is not shown." in err
    assert_no_secrets(err)


def test_control_character_in_a_header_value_is_refused_without_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_GQL_URL", closed_port_url())
    monkeypatch.setenv("PYTEST_GQL_HEADERS", f"X-Bad: {HEADER_SENTINEL}\x07")
    status, _, err = run(capsys, "operations")
    assert status == 2
    assert "control characters" in err
    assert_no_secrets(err)


def test_every_error_path_keeps_the_header_sentinel_out(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    schema_file: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PYTEST_GQL_HEADERS", f"Authorization: Bearer {HEADER_SENTINEL}")
    broken = tmp_path / "broken.graphql"
    broken.write_text("type Query {", encoding="utf-8")
    cases = [
        ["operations"],
        ["describe", "nope", "--schema", str(schema_file)],
        ["describe", "both", "--schema", str(schema_file)],
        ["operations", "--schema", str(tmp_path / "missing.graphql")],
        ["operations", "--schema", str(broken)],
        ["operations", "--header", f"X: {HEADER_SENTINEL}"],
    ]
    statuses = []
    for argv in cases:
        status, out, err = run(capsys, *argv)
        statuses.append(status)
        assert_no_secrets(out, err)
    assert statuses == [2, 1, 2, 3, 3, 2]


# -- entry points and purity ---------------------------------------------------


def child_environment() -> dict[str, str]:
    """The parent environment without any ``PYTEST_GQL_*`` setting.

    Windows needs its own variables (``SYSTEMROOT``) to start Python with
    sockets, so the child inherits the rest instead of getting a bare mapping.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_GQL_")}
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    return env


def test_python_dash_m_runs_the_same_command(schema_file: Path) -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest_graphql",
            "operations",
            "--schema",
            str(schema_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_environment(),
    )
    assert done.returncode == 0, done.stderr
    assert "create_user" in done.stdout


def test_the_console_script_points_at_main() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {
        "pytest-graphql": "pytest_graphql._cli:main"
    }
    assert (root / "src" / "pytest_graphql" / "__main__.py").is_file()


def test_the_command_imports_no_pytest() -> None:
    code = (
        "import sys\n"
        "import pytest_graphql.__main__, pytest_graphql._cli\n"
        "assert 'pytest' not in sys.modules, 'pytest was imported'\n"
        "assert '_pytest' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=child_environment(),
    )
    assert done.returncode == 0, done.stderr


# -- a default is the literal the schema declares ------------------------------

DEFAULT_CASES = (
    pytest.param(
        "scalar JSON\ntype Query { f(a: JSON = {limit: 10}): String }",
        "{limit: 10}",
        id="custom scalar object",
    ),
    pytest.param(
        "scalar JSON\ntype Query { f(a: JSON = [1, 2]): String }",
        "[1, 2]",
        id="custom scalar list",
    ),
    pytest.param(
        'scalar JSON\ntype Query { f(a: JSON = {k: [1, {z: null}], s: "x"}): String }',
        '{k: [1, {z: null}], s: "x"}',
        id="custom scalar nested",
    ),
    pytest.param(
        "input Filter { limit: Int = 10 }\ntype Query { f(a: Filter = {}): String }",
        "{}",
        id="input object keeps the written literal",
    ),
    pytest.param(
        "input Filter { limit: Int = 10, tags: [String!] = [] }\n"
        "type Query { f(a: Filter = {limit: 5}): String }",
        "{limit: 5}",
        id="input object with nested defaults",
    ),
    pytest.param(
        "enum Mode { FAST SLOW }\ntype Query { f(a: Mode = SLOW, b: Int = 3): String }",
        "SLOW",
        id="enum",
    ),
)


@pytest.mark.parametrize(("sdl", "expected"), DEFAULT_CASES)
def test_a_default_is_printed_as_declared(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sdl: str, expected: str
) -> None:
    path = tmp_path / "defaults.graphql"
    path.write_text(sdl, encoding="utf-8")
    status, out, err = run(capsys, "operations", "--json", "--schema", str(path))
    assert (status, err) == (0, "")
    operation = json.loads(out)["operations"][0]
    assert operation["arguments"][0]["default"] == expected
    status, out, err = run(capsys, "describe", "f", "--json", "--schema", str(path))
    assert (status, err) == (0, "")
    assert json.loads(out)["arguments"][0]["default"] == expected
    status, out, err = run(capsys, "describe", "f", "--schema", str(path))
    assert (status, err) == (0, "")
    assert f"default {expected}" in out


def test_a_default_read_by_introspection_does_not_crash(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint path reaches the same code with a schema built from JSON."""
    # graphql-core 3.2 cannot introspect a structured custom-scalar default on
    # the server side either, so the test server only holds one on 3.3.
    scalar_default = ", d: JSON = {limit: 10}" if LINE_3_3 else ""
    sdl = (
        "scalar JSON\ninput Filter { limit: Int = 10 }\n"
        f"type Query {{ f(b: Filter = {{}}, c: Int = 3{scalar_default}): String }}"
    )
    schema = build_schema(sdl)

    def respond(body: bytes, _headers: dict[str, str]) -> PlannedResponse:
        result = graphql_sync(schema, json.loads(body)["query"])
        return PlannedResponse(
            200,
            (("Content-Type", "application/json"),),
            json.dumps({"data": result.data}).encode(),
        )

    with local_server(respond) as url:
        monkeypatch.setenv("PYTEST_GQL_URL", url)
        status, out, err = run(capsys, "operations", "--json")
    assert (status, err) == (0, "")
    defaults = [a["default"] for a in json.loads(out)["operations"][0]["arguments"]]
    assert defaults[1] == "3"
    assert all(isinstance(default, str) for default in defaults)
    if LINE_3_3:
        assert defaults == ["{}", "3", "{limit: 10}"]


# -- a sample uses only spellings the client resolves --------------------------


class _Recorder:
    """Stands in for ``gql`` and records the call a sample makes."""

    def __init__(self) -> None:
        self.call: tuple[str, str, dict[str, object]] | None = None
        self.fake = _Fake()

    def query(self, name: str, /, **variables: object) -> None:
        self.call = ("query", name, variables)

    def mutation(self, name: str, /, **variables: object) -> None:
        self.call = ("mutation", name, variables)


class _Fake:
    def __getattr__(self, _name: str) -> object:
        return lambda **_: {}


SAMPLE_SCHEMAS = (
    pytest.param(
        "type Query { user(class: ID!, from: String!): String }",
        "user",
        id="python keywords",
    ),
    pytest.param(
        "type Query { user(userId: ID!, userID: ID!): String }",
        "user",
        id="snake aliases collide",
    ),
    pytest.param(
        "type Query { user(user_id: ID!, userId: ID!, userID: ID!): String }",
        "user",
        id="exact name shadows another snake alias",
    ),
    pytest.param(
        "type Query { userId: String userID: String }",
        "userId",
        id="ambiguous operation alias",
    ),
    pytest.param(
        "type Query { userId: String user_id: String }",
        "userId",
        id="operation alias shadowed by an exact name",
    ),
    pytest.param(
        "type Query { userId(id: ID!): String user_id(id: ID!): String }",
        "user_id",
        id="exact operation name that is also a snake form",
    ),
    pytest.param(
        "type Query { ping: Boolean! }",
        "ping",
        id="no arguments",
    ),
    pytest.param(
        "type Query { find(firstName: String!, lastName: String!): String }",
        "find",
        id="plain snake arguments",
    ),
    pytest.param(
        "type Query { find(fields: String!, raw: Boolean!): String }",
        "find",
        id="arguments named like options",
    ),
    pytest.param(
        "type Query { find(variables: String!, validate: Boolean!): String }",
        "find",
        id="arguments named like the escape hatch",
    ),
    pytest.param(
        "type Query { f(operationName: String!): String }",
        "f",
        id="camel case name of a reserved option",
    ),
    pytest.param(
        "type Query { f(raiseOnError: Boolean!, maxDepth: Int!, id: ID!): String }",
        "f",
        id="several camel case reserved names",
    ),
    pytest.param(
        "type Query { f(cyclePolicy: String!, perTypeDepthCap: Int!, "
        "includeDeprecated: Boolean!, maxFields: Int!, raiseOnPartial: Boolean!): "
        "String }",
        "f",
        id="every multiword reserved option",
    ),
    pytest.param(
        'type Query { f(fields: String! = "a", id: ID!): String }',
        "f",
        id="non-null reserved argument with a default",
    ),
    pytest.param(
        'type Query { f(fields: String! = "a"): String }',
        "f",
        id="only a non-null reserved argument with a default",
    ),
    pytest.param(
        "type Query { f(fields: String, id: ID!): String }",
        "f",
        id="optional reserved argument is left out",
    ),
    pytest.param(
        "type Query { find(None: String!, async: Int!, _x: ID!): String }",
        "find",
        id="more python keywords and leading underscore",
    ),
)


@pytest.mark.parametrize(("sdl", "name"), SAMPLE_SCHEMAS)
def test_a_sample_is_valid_python_and_the_client_accepts_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sdl: str, name: str
) -> None:
    """The sample, with real values in place of ``...``, goes through the client.

    ``assemble_operation`` is the path ``gql.query`` takes before it sends, so
    its name lookup, its reserved-name rule and its argument lookup all apply.
    The document it builds must be the one the command printed.
    """
    from graphql import get_named_type, is_required_argument, print_ast

    from pytest_graphql import ClientConfig
    from pytest_graphql._core.naming import NameMap
    from pytest_graphql._core.operation import assemble_operation, resolve_operation
    from pytest_graphql._core.selection.builder import SelectionBuilder

    path = tmp_path / "sample.graphql"
    path.write_text(sdl, encoding="utf-8")
    status, out, err = run(capsys, "describe", name, "--json", "--schema", str(path))
    assert (status, err) == (0, "")
    record = json.loads(out)
    sample = record["sample"]
    compile(sample, "<sample>", "eval")

    recorder = _Recorder()
    eval(sample, {"gql": recorder})
    assert recorder.call is not None
    kind, written, keywords = recorder.call
    assert kind == record["kind"]

    schema = build_schema(sdl)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resolved, field = resolve_operation(schema, "query", written)
        assert resolved == record["schema_name"]
        by_name = NameMap.build(list(field.args))

        def concrete(key: str) -> object:
            argument = field.args[by_name.get(key) or key]
            type_name = get_named_type(argument.type).name
            return {"Int": 1, "Boolean": True}.get(type_name, "x")

        def fill(mapping: dict[str, object]) -> dict[str, object]:
            return {k: concrete(k) if v is ... else v for k, v in mapping.items()}

        explicit = keywords.get("variables")
        if isinstance(explicit, dict):
            kwargs: dict[str, object] = {"variables": fill(explicit)}
        else:
            kwargs = fill(keywords)
        assembled = assemble_operation(
            schema=schema,
            kind="query",
            name=written,
            kwargs=kwargs,
            policy=ClientConfig().selection_policy(),
            builder=SelectionBuilder(schema),
        )
    assert assembled.field_name == record["schema_name"]
    assert print_ast(assembled.document) == record["document"]
    required = {
        name for name, argument in field.args.items() if is_required_argument(argument)
    }
    declared = {
        v.variable.name.value
        for v in assembled.document.definitions[0].variable_definitions
    }
    # The sample supplies every required argument and nothing else.
    assert declared == required


def test_a_sample_prefers_the_snake_form_when_it_resolves(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "plain.graphql"
    path.write_text(
        "type Query { findUser(firstName: String!): String }", encoding="utf-8"
    )
    _, out, _ = run(capsys, "describe", "findUser", "--json", "--schema", str(path))
    assert json.loads(out)["sample"] == 'gql.query("find_user", first_name=...)'


# -- a default read by introspection keeps its literal on both graphql-core lines -


def _wire_type(
    kind: str, name: str | None = None, of: object = None
) -> dict[str, object]:
    return {"kind": kind, "name": name, "ofType": of}


def _input(
    name: str, type_: dict[str, object], default: str | None
) -> dict[str, object]:
    return {
        "name": name,
        "description": None,
        "type": type_,
        "defaultValue": default,
    }


def _scalar(name: str) -> dict[str, object]:
    return {"kind": "SCALAR", "name": name, "description": None}


INTROSPECTED_DEFAULTS = {
    "__schema": {
        "queryType": {"name": "Query"},
        "mutationType": None,
        "subscriptionType": None,
        "directives": [],
        "types": [
            {
                "kind": "OBJECT",
                "name": "Query",
                "description": None,
                "interfaces": [],
                "fields": [
                    {
                        "name": "f",
                        "description": None,
                        "isDeprecated": False,
                        "deprecationReason": None,
                        "type": _wire_type("SCALAR", "String"),
                        "args": [
                            _input("b", _wire_type("INPUT_OBJECT", "Filter"), "{  }"),
                            _input("d", _wire_type("SCALAR", "JSON"), "{ limit: 10 }"),
                            _input("e", _wire_type("SCALAR", "JSON"), "FAST"),
                            _input("g", _wire_type("SCALAR", "JSON"), "[ 1, 2 ]"),
                            _input("c", _wire_type("SCALAR", "Int"), "3"),
                            _input("n", _wire_type("SCALAR", "Int"), None),
                        ],
                    }
                ],
            },
            {
                "kind": "INPUT_OBJECT",
                "name": "Filter",
                "description": None,
                "inputFields": [
                    _input("limit", _wire_type("SCALAR", "Int"), "10"),
                    _input(
                        "tags",
                        _wire_type("LIST", None, _wire_type("SCALAR", "String")),
                        "[  ]",
                    ),
                ],
            },
            _scalar("String"),
            _scalar("Int"),
            _scalar("JSON"),
        ],
    }
}

INTROSPECTED_EXPECTED = ["{}", "{limit: 10}", "FAST", "[1, 2]", "3", None]


def test_an_introspected_default_keeps_its_literal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed payload, so the result does not depend on a server's graphql-core.

    The wire text of a default is the literal. Both graphql-core lines must print
    it the same way, though 3.2 coerces the text while it builds the schema.
    """

    def respond(_body: bytes, _headers: dict[str, str]) -> PlannedResponse:
        return PlannedResponse(
            200,
            (("Content-Type", "application/json"),),
            json.dumps({"data": INTROSPECTED_DEFAULTS}).encode(),
        )

    with local_server(respond) as url:
        monkeypatch.setenv("PYTEST_GQL_URL", url)
        status, out, err = run(capsys, "operations", "--json")
        assert (status, err) == (0, "")
        arguments = json.loads(out)["operations"][0]["arguments"]
        assert [a["default"] for a in arguments] == INTROSPECTED_EXPECTED
        status, out, err = run(capsys, "describe", "f", "--json")
        assert (status, err) == (0, "")
        arguments = json.loads(out)["arguments"]
        assert [a["default"] for a in arguments] == INTROSPECTED_EXPECTED


def test_an_introspected_input_field_default_keeps_its_literal() -> None:
    from pytest_graphql._core.graphql_compat import default_literal
    from pytest_graphql._core.schema.source import IntrospectionSource

    class Fixed:
        def execute(
            self, _query: str, *, variables: object = None
        ) -> dict[str, object]:
            del variables
            return {"data": INTROSPECTED_DEFAULTS}

    schema = IntrospectionSource(Fixed()).load()
    filter_type = schema.type_map["Filter"]
    assert default_literal(filter_type.fields["limit"]) == "10"  # type: ignore[union-attr]
    assert default_literal(filter_type.fields["tags"]) == "[]"  # type: ignore[union-attr]


def test_keeping_introspected_defaults_never_fails_a_load() -> None:
    from pytest_graphql._core.graphql_compat import keep_introspected_defaults

    schema = build_schema("type Query { a(x: Int = 1): Int }")
    for data in (
        {},
        {"__schema": None},
        {"__schema": {"types": [{}]}},
        {"__schema": {"types": 5}},
    ):
        keep_introspected_defaults(schema, data)
