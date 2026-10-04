"""The one-line error summaries a response builds for failure reports (M8).

A response keeps only a digest-only snapshot of its request, so it cannot
scrub server text after it exists. ``build_response`` still holds the live
request, so each error's summary is built there: scrubbed, escaped and cut
once, and kept as the only form of that text a failure report may show.
"""

from __future__ import annotations

import pytest
from graphql import GraphQLSchema

from tests.unit.scripted_steps import (
    build_test_schema,
    envelope,
    failure,
    make_client,
    rejected,
)


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_test_schema()


def _summaries(schema: GraphQLSchema, *errors: dict, **config: object) -> list:
    client, _ = make_client(schema, envelope({"user": {"id": "u1"}}, errors), **config)
    response = client.query(
        "user", id="u1", fields=["id"], raw=True, raise_on_error=False
    )
    return [info._summary for info in response.errors]


def test_a_summary_is_code_path_and_message(schema: GraphQLSchema) -> None:
    assert _summaries(
        schema, failure("taken", code="CONFLICT", path=["updateUser", 0, "name"])
    ) == ['CONFLICT at ["updateUser", 0, "name"]: taken']


def test_a_summary_without_a_code_or_a_path(schema: GraphQLSchema) -> None:
    assert _summaries(schema, failure("plain")) == ["(no code): plain"]
    assert _summaries(schema, failure("p", path=["a"])) == ['(no code) at ["a"]: p']


def test_a_non_string_code_is_no_code(schema: GraphQLSchema) -> None:
    error = {"message": "m", "extensions": {"code": 5}}

    assert _summaries(schema, error) == ["(no code): m"]


def test_control_characters_are_escaped(schema: GraphQLSchema) -> None:
    (summary,) = _summaries(schema, failure("a\x1b[31mred" + chr(0x202E) + "txt"))

    assert summary == "(no code): a\\x1b[31mred\\u202etxt"


def test_a_long_message_is_cut_visibly(schema: GraphQLSchema) -> None:
    (summary,) = _summaries(schema, failure("x" * 500), max_diagnostic_bytes=100)

    assert summary.startswith("(no code): " + "x" * 100)
    assert summary.endswith("... (truncated, 400 byte(s) cut)")


def test_only_the_first_max_recorded_errors_get_a_summary(
    schema: GraphQLSchema,
) -> None:
    summaries = _summaries(
        schema, *(failure(f"e{n}") for n in range(5)), max_recorded_errors=2
    )

    assert summaries == ["(no code): e0", "(no code): e1", None, None, None]


def test_a_credential_in_a_message_is_replaced_by_its_marker(
    schema: GraphQLSchema,
) -> None:
    secret = "tok-0123456789abcdef"
    client, _ = make_client(
        schema,
        rejected(failure(f"bad token {secret}", path=["x", f"y{secret}"])),
        headers={"Authorization": f"Bearer {secret}"},
    )

    response = client.query(
        "user", id="u1", fields=["id"], raw=True, raise_on_error=False
    )

    (summary,) = (info._summary for info in response.errors)
    assert summary is not None
    assert secret not in summary
    assert "[redacted:Authorization]" in summary


def test_a_secret_split_by_a_zero_width_character_is_still_replaced(
    schema: GraphQLSchema,
) -> None:
    secret = "tok-0123456789abcdef"
    split = secret[:6] + chr(0x200B) + secret[6:]
    client, _ = make_client(
        schema,
        rejected(failure(f"bad {split}")),
        headers={"X-API-Key": secret},
    )

    response = client.query(
        "user", id="u1", fields=["id"], raw=True, raise_on_error=False
    )

    (summary,) = (info._summary for info in response.errors)
    assert summary is not None
    assert secret not in summary
    assert "\\u200b" not in summary


def test_one_response_computes_its_secret_set_a_fixed_number_of_times(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The secret set is the expensive part. Twenty errors with a summary each
    # must not compute it once per piece of text: the count is the same for
    # two errors as for twenty. It is a few per call: the snapshot, the error
    # texts, the data excerpt and the recorded curl command.
    from pytest_graphql._core.diagnostics import RequestInfo

    calls: list[int] = []
    original = RequestInfo._redaction_context

    def counted(self: RequestInfo) -> object:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(RequestInfo, "_redaction_context", counted)

    def count_for(number: int) -> int:
        calls.clear()
        errors = tuple(failure(f"e{n}", code="C", path=["a", n]) for n in range(number))
        client, _ = make_client(
            schema,
            envelope({"user": {"id": "u1"}}, errors),
            headers={"Authorization": "Bearer abcdefghijklmnop"},
        )
        client.query("user", id="u1", fields=["id"], raw=True, raise_on_error=False)
        return len(calls)

    assert count_for(20) == count_for(2)
    assert count_for(20) <= 5
