"""The failure section text (SPEC 7.5, DESIGN section 3 "Failure report").

The golden test reproduces the SPEC 7.5 block. The rest pin what the SPEC leaves
open: the wording of a skipped-field line for each reason, a call that got no
response, an error whose message has a newline, the numbering when the recorder
dropped older calls, and that a line holding a secret is withheld whole.
"""

from __future__ import annotations

from typing import Any

import pytest

from pytest_graphql._core.diagnostics import (
    WITHHELD_TEXT,
    OmissionRecord,
    RecordedCall,
    RequestInfo,
)
from pytest_graphql.plugin.reporting import (
    log_message,
    render_call,
    render_calls,
    section_title,
)

URL = "http://localhost:8000/graphql"
SECRET = "section-secret-value-0123"

QUERY_DOCUMENT = (
    "query user($id: ID!) { user(id: $id) { id name email settings { id theme } } }"
)
MUTATION_DOCUMENT = (
    "mutation updateUser($id: ID!, $name: String!) "
    "{ updateUser(id: $id, name: $name) { id name } }"
)

#: SPEC 7.5, with the one change that ``curl`` is the single line ``as_curl()``
#: returns.
SPEC_7_5_SECTION = "\n".join(
    [
        "[1] query user  200  143ms",
        "    " + QUERY_DOCUMENT,
        '    variables: {"id": "123"}',
        "    skipped (require arguments): User.orders, User.auditEntries",
        '    data: {"user": {"id": "123", "name": "John", ...}}'
        "   (truncated, 41 fields)",
        "",
        "[2] mutation updateUser  200  201ms   <-- FAILED HERE",
        "    " + MUTATION_DOCUMENT,
        '    variables: {"id": "123", "name": "New"}',
        "    errors:",
        '      - CONFLICT at ["updateUser"]: name already taken',
        "    reproduce:",
        "      CURL",
    ]
)


def info(
    kind: str = "query",
    operation: str | None = "user",
    document: str = QUERY_DOCUMENT,
    variables: dict[str, Any] | None = None,
    omissions: tuple[OmissionRecord, ...] = (),
    **overrides: Any,
) -> RequestInfo:
    values: dict[str, Any] = {
        "operation": operation,
        "kind": kind,
        "document": document,
        "variables": {} if variables is None else variables,
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "url": URL,
        "omissions": omissions,
    }
    values.update(overrides)
    return RequestInfo(**values)


def call_of(request: RequestInfo, **fields: Any) -> RecordedCall:
    settings: dict[str, Any] = {
        "outcome": "ok",
        "status_code": 200,
        "duration_ms": 100.0,
    }
    settings.update(fields)
    return RecordedCall(request=request.redacted(), **settings)


def spec_calls() -> tuple[RecordedCall, RecordedCall, str]:
    first_request = info(
        variables={"id": "123"},
        omissions=(
            OmissionRecord("User", ("orders",), "required-argument"),
            OmissionRecord("User", ("auditEntries",), "required-argument"),
        ),
    )
    second_request = info(
        kind="mutation",
        operation="updateUser",
        document=MUTATION_DOCUMENT,
        variables={"id": "123", "name": "New"},
    )
    curl = second_request.as_curl()
    first = call_of(
        first_request,
        duration_ms=143.2,
        data='{"user": {"id": "123", "name": "John", ...}}',
        data_fields=41,
        data_cut=True,
        curl=first_request.as_curl(),
    )
    second = call_of(
        second_request,
        outcome="errors",
        duration_ms=201.4,
        error_count=1,
        errors=('CONFLICT at ["updateUser"]: name already taken',),
        data="null",
        curl=curl,
    )
    return first, second, curl


def test_the_section_reproduces_spec_7_5() -> None:
    first, second, curl = spec_calls()
    assert render_calls([first, second]) == SPEC_7_5_SECTION.replace("CURL", curl)


