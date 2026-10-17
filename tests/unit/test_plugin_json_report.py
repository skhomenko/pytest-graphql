"""The JSON report of ``--gql-report`` (DESIGN section 13.6).

Each failed phase that has a ``GraphQL calls`` section gets one JSON Lines
record. Its values are read from the same checked lines the section is built
from, so these tests pin that a record holds every field, that each value is
text the section shows, and that redaction and withholding hold in the record as
they do in the section. The last test runs real xdist workers and checks that
only the controller writes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pytest_graphql._core.client import ClientConfig
from pytest_graphql._core.diagnostics import WITHHELD_TEXT, OmissionRecord
from pytest_graphql.plugin.reporting import (
    LOGGER_NAME,
    REPORT_FORMAT,
    _render_section,
    report_line,
)
from tests.unit.scripted_transport import run_scripted, sections_of
from tests.unit.test_report_render import SECRET, call_of, info, spec_calls

ROOT = Path(__file__).resolve().parents[2]

RECORD_FIELDS = ["format", "nodeid", "phase", "seed", "run_id", "worker", "calls"]
CALL_FIELDS = [
    "number",
    "kind",
    "operation",
    "status",
    "duration_ms",
    "failed_here",
    "document",
    "variables",
    "skipped",
    "skipped_more",
    "data",
    "errors",
    "errors_not_shown",
    "failure",
    "reproduce",
]

TWO_CALLS = """
def test_fails(gql):
    gql.execute(USER_QUERY, {"id": "123"})
    gql.execute(UPDATE_MUTATION, {"id": "123", "name": "New"}, raise_on_error=False)
    assert False, "boom"
"""


def run_reported(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    test: str,
    *,
    path: str = "report.jsonl",
    **options: Any,
) -> list[dict[str, Any]]:
    """One scripted session with ``--gql-report``, and the records it wrote."""
    run_scripted(pytester, monkeypatch, test, args=[f"--gql-report={path}"], **options)
    return read_records(pytester.path / path)


def read_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    assert text == "" or text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def shown_strings(calls: list[dict[str, Any]]) -> list[str]:
    """Every text a record's calls hold, keys of ``skipped`` included."""
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for key, inner in value.items():
                found.append(key)
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    for call in calls:
        for value in call.values():
            walk(value)
    return found


def assert_only_shown_text(record: dict[str, Any], section: str) -> None:
    for text in shown_strings(record["calls"]):
        # The notice is fixed text, and a record withholds where its own
        # escaping would spell a secret that the section did not show.
        if text == WITHHELD_TEXT:
            continue
        assert text in section, f"{text!r} is not in the section:\n{section}"


# -- the file -------------------------------------------------------------------


def test_a_passing_run_leaves_an_empty_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = run_reported(
        pytester,
        monkeypatch,
        'def test_passes(gql):\n    gql.execute(USER_QUERY, {"id": "1"})\n',
    )
    assert records == []
    assert (pytester.path / "report.jsonl").read_bytes() == b""


