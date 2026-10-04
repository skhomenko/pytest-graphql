"""The data excerpt a recorded call keeps for the failure section (DESIGN section 7).

Response data is the one free-form value that reaches a report without a
snapshot to carry it, so the text is built where the live request is still in
hand: path-based redaction first, then the value scrub, then the escape, then
the cut. The cut lands on an entry boundary, states the field count, and never
exceeds ``max_diagnostic_bytes``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.excerpt import DataExcerpt, count_fields, render_data_excerpt

SECRET = "bearer-secret-value-0123"


def request(**overrides: Any) -> RequestInfo:
    values: dict[str, Any] = {
        "operation": None,
        "kind": "query",
        "document": "{ x }",
        "variables": {},
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "url": "http://example.test/graphql",
    }
    values.update(overrides)
    return RequestInfo(**values)


def excerpt(data: Any, limit: int | None = None, **overrides: Any) -> DataExcerpt:
    return render_data_excerpt(request(**overrides), data, limit)


def test_small_data_is_compact_json_with_the_default_separators() -> None:
    result = excerpt({"user": {"id": "123", "name": "John"}})
    assert result == DataExcerpt(
        text='{"user": {"id": "123", "name": "John"}}', fields=3, cut=False
    )


def test_a_scalar_and_a_null_render_as_json() -> None:
    assert excerpt(None).text == "null"
    assert excerpt(5).text == "5"
    assert excerpt([]).text == "[]"
    assert excerpt({}).text == "{}"


def test_fields_count_every_object_member_at_every_depth() -> None:
    data = {"a": [{"b": 1}, {"b": 2, "c": 3}], "d": {"e": None}}
    assert excerpt(data).fields == 6


def test_the_cut_lands_on_an_entry_boundary_and_says_so() -> None:
    data = {"user": {"id": "123", "name": "John", "email": "john@example.test"}}
    result = excerpt(data, 50)
    assert result.text == '{"user": {"id": "123", "name": "John", ...}}'
    assert result.cut
    assert result.fields == 4


def test_a_list_cut_keeps_the_complete_elements() -> None:
    result = excerpt({"ids": list(range(100, 200))}, 40)
    assert result.text.startswith('{"ids": [100, 101, ')
    assert result.text.endswith(", ...]}")
    assert result.cut


def test_a_nested_container_that_holds_no_entry_shows_only_the_marker() -> None:
    result = excerpt({"first": {"a": 1, "b": 2}}, 18)
    assert result.text == '{"first": {...}}'
    assert result.cut


def shapes() -> dict[str, Any]:
    return {
        "records": {
            "users": [
                {"id": str(i), "name": f"name-{i}", "tags": ["a", "b"]}
                for i in range(20)
            ]
        },
        "scalar": 12345,
        "string": "x" * 300,
        "empty": {},
        "nested": {"a": {"b": {"c": {"d": [1, 2, {"e": "f"}]}}}},
        "unicode": {"names": ["\u00e9" * 10, "\u4e2d" * 10, "\U0001f600" * 5]},
        "secret": {"echo": f"Bearer {SECRET} " * 6, "n": 1},
        "escapes": {"v": "a\x1b" * 40},
    }


@pytest.mark.parametrize("name", sorted(shapes()))
def test_the_excerpt_never_exceeds_the_limit_for_any_limit(name: str) -> None:
    data = shapes()[name]
    for limit in range(0, 200):
        result = excerpt(data, limit)
        assert len(result.text.encode("utf-8")) <= limit, (name, limit, result.text)
        assert result.fields == count_fields(data)


def test_a_limit_below_the_cut_marker_shows_what_fits_of_it() -> None:
    assert excerpt({"a": 1}, 0) == DataExcerpt(text="", fields=1, cut=True)
    assert excerpt({"a": 1}, 1).text == "."
    assert excerpt({"a": 1}, 2).text == ".."
    assert excerpt({"a": 1}, 3).text == "..."


def test_a_negative_limit_is_no_room_at_all() -> None:
    assert excerpt({"a": 1}, -5).text == ""


def test_a_value_that_fits_is_not_cut_at_its_exact_size() -> None:
    text = '{"a": 1}'
    assert excerpt({"a": 1}, len(text)) == DataExcerpt(text=text, fields=1, cut=False)


def test_a_cut_text_is_a_balanced_json_prefix() -> None:
    data = {"users": [{"id": str(i), "tags": ["a", "b"]} for i in range(30)]}
    text = excerpt(data, 120).text
    assert text.count("{") == text.count("}")
    assert text.count("[") == text.count("]")
    assert "..." in text


def test_the_default_limit_is_the_request_diagnostic_cap() -> None:
    data = {"items": ["x" * 40 for _ in range(50)]}
    result = excerpt(data, None, max_diagnostic_bytes=200)
    assert len(result.text.encode("utf-8")) <= 200
    assert result.cut


def test_a_long_string_is_shortened_with_a_visible_cut() -> None:
    result = excerpt({"bio": "z" * 500}, 120)
    assert result.text.startswith('{"bio": "zzzz')
    assert "byte(s) cut)" in result.text
    assert len(result.text.encode("utf-8")) <= 120
    assert result.cut


def test_the_limit_counts_utf8_bytes_not_characters() -> None:
    data = {"names": ["é" * 10 for _ in range(10)]}
    result = excerpt(data, 60)
    assert len(result.text.encode("utf-8")) <= 60
    assert "é" in result.text


def test_a_redacted_path_shows_a_marker_and_no_value() -> None:
    data = {"user": {"name": "John", "password": "p4ss-w0rd-value", "token": ["t"]}}
    text = excerpt(data).text
    assert "p4ss-w0rd-value" not in text
    assert '"name": "John"' in text
    assert text.count("[redacted") == 2


def test_a_dotted_pattern_redacts_below_its_tail() -> None:
    data = {"user": {"settings": {"theme": "dark"}, "name": "John"}}
    text = excerpt(data, redact_variables=("user.settings",)).text
    assert "dark" not in text
    assert '"name": "John"' in text


def test_path_patterns_match_the_snake_case_form_of_a_key() -> None:
    text = excerpt({"apiKey": "key-value-0123456"}).text
    assert "key-value-0123456" not in text


def test_a_string_holding_a_request_secret_is_scrubbed() -> None:
    result = excerpt({"echo": f"the header was Bearer {SECRET} ok"})
    assert SECRET not in result.text
    assert "[redacted" in result.text


def test_a_key_holding_a_request_secret_is_scrubbed() -> None:
    assert SECRET not in excerpt({SECRET: 1}).text


def test_a_secret_with_quotes_survives_neither_the_scrub_nor_the_json_escape() -> None:
    secret = "a\"b'c\\d-secret-value"
    result = render_data_excerpt(
        request(headers={"Authorization": f"Bearer {secret}"}),
        {"echo": f"x{secret}y"},
    )
    assert secret not in result.text
    assert json.dumps(secret)[1:-1] not in result.text
    assert repr(secret)[1:-1] not in result.text


def test_control_characters_are_escaped() -> None:
    text = excerpt({"v": "a\x1b[31mred\x07"}).text
    assert "\x1b" not in text
    assert "\x07" not in text


def test_a_lone_surrogate_cannot_break_the_output() -> None:
    text = excerpt({"v": "a\ud800b"}).text
    text.encode("utf-8")


def test_a_hidden_character_is_escaped() -> None:
    text = excerpt({"v": "a" + chr(0x200B) + "b"}).text
    assert chr(0x200B) not in text


def test_scrubbing_can_be_switched_off_but_escaping_cannot() -> None:
    result = excerpt({"echo": f"Bearer {SECRET}", "v": "\x1b"}, redact_values=False)
    assert SECRET in result.text
    assert "\x1b" not in result.text