def test_the_curl_line_is_the_recorded_as_curl_text() -> None:
    _, second, curl = spec_calls()
    assert "PYTEST_GQL_HEADER_AUTHORIZATION" in curl
    assert SECRET not in render_calls([second])


def test_only_the_last_call_is_marked_and_reproduced() -> None:
    first, second, _ = spec_calls()
    text = render_calls([first, second, first])
    assert text.count("<-- FAILED HERE") == 1
    assert text.count("reproduce:") == 1
    assert text.rstrip().endswith(first.curl)


def test_a_call_with_no_variables_has_no_variables_line() -> None:
    lines = render_call(1, call_of(info()))
    assert not any(line.strip().startswith("variables:") for line in lines)


def test_a_document_is_shown_on_one_line() -> None:
    document = "query user {\n  user(id: 1) {\n    id\n    name\n  }\n}"
    lines = render_call(1, call_of(info(document=document)))
    assert lines[1] == "    query user { user(id: 1) { id name } }"


def test_an_anonymous_operation_is_named_as_such() -> None:
    lines = render_call(1, call_of(info(operation=None, document="{ x }")))
    assert lines[0] == "[1] query <anonymous>  200  100ms"


def test_a_call_with_no_status_shows_a_dash_and_its_failure() -> None:
    request = info()
    call = call_of(
        request,
        outcome="failed",
        status_code=None,
        duration_ms=12.4,
        failure="GraphQLConnectionError: connection refused",
    )
    lines = render_call(3, call)
    assert lines[0] == "[3] query user  -  12ms"
    assert "    failure: GraphQLConnectionError: connection refused" in lines
    assert not any(line.strip().startswith("data:") for line in lines)


def test_a_cut_data_line_states_the_field_count_and_a_whole_one_does_not() -> None:
    whole = call_of(info(), data='{"a": 1}', data_fields=1)
    cut = call_of(info(), data='{"a": 1, ...}', data_fields=9, data_cut=True)
    assert '    data: {"a": 1}' in render_call(1, whole)
    assert '    data: {"a": 1}   (truncated' not in render_call(1, whole)
    assert '    data: {"a": 1, ...}   (truncated, 9 fields)' in render_call(1, cut)


def test_null_data_and_a_call_without_data_show_no_data_line() -> None:
    for data in (None, "null"):
        lines = render_call(1, call_of(info(), data=data))
        assert not any(line.strip().startswith("data:") for line in lines)


def test_one_field_is_counted_in_the_singular() -> None:
    cut = call_of(info(), data="{...}", data_fields=1, data_cut=True)
    assert "(truncated, 1 field)" in "\n".join(render_call(1, cut))


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("required-argument", "require arguments"),
        ("deprecated", "deprecated"),
        ("connection-page-size", "connection with no page-size argument"),
        ("connection-depth", "connection depth cap"),
        ("depth", "depth cap"),
        ("cycle", "cycle"),
        ("should-include", "excluded by the selection policy"),
        ("union-member-cap", "union member cap"),
    ],
)
def test_each_omission_reason_has_a_label(reason: str, label: str) -> None:
    record = OmissionRecord("Team", ("members",), reason)  # type: ignore[arg-type]
    lines = render_call(1, call_of(info(omissions=(record,))))
    assert f"    skipped ({label}): Team.members" in lines


def test_omissions_group_by_reason_in_first_seen_order_and_name_the_field() -> None:
    omissions = (
        OmissionRecord("User", ("orders",), "required-argument"),
        OmissionRecord("User", ("oldName",), "deprecated"),
        OmissionRecord("Settings", ("settings", "theme"), "required-argument"),
    )
    lines = render_call(1, call_of(info(omissions=omissions)))
    assert "    skipped (require arguments): User.orders, Settings.theme" in lines
    assert "    skipped (deprecated): User.oldName" in lines
    assert lines.index(
        "    skipped (require arguments): User.orders, Settings.theme"
    ) < lines.index("    skipped (deprecated): User.oldName")