def test_the_file_is_created_empty_and_replaces_an_earlier_one(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (pytester.path / "out").mkdir()
    (pytester.path / "out" / "report.jsonl").write_text("stale\n")
    records = run_reported(
        pytester, monkeypatch, "def test_plain():\n    pass\n", path="out/report.jsonl"
    )
    assert records == []


def test_a_relative_path_resolves_against_the_invocation_directory(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = run_reported(
        pytester, monkeypatch, TWO_CALLS, path="nested/dir/report.jsonl"
    )
    assert len(records) == 1


def test_no_flag_writes_no_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, TWO_CALLS)
    assert not list(pytester.path.glob("*.jsonl"))


# -- records ----------------------------------------------------------------------

PHASES = """
import pytest


@pytest.fixture
def cleanup(gql):
    yield
    raise RuntimeError("teardown failed")


def test_fails_twice(cleanup, gql):
    gql.execute(USER_QUERY, {"id": "1"})
    assert False


def test_passes(gql):
    gql.execute(USER_QUERY, {"id": "1"})


def test_fails_with_no_call(gql):
    assert False
"""


def test_one_record_per_failed_phase_with_a_calls_section(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = run_reported(pytester, monkeypatch, PHASES)
    assert [(r["nodeid"], r["phase"]) for r in records] == [
        ("test_inner.py::test_fails_twice", "call"),
        ("test_inner.py::test_fails_twice", "teardown"),
    ]


def test_every_field_is_present_and_holds_what_the_section_shows(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,) = run_reported(pytester, monkeypatch, TWO_CALLS)
    assert list(record) == RECORD_FIELDS
    assert record["format"] == REPORT_FORMAT
    assert record["phase"] == "call"
    assert record["worker"] == "main"
    assert isinstance(record["seed"], int)
    assert isinstance(record["run_id"], str) and record["run_id"]
    first, second = record["calls"]
    assert [list(first), list(second)] == [CALL_FIELDS, CALL_FIELDS]
    section = sections_of("test_fails")["GraphQL calls (2)"]
    lines = section.splitlines()
    assert first["number"] == 1
    assert first["kind"] == "query"
    assert first["operation"] == "user"
    assert first["status"] == 200
    assert lines[0] == f"[1] query user  200  {first['duration_ms']}ms"
    assert first["failed_here"] is False
    assert lines[1] == "    " + first["document"]
    assert lines[2] == "    variables: " + first["variables"]
    assert lines[3] == "    data: " + first["data"]
    assert first["errors"] is None
    assert first["reproduce"] is None
    assert second["failed_here"] is True
    assert second["errors"] == ['CONFLICT at ["updateUser"]: name already taken']
    assert lines[-1] == "      " + second["reproduce"]
    assert second["reproduce"].startswith("curl -sS -X POST")
    for call in (first, second):
        for name in ("skipped", "skipped_more", "errors_not_shown", "failure"):
            assert call[name] is None
    assert_only_shown_text(record, section)


def test_a_call_with_no_response_has_a_null_status_and_its_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,) = run_reported(
        pytester,
        monkeypatch,
        """
import pytest


def test_fails(gql):
    with pytest.raises(ConnectionError):
        gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        routes='{"user": ConnectionError("refused by the scripted server")}',
    )
    (call,) = record["calls"]
    assert call["status"] is None
    assert "refused by the scripted server" in call["failure"]
    assert_only_shown_text(record, sections_of("test_fails")["GraphQL calls (1)"])


def test_a_recorder_that_dropped_calls_keeps_the_true_numbers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,) = run_reported(
        pytester,
        monkeypatch,
        """
def test_fails(gql):
    for _ in range(10):
        gql.execute(USER_QUERY, {"id": "1"})
    assert False
""",
        conftest="""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(gql_config, max_recorded_calls=3)
""",
    )
    assert [call["number"] for call in record["calls"]] == [8, 9, 10]
    assert [call["failed_here"] for call in record["calls"]] == [False, False, True]
    assert [call["reproduce"] is None for call in record["calls"]] == [
        True,
        True,
        False,
    ]
    section = sections_of("test_fails")["GraphQL calls (last 3 of 10)"]
    assert_only_shown_text(record, section)


# -- redaction and withholding ------------------------------------------------------

HEADER_SECRET = "zqhdr-secret-value-0123456789"
VARIABLE_SECRET = "zqvar-secret-value-0123456789"


def test_a_redacted_header_and_a_redacted_variable_stay_redacted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    echo = f"the server said: {HEADER_SECRET} {VARIABLE_SECRET}"
    (record,) = run_reported(
        pytester,
        monkeypatch,
        f"""
def test_fails(gql):
    gql.execute(
        UPDATE_MUTATION,
        {{"id": "1", "name": {VARIABLE_SECRET!r}}},
        headers={{"Authorization": "Bearer {HEADER_SECRET}"}},
        raise_on_error=False,
    )
    assert False
""",
        routes=f"{{'updateUser': envelope(None, ({{'message': {echo!r}}},))}}",
        conftest="""
import dataclasses


@pytest.fixture(scope="session")
def gql_config(gql_config):
    return dataclasses.replace(
        gql_config, redact_variables=(*gql_config.redact_variables, "name")
    )
""",
    )
    text = (pytester.path / "report.jsonl").read_text(encoding="utf-8")
    for secret in (HEADER_SECRET, VARIABLE_SECRET):
        assert secret not in text
        assert secret[:8] not in text
    (call,) = record["calls"]
    assert "[redacted" in call["variables"]
    assert '"${PYTEST_GQL_HEADER_AUTHORIZATION}"' in call["reproduce"]
    assert "the server said" in call["errors"][0]
    assert_only_shown_text(record, sections_of("test_fails")["GraphQL calls (1)"])


def test_a_value_the_section_withholds_is_the_withheld_notice(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first response repeats a header that only the second call sends, so
    # the first call's text passed its own checks and the section withholds the
    # error line once it checks it against every call.
    echo = f"the server said: {HEADER_SECRET}"
    (record,) = run_reported(
        pytester,
        monkeypatch,
        f"""
def test_fails(gql):
    gql.execute(USER_QUERY, {{"id": "1"}}, raise_on_error=False)
    gql.execute(
        UPDATE_MUTATION, {{"id": "1", "name": "n"}},
        headers={{"X-Call-Secret": {HEADER_SECRET!r}}},
    )
    assert False
""",
        routes=(
            f"{{'user': envelope(None, ({{'message': {echo!r}}},)), "
            "'updateUser': envelope({'updateUser': {'id': '1', 'name': 'n'}})}"
        ),
        ini="gql_redact_headers = x-call-secret\n",
    )
    section = sections_of("test_fails")["GraphQL calls (2)"]
    assert f"      - {WITHHELD_TEXT}" in section.splitlines()
    first, _ = record["calls"]
    assert first["errors"] == [WITHHELD_TEXT]
    assert HEADER_SECRET not in (pytester.path / "report.jsonl").read_text()
    assert_only_shown_text(record, section)


# -- the record builder, without a session ----------------------------------------


def built(calls: list[Any], **options: Any) -> dict[str, Any]:
    section = _render_section(calls, **options)
    trace = SimpleNamespace(config=ClientConfig(seed=7))
    line = report_line(
        SimpleNamespace(),  # type: ignore[arg-type]
        "test_x.py::test_x",
        "call",
        trace,  # type: ignore[arg-type]
        section,
    )
    assert line is not None
    assert "\n" not in line
    record: dict[str, Any] = json.loads(line)
    assert_only_shown_text(record, section.text)
    return record


def test_the_spec_7_5_calls_give_the_spec_7_5_values() -> None:
    first, second, curl = spec_calls()
    record = built([first, second])
    assert record["seed"] == 7
    assert record["worker"] == "main"
    assert record["calls"][0] == {
        "number": 1,
        "kind": "query",
        "operation": "user",
        "status": 200,
        "duration_ms": 143,
        "failed_here": False,
        "document": (
            "query user($id: ID!) { user(id: $id) "
            "{ id name email settings { id theme } } }"
        ),
        "variables": '{"id": "123"}',
        "skipped": {"require arguments": ["User.orders", "User.auditEntries"]},
        "skipped_more": None,
        "data": '{"user": {"id": "123", "name": "John", ...}}   (truncated, 41 fields)',
        "errors": None,
        "errors_not_shown": None,
        "failure": None,
        "reproduce": None,
    }
    assert record["calls"][1]["reproduce"] == curl
    assert record["calls"][1]["failed_here"] is True


def test_an_unnamed_operation_is_null_and_counts_are_numbers() -> None:
    # A snapshot keeps 50 omission records and counts the rest.
    omissions = tuple(
        OmissionRecord("User", (f"field{n}",), "deprecated") for n in range(53)
    )
    request = info(operation=None, omissions=omissions)
    call = call_of(
        request,
        status_code=None,
        error_count=4,
        errors=("(no code): one", "(no code): two\nsecond line"),
    )
    (shown,) = built([call])["calls"]
    assert shown["operation"] is None
    assert shown["status"] is None
    assert shown["skipped_more"] == 3
    assert shown["errors_not_shown"] == 2
    # A continuation line keeps the indent the section gives it.
    assert shown["errors"] == ["(no code): one", "(no code): two\n        second line"]


def test_a_withheld_heading_withholds_the_values_it_holds() -> None:
    request = info(headers={"X-Api-Key": "query user  200"})
    (shown,) = built([call_of(request)])["calls"]
    for name in ("kind", "operation", "status", "duration_ms"):
        assert shown[name] == WITHHELD_TEXT
    assert shown["number"] == 1


def test_a_withheld_line_is_the_notice_and_the_rest_stays() -> None:
    call = call_of(
        info(variables={"id": "123"}),
        data=f'{{"echo": "{SECRET}"}}',
        data_fields=1,
    )
    (shown,) = built([call])["calls"]
    assert shown["data"] == WITHHELD_TEXT
    assert shown["variables"] == '{"id": "123"}'


def test_a_withheld_section_withholds_every_value() -> None:
    secret = "}\n\n[2] query"
    first = call_of(
        info(variables={"password": secret}), data='{"a": 1}', data_fields=1
    )
    record = built([first, call_of(info())])
    for shown in record["calls"]:
        for name, value in shown.items():
            if name in ("number", "failed_here") or value is None:
                continue
            assert value == WITHHELD_TEXT, name


def test_a_secret_that_json_escaping_spells_withholds_the_values() -> None:
    # The section shows ``"tail-of-secret`` and the record escapes the quote,
    # so only the record's line holds ``\\"tail-of-secret``.
    secret = '\\"tail-of-secret-0123'
    call = call_of(
        info(headers={"X-Api-Key": secret}),
        data='{"tail-of-secret-0123": 1}',
        data_fields=1,
    )
    section = _render_section([call])
    assert '"tail-of-secret-0123' in section.text
    record = built([call])
    (shown,) = record["calls"]
    assert shown["data"] == WITHHELD_TEXT
    assert shown["number"] == 1


# -- the values that name the test and the run ---------------------------------
#
# No call produced the test id, the run id or the worker id, so no checked line
# vouches for them. JSON escapes a quote or a backslash, so a check of the
# finished line alone would miss a credential that a reader decodes whole.

QUOTED = 'zqmeta"quote-token-0123'
SLASHED = "zqmeta\\slash-token-0123"


def head_record(
    credential: str, field: str, *, seed: int = 7
) -> tuple[str, dict[str, Any]]:
    section = _render_section([call_of(info(headers={"X-Api-Key": credential}))])
    nodeid = "test_x.py::test_x"
    workerinput = {"workerid": "gw0", "testrunuid": "run0"}
    if field == "nodeid":
        nodeid = f"test_x.py::test_x[{credential}]"
    elif field == "worker":
        workerinput["workerid"] = credential
    elif field == "run_id":
        workerinput["testrunuid"] = credential
    line = report_line(
        SimpleNamespace(workerinput=workerinput),  # type: ignore[arg-type]
        nodeid,
        "call",
        SimpleNamespace(config=ClientConfig(seed=seed)),  # type: ignore[arg-type]
        section,
    )
    assert line is not None
    return line, json.loads(line)


def assert_absent(credential: str, line: str, record: dict[str, Any]) -> None:
    """Neither the line nor any decoded value holds the credential."""
    assert credential not in line
    assert json.dumps(credential)[1:-1] not in line
    decoded = [str(value) for value in record.values() if not isinstance(value, list)]
    decoded.extend(shown_strings(record["calls"]))
    assert not [text for text in decoded if credential in text]


@pytest.mark.parametrize("credential", [QUOTED, SLASHED])
@pytest.mark.parametrize("field", ["nodeid", "run_id", "worker"])
def test_a_test_or_run_value_holding_a_credential_is_withheld(
    credential: str, field: str
) -> None:
    line, record = head_record(credential, field)
    assert record[field] == WITHHELD_TEXT
    assert_absent(credential, line, record)
    # Only that value is withheld. The calls show what the section shows.
    assert record["calls"][0]["kind"] == "query"
    assert record["format"] == REPORT_FORMAT


def test_a_seed_that_spells_a_credential_is_withheld() -> None:
    line, record = head_record("123456789012", "", seed=123456789012)
    assert record["seed"] == WITHHELD_TEXT
    assert_absent("123456789012", line, record)


def test_a_parameter_id_holding_a_credential_is_withheld_in_a_real_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,) = run_reported(
        pytester,
        monkeypatch,
        f"""
import pytest


@pytest.mark.parametrize("api_key", [{QUOTED!r}])
def test_fails(gql, api_key):
    gql.execute(USER_QUERY, {{"id": "1"}}, headers={{"X-Api-Key": api_key}})
    assert False, "boom"
""",
    )
    line = (pytester.path / "report.jsonl").read_text(encoding="utf-8").strip()
    assert record["nodeid"] == WITHHELD_TEXT
    assert_absent(QUOTED, line, record)
    assert [call["operation"] for call in record["calls"]] == ["user"]


# -- a file that cannot be written ------------------------------------------------

FAULTY_FILE = """
import errno

from pytest_graphql.plugin import reporting


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    writer = session.config.stash[reporting._WRITER]
    handle = writer._handle

    class FaultyFile:
        def write(self, text):
            if FAULT == "write":
                raise OSError(errno.ENOSPC, "synthetic disk full")
            return handle.write(text)

        def flush(self):
            if FAULT == "flush":
                raise OSError(errno.ENOSPC, "synthetic disk full")
            return handle.flush()

        def close(self):
            handle.close()
            if FAULT == "close":
                raise OSError(errno.ENOSPC, "synthetic disk full")

    writer._handle = FaultyFile()
"""

THREE_TESTS = (
    TWO_CALLS
    + """

def test_fails_again(gql):
    gql.execute(USER_QUERY, {"id": "2"})
    assert False


def test_later():
    pass
"""
)

INCOMPLETE = "--gql-report: the file is incomplete, because writing it raised OSError"


@pytest.mark.parametrize("fault", ["write", "flush", "close"])
def test_a_file_fault_leaves_the_run_and_its_results_alone(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    before = logging.getLogger(LOGGER_NAME).level
    result = run_scripted(
        pytester,
        monkeypatch,
        THREE_TESTS,
        conftest=f"FAULT = {fault!r}\n" + FAULTY_FILE,
        args=["--gql-report=report.jsonl", "--gql-log"],
    )
    assert result.ret == 1
    result.assert_outcomes(failed=2, passed=1)
    output = result.stdout.str()
    assert INCOMPLETE in result.stdout.lines
    assert "synthetic disk full" not in output
    assert "INTERNALERROR" not in output
    # Cleanup after the file still ran: the call logger has its level back.
    assert logging.getLogger(LOGGER_NAME).level == before
    lines = (pytester.path / "report.jsonl").read_text(encoding="utf-8").splitlines()
    # The first fault stops the writer, so a write fault leaves no record and a
    # flush fault leaves the one record it wrote before the flush.
    assert len(lines) == {"write": 0, "flush": 1, "close": 2}[fault]


def test_a_complete_file_adds_no_note(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_scripted(
        pytester, monkeypatch, THREE_TESTS, args=["--gql-report=report.jsonl"]
    )
    assert "incomplete" not in result.stdout.str()


@pytest.mark.parametrize("path", ["taken-dir-zq", "taken\x00nul-zq"])
def test_a_path_that_cannot_be_opened_is_a_usage_error_that_hides_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    (pytester.path / "taken-dir-zq").mkdir()
    result = run_scripted(
        pytester, monkeypatch, TWO_CALLS, args=[f"--gql-report={path}"]
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "cannot write the file --gql-report names" in result.stderr.str()
    assert "-zq" not in result.stderr.str() + result.stdout.str()


# -- xdist ------------------------------------------------------------------------

XDIST_CONFTEST = """
import pytest

from tests.schema.resolvers import build_schema
from tests.unit.scripted_transport import ScriptedTransport, envelope, user_data


@pytest.fixture(scope="session")
def gql_schema():
    return build_schema()


@pytest.fixture(scope="session")
def gql_transport():
    return ScriptedTransport(routes={"user": envelope(user_data())})


@pytest.fixture(scope="session")
def gql_url():
    return "http://127.0.0.1:9/graphql"
"""

XDIST_TESTS = """
import pytest

from tests.unit.scripted_transport import USER_QUERY


@pytest.mark.parametrize("n", range(6))
def test_fails(gql, n):
    gql.execute(USER_QUERY, {"id": str(n)})
    assert False


def test_passes(gql):
    gql.execute(USER_QUERY, {"id": "1"})
"""


def test_under_xdist_only_the_controller_writes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makeconftest(XDIST_CONFTEST)
    pytester.makepyfile(test_inner=XDIST_TESTS)
    result = pytester.runpytest_subprocess(
        "-p", "no:hypothesispytest", "-n", "2", "--gql-report=report.jsonl"
    )
    result.assert_outcomes(failed=6, passed=1)
    records = read_records(pytester.path / "report.jsonl")
    assert sorted(r["nodeid"] for r in records) == sorted(
        f"test_inner.py::test_fails[{n}]" for n in range(6)
    )
    assert {r["worker"] for r in records} <= {"gw0", "gw1"}
    assert len({r["run_id"] for r in records}) == 1
    assert not [
        p.name for p in pytester.path.rglob("*.jsonl") if p.name != "report.jsonl"
    ]