def test_dropped_omission_records_are_counted_not_hidden() -> None:
    omissions = tuple(OmissionRecord("User", (f"f{i}",), "depth") for i in range(60))
    lines = render_call(1, call_of(info(omissions=omissions)))
    assert "    skipped: 10 more not listed" in lines


def test_errors_past_the_limit_are_counted_not_listed() -> None:
    call = call_of(
        info(),
        outcome="errors",
        error_count=7,
        errors=("(no code): a", "(no code): b", "(no code): c"),
    )
    lines = render_call(1, call)
    assert lines[-4:] == [
        "      - (no code): a",
        "      - (no code): b",
        "      - (no code): c",
        "      - ... 4 more not shown",
    ]


def test_an_error_with_a_newline_continues_under_its_own_entry() -> None:
    call = call_of(
        info(),
        outcome="errors",
        error_count=2,
        errors=('BAD at ["a"]: line one\n- (no code): forged', "(no code): real"),
    )
    lines = render_call(1, call)
    assert '      - BAD at ["a"]: line one' in lines
    assert "        - (no code): forged" in lines
    assert lines.count("      - (no code): forged") == 0


def test_numbering_continues_from_the_calls_the_recorder_dropped() -> None:
    first, second, _ = spec_calls()
    text = render_calls([first, second], total=120)
    assert text.startswith("[119] query user")
    assert "\n[120] mutation updateUser" in text


def test_the_section_title_counts_the_calls() -> None:
    assert section_title(2, 2) == "GraphQL calls (2)"
    assert section_title(50, 120) == "GraphQL calls (last 50 of 120)"


def test_a_line_that_would_show_a_secret_is_withheld_alone() -> None:
    request = info(variables={"id": "123"})
    call = call_of(
        request,
        data=f'{{"echo": "{SECRET}"}}',
        data_fields=1,
        errors=("(no code): fine",),
        error_count=1,
        outcome="errors",
    )
    lines = render_call(1, call)
    text = "\n".join(lines)
    assert SECRET not in text
    assert f"    data: {WITHHELD_TEXT}" in lines
    assert "      - (no code): fine" in lines
    assert '    variables: {"id": "123"}' in lines


def test_a_heading_that_would_show_a_secret_is_withheld() -> None:
    # The secret is the fixed text and the operation name together, so no
    # single field of the snapshot holds it, and only the line does.
    request = info(headers={"X-Api-Key": "query user  200"})
    lines = render_call(1, call_of(request))
    assert lines[0] == f"[1] {WITHHELD_TEXT}"
    assert "query user  200" not in "\n".join(lines[:1])


def test_a_secret_that_spans_two_calls_withholds_the_whole_section() -> None:
    # The secret holds the end of one call's data line and the start of the next
    # call's heading, so only the finished section holds it.
    secret = "}\n\n[2] query"
    first = call_of(
        info(variables={"password": secret}), data='{"a": 1}', data_fields=1
    )
    second = call_of(info())
    text = render_calls([first, second])
    assert secret not in text
    assert text == WITHHELD_TEXT


def test_a_log_message_at_summary_level_is_one_line() -> None:
    first, _, _ = spec_calls()
    message = log_message(first, "summary")
    assert message == (
        f"query user POST {URL} -> ok (status 200, 143.2 ms, 0 error(s))"
    )


def test_a_log_message_at_full_level_adds_the_body_and_no_reproduce_line() -> None:
    first, second, _ = spec_calls()
    full = log_message(second, "full")
    assert full.splitlines()[0].startswith("mutation updateUser POST")
    assert '    variables: {"id": "123", "name": "New"}' in full
    assert '      - CONFLICT at ["updateUser"]: name already taken' in full
    assert "reproduce" not in full
    assert "FAILED HERE" not in full
    assert log_message(first, "full").count("\n") > 0


def test_a_log_message_is_withheld_when_it_would_show_a_secret() -> None:
    request = info()
    call = call_of(request, data=f'"{SECRET}"', data_fields=0)
    assert SECRET not in log_message(call, "full")
