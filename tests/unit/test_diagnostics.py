"""Diagnostics foundation: ``RequestInfo``, redaction, escaping, ``as_curl()``.

Everything here is built from a constructed ``RequestInfo`` and its own
redaction settings. Per the milestone's stopping rule, no test constructs a
``GraphQLClient``, a transport or a live request, because none exist yet.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import pytest_graphql
from pytest_graphql._core.diagnostics import (
    DEFAULT_MAX_DIAGNOSTIC_BYTES,
    DEFAULT_MAX_RECORDED_ERRORS,
    DEFAULT_MIN_REDACTED_VALUE_LENGTH,
    DEFAULT_REDACT_HEADERS,
    DEFAULT_REDACT_VARIABLES,
    DiagnosticSnapshot,
    RequestInfo,
    _curl_header_variable,
    escape_control_characters,
    scrub_text,
)
from pytest_graphql._core.errors import DiagnosticRenderError

# RFC 9110 tchar: DIGIT / ALPHA / one of "!#$%&'*+-.^_`|~".
_TCHAR_SPECIALS = "!#$%&'*+-.^_`|~"
#: The complete DIGIT and ALPHA parts of tchar; the specials above cover the rest.
_TCHAR_ALPHA_DIGIT = [
    *"0123456789",
    *"abcdefghijklmnopqrstuvwxyz",
    *"ABCDEFGHIJKLMNOPQRSTUVWXYZ",
]
#: Not valid tchar, but httpx still accepts them in a field name (C48, C54).
_NONCONFORMING_NAMES = [";", '"', " "]


def _make_request(
    *,
    headers: Mapping[str, str] | None = None,
    variables: Mapping[str, Any] | None = None,
    document: str = "query Greet { greet }",
    operation: str | None = "Greet",
    url: str = "https://example.test/graphql",
    **overrides: Any,
) -> RequestInfo:
    return RequestInfo(
        operation=operation,
        kind="query",
        document=document,
        variables=variables or {},
        headers=headers or {},
        url=url,
        **overrides,
    )


def _rendered_text(value: object) -> str:
    """Every string reachable from a snapshot field, joined for substring checks.

    A mapping's keys are walked too, not only its values: a mapping key is
    text that can carry a leaked secret exactly as a value can (section 7,
    "Coverage").
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(f"{k} {_rendered_text(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " ".join(_rendered_text(v) for v in value)
    return ""


def _snapshot_text(request: RequestInfo) -> str:
    """Every string in the snapshot, plus ``repr(request)`` and the complete
    rendered ``as_curl()`` output.

    A planted secret must be absent from all of them: the report-facing
    snapshot, its ``curl_headers`` sequence, its truncation notes, the
    ``repr()`` renderer and the command-facing renderer are independent
    output paths over the same redacted data (section 7, "Coverage"), and
    each one is a place a secret smuggled through a replacement label, or
    synthesized by a renderer's own escaping, could still surface. Neither
    renderer's output is split or trimmed here: a check that only looks at
    part of ``as_curl()`` (its prefix before ``--data``, say) would miss a
    leak that only a full-body renderer's own JSON escaping can produce
    (CR-20260918T124428Z-726dee2-a66dfd92).
    """
    snapshot = request.redacted()
    curl_header_text = " ".join(
        f"{entry.name} {entry.value} {entry.curl_variable or ''}"
        for entry in snapshot.curl_headers
    )
    return " ".join(
        [
            snapshot.operation or "",
            snapshot.document,
            snapshot.url,
            snapshot.method,
            _rendered_text(snapshot.headers),
            _rendered_text(snapshot.variables),
            curl_header_text,
            " ".join(snapshot.truncated),
            repr(request),
            request.as_curl(),
        ]
    )


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def test_request_info_and_snapshot_are_exported_from_the_top_level_package() -> None:
    """C9: `RequestInfo` and `DiagnosticSnapshot` are named `__all__` members
    of the top-level package, not only importable from their own module."""
    assert pytest_graphql.RequestInfo is RequestInfo
    assert pytest_graphql.DiagnosticSnapshot is DiagnosticSnapshot
    assert "RequestInfo" in pytest_graphql.__all__
    assert "DiagnosticSnapshot" in pytest_graphql.__all__


def test_defaults_are_the_documented_ones() -> None:
    request = _make_request()
    assert request.method == "POST"
    assert request.idempotent is False
    assert request.redact_headers == DEFAULT_REDACT_HEADERS
    assert request.redact_variables == DEFAULT_REDACT_VARIABLES
    assert request.redact_values is True
    assert request.min_redacted_value_length == DEFAULT_MIN_REDACTED_VALUE_LENGTH
    assert request.max_diagnostic_bytes == DEFAULT_MAX_DIAGNOSTIC_BYTES
    assert request.max_recorded_errors == DEFAULT_MAX_RECORDED_ERRORS


# --------------------------------------------------------------------------
# redacted(): the exit criteria fixtures.
# --------------------------------------------------------------------------


def test_header_match_is_redacted() -> None:
    request = _make_request(headers={"Authorization": "Bearer secrettoken123456"})
    snapshot = request.redacted()
    assert snapshot.headers["Authorization"] == "[redacted:Authorization]"
    assert "secrettoken123456" not in _snapshot_text(request)


def test_header_name_matches_after_stripping_and_case_folding() -> None:
    request = _make_request(headers={"  AUTHORIZATION  ": "supersecretvalue1"})
    snapshot = request.redacted()
    assert snapshot.headers["  AUTHORIZATION  "] == "[redacted:  AUTHORIZATION  ]"
    assert "supersecretvalue1" not in _snapshot_text(request)


def test_cookie_header_is_redacted_by_default() -> None:
    request = _make_request(headers={"Cookie": "session=abc123456789"})
    snapshot = request.redacted()
    assert snapshot.headers["Cookie"] == "[redacted:Cookie]"
    assert "abc123456789" not in _snapshot_text(request)


def test_nested_variable_path_is_redacted_at_every_depth() -> None:
    request = _make_request(
        variables={
            "input": {
                "user": {"password": "hunter2istoolong"},
                "items": [
                    {"token": "abcdef1234567890"},
                    {"other": "fine, keep me"},
                ],
            }
        }
    )
    snapshot = request.redacted()
    variables = snapshot.variables
    assert variables["input"]["user"]["password"] == "[redacted:input.user.password]"
    # Lists are transparent to the path (C2 "inside input objects and inside lists").
    assert variables["input"]["items"][0]["token"] == "[redacted:input.items.token]"
    assert variables["input"]["items"][1]["other"] == "fine, keep me"
    assert "hunter2istoolong" not in _snapshot_text(request)
    assert "abcdef1234567890" not in _snapshot_text(request)


def test_dotted_pattern_matches_a_tail_at_any_depth() -> None:
    """DESIGN_DECISIONS.md section 7: "matched ... at every depth" holds for a
    dotted pattern too, as a contiguous tail of the path, not only when it
    names the complete path from the document root."""
    request = _make_request(
        variables={"input": {"user": {"password": "hunter2istoolong"}}},
        redact_variables=("user.password",),
    )
    snapshot = request.redacted()
    variables = snapshot.variables
    assert variables["input"]["user"]["password"] == "[redacted:input.user.password]"
    assert "hunter2istoolong" not in _snapshot_text(request)


def test_dotted_pattern_with_no_matching_tail_does_not_match() -> None:
    request = _make_request(
        variables={"note": {"password": "shouldnotmatchhere12"}},
        redact_variables=("account.password",),
    )
    snapshot = request.redacted()
    # "account.password" is not a tail of "note.password", so nothing is
    # structurally redacted; the value is still long enough to be scrubbed
    # only if it entered the secret set, which it never does when no path
    # matched it.
    assert snapshot.variables["note"]["password"] == "shouldnotmatchhere12"


def test_secret_nested_under_a_matched_path_is_still_scrubbed() -> None:
    """C16: a matched subtree's whole JSON form enters the secret set, but so
    does every scalar nested inside it, so it is still found when echoed on
    its own elsewhere."""
    secret = "mylongsecretvalue1"
    request = _make_request(
        variables={"secret": {"inner": secret}, "note": f"echo: {secret}"},
    )
    snapshot = request.redacted()
    assert snapshot.variables["secret"] == "[redacted:secret]"
    assert secret not in _snapshot_text(request)
    assert "[redacted:secret]" in snapshot.variables["note"]


def test_glob_pattern_matches_variable_paths() -> None:
    request = _make_request(
        variables={"client_secret": "verysecretvalue123"},
        redact_variables=("*_secret",),
    )
    snapshot = request.redacted()
    assert snapshot.variables["client_secret"] == "[redacted:client_secret]"


def test_short_value_is_structurally_redacted_but_not_scrubbed_elsewhere() -> None:
    """C16: below min length, a value is still redacted at its own path, but
    the free-text scrub never touches it elsewhere."""
    request = _make_request(
        variables={"password": "abcd", "note": "the code is abcd, remember it"}
    )
    snapshot = request.redacted()
    assert snapshot.variables["password"] == "[redacted:password]"
    assert snapshot.variables["note"] == "the code is abcd, remember it"


def test_token_split_by_zero_width_character_is_still_scrubbed() -> None:
    secret = "supersecrettoken1"
    split_secret = "supersecrett" + chr(0x200B) + "oken1"  # zero-width space inside
    request = _make_request(
        headers={"Authorization": secret},
        variables={"message": f"leaked here: {split_secret} oops"},
    )
    snapshot = request.redacted()
    assert "[redacted:Authorization]" in snapshot.variables["message"]
    assert secret not in _snapshot_text(request)
    assert split_secret not in _snapshot_text(request)


def test_one_secret_that_is_a_substring_of_another() -> None:
    long_secret = "longsecrettoken1234567890"
    short_secret = "secrettoken1234567890"
    request = _make_request(
        headers={"Authorization": long_secret, "X-Api-Key": short_secret},
        variables={"note": f"seen: {long_secret}"},
    )
    snapshot = request.redacted()
    note = snapshot.variables["note"]
    # The longer secret is matched whole, never leaving a mangled remainder
    # from the shorter one being replaced first.
    assert note == "seen: [redacted:Authorization]"
    assert long_secret not in _snapshot_text(request)
    assert short_secret not in _snapshot_text(request)


def test_bearer_scheme_suffix_is_a_derived_secret() -> None:
    token = "sometoken1234567890"
    request = _make_request(
        headers={"Authorization": f"Bearer {token}"},
        variables={"note": f"copied token: {token}"},
    )
    snapshot = request.redacted()
    assert token not in _snapshot_text(request)
    assert "[redacted:Authorization]" in snapshot.variables["note"]


def test_percent_encoded_form_is_a_derived_secret() -> None:
    secret = "sec ret@value123"
    encoded = "sec%20ret%40value123"
    request = _make_request(
        headers={"Authorization": secret},
        variables={"note": f"encoded copy: {encoded}"},
    )
    snapshot = request.redacted()
    assert encoded not in _snapshot_text(request)
    assert "[redacted:Authorization]" in snapshot.variables["note"]


def test_percent_encoded_scheme_suffix_is_a_derived_secret() -> None:
    """A derived form's own percent-encoded spelling must also be a derived
    secret, not only the verbatim value's (section 7, "Derived forms": "The
    percent-encoded form of each value is added")."""
    token = "abc/defghijkl"
    encoded_suffix = urllib.parse.quote(token, safe="")
    request = _make_request(
        headers={"Authorization": f"Bearer {token}"},
        variables={"note": f"copy: {encoded_suffix}"},
    )
    snapshot = request.redacted()
    assert encoded_suffix not in _snapshot_text(request)
    assert "[redacted:Authorization]" in snapshot.variables["note"]


def test_secret_in_url_path_is_scrubbed_in_snapshot_and_curl() -> None:
    """A URL is a renderer like any other: it must pass through the same
    scrub as every other field, not only have its userinfo and query
    stripped."""
    token = "urlpathsecret1234567"
    request = _make_request(
        headers={"Authorization": token},
        url=f"https://example.test/graphql/{token}",
    )
    snapshot = request.redacted()
    assert token not in snapshot.url
    assert "[redacted:Authorization]" in snapshot.url
    assert token not in request.as_curl()


def test_cookie_value_is_scrubbed_from_free_form_text() -> None:
    """C16: "every cookie value" is a secret-set member on its own, distinct
    from the whole ``Cookie`` header value that ``redact_headers`` already
    hides."""
    cookie_value = "abc1234567890xyz"
    request = _make_request(
        headers={"Cookie": f"session={cookie_value}; csrf=short"},
        variables={"note": f"leaked: {cookie_value}"},
    )
    snapshot = request.redacted()
    assert snapshot.headers["Cookie"] == "[redacted:Cookie]"
    assert cookie_value not in _snapshot_text(request)
    assert "[redacted:Cookie]" in snapshot.variables["note"]


def test_secret_used_as_a_variable_key_does_not_survive_in_the_key_itself() -> None:
    """C16 "Coverage": no text reaches a snapshot without passing the
    scrub, including a mapping key, not only a value (F03)."""
    secret = "knownsecretvalue123"
    request = _make_request(
        headers={"Authorization": f"Bearer {secret}"},
        variables={secret: "unrelated value"},
    )
    assert secret not in _snapshot_text(request)


def test_secret_appearing_as_a_header_name_is_scrubbed() -> None:
    """A header *name* is text like any other and must pass the scrub, not
    only be escaped (F03)."""
    secret = "headernamematchessecret1"
    request = _make_request(headers={secret: "value", "Authorization": secret})
    assert secret not in _snapshot_text(request)


def test_redaction_placeholder_label_scrubs_a_secret_variable_name() -> None:
    """The `[redacted:<path>]` label itself is rendered text and must pass
    the scrub the same as any other string (F03)."""
    secret = "labelsecretvalue12345"
    request = _make_request(
        headers={"Authorization": secret},
        variables={"password": secret},
    )
    assert secret not in _snapshot_text(request)


def test_percent_encoded_userinfo_password_is_scrubbed_in_decoded_form() -> None:
    """Section 7 "Derived forms": the URL userinfo value enters the secret
    set in its original (decoded) form, and its percent-encoded spelling is
    the derived form -- not the reverse -- matching how the query string
    already behaves through `parse_qsl()` (F04)."""
    decoded_password = "decoded/secretvalue123"
    encoded_password = urllib.parse.quote(decoded_password, safe="")
    request = _make_request(
        url=f"https://user:{encoded_password}@example.test/graphql",
        variables={"note": f"leaked: {decoded_password}"},
    )
    snapshot = request.redacted()
    assert decoded_password not in _snapshot_text(request)
    assert "[redacted:url]" in snapshot.variables["note"]


def test_url_userinfo_and_query_are_stripped_and_scrubbed() -> None:
    request = _make_request(
        url="https://user:hunterpass2@example.test/graphql?token=abcd1234567890",
        variables={"note": "copy: abcd1234567890"},
    )
    snapshot = request.redacted()
    assert "user" not in snapshot.url or "hunterpass2" not in snapshot.url
    assert "hunterpass2" not in snapshot.url
    assert "abcd1234567890" not in snapshot.url
    assert "?" not in snapshot.url
    assert "[redacted:url]" in snapshot.variables["note"]


def test_redact_values_false_disables_scrub_not_structural_redaction() -> None:
    request = _make_request(
        variables={"password": "onlystructural1", "note": "copy: onlystructural1"},
        redact_values=False,
    )
    snapshot = request.redacted()
    assert snapshot.variables["password"] == "[redacted:password]"
    # The scrub is off: an unrelated field echoing the same secret is untouched.
    assert snapshot.variables["note"] == "copy: onlystructural1"


def test_ipv6_url_keeps_its_brackets() -> None:
    request = _make_request(url="http://[::1]:8000/graphql")
    assert request.redacted().url == "http://[::1]:8000/graphql"


def test_ipv6_url_without_a_port_keeps_its_brackets() -> None:
    request = _make_request(url="http://[::1]/graphql")
    assert request.redacted().url == "http://[::1]/graphql"


def test_ipv6_url_strips_userinfo_and_keeps_brackets() -> None:
    request = _make_request(url="http://user:pass@[::1]:8000/graphql?x=1")
    assert request.redacted().url == "http://[::1]:8000/graphql"


def test_redacted_header_placeholder_escapes_a_control_character_in_the_name() -> None:
    """Python's ``str.strip()`` treats a trailing carriage return as
    whitespace, so this header name still matches ``authorization`` after
    normalizing; the raw control character must not reach the rendered
    placeholder or the snapshot's header key unescaped."""
    name = "Authorization\r"
    request = _make_request(headers={name: "supersecretvalue123456"})
    snapshot = request.redacted()
    rendered = "".join(snapshot.headers.keys()) + "".join(snapshot.headers.values())
    assert "\r" not in rendered
    assert "\\x0d" in rendered


def test_variable_key_control_characters_are_escaped() -> None:
    key = "a\x1b[2Jb"
    request = _make_request(variables={key: "harmlessvalue"})
    snapshot = request.redacted()
    rendered_keys = "".join(snapshot.variables.keys())
    assert "\x1b" not in rendered_keys
    assert "\\x1b" in rendered_keys


def test_secret_map_is_not_a_public_attribute() -> None:
    """C16: the secret set is "never returned by a public API"."""
    assert not hasattr(_make_request(), "secret_map")


def test_scrub_removes_this_requests_known_secrets() -> None:
    token = "requestboundsecret1"
    request = _make_request(headers={"Authorization": token})
    scrubbed = request.scrub(f"the server said: {token}")
    assert token not in scrubbed
    assert "[redacted:Authorization]" in scrubbed


def test_scrub_is_a_no_op_when_redact_values_is_false() -> None:
    token = "requestboundsecret2"
    request = _make_request(headers={"Authorization": token}, redact_values=False)
    assert request.scrub(f"copy: {token}") == f"copy: {token}"


# --------------------------------------------------------------------------
# __repr__ / __str__
# --------------------------------------------------------------------------


def test_repr_and_str_never_contain_a_planted_secret() -> None:
    request = _make_request(
        headers={"Authorization": "Bearer topsecretvalue12345"},
        variables={"input": {"password": "anothersecretvalue1"}},
    )
    assert "topsecretvalue12345" not in repr(request)
    assert "anothersecretvalue1" not in repr(request)
    assert "topsecretvalue12345" not in str(request)
    assert "anothersecretvalue1" not in str(request)
    assert "[redacted:" in repr(request)


# --------------------------------------------------------------------------
# scrub_text() / escape_control_characters(), exposed standalone (C59).
# --------------------------------------------------------------------------


def test_scrub_text_is_usable_standalone() -> None:
    """``secrets`` maps a value to the complete replacement text: scrub_text
    never builds or wraps a marker itself, so a standalone caller supplies
    whatever already-safe text (for example ``[redacted:custom]``) it wants
    substituted (F03: the marker is validated as one complete string, not
    assembled from an unchecked label after the fact)."""
    assert (
        scrub_text("hello secretvalue1 world", {"secretvalue1": "[redacted:custom]"})
        == "hello [redacted:custom] world"
    )
    assert scrub_text("nothing to see", {}) == "nothing to see"


def test_escape_control_characters_covers_the_documented_alphabet() -> None:
    nbsp = chr(0x00A0)
    zwsp = chr(0x200B)
    tag_char = chr(0xE0041)
    text = f"a\tb\ncarriage\rreturn{nbsp}nbsp{zwsp}zwsp{tag_char}tag"
    escaped = escape_control_characters(text)
    assert "\t" in escaped  # tab survives
    assert "\n" in escaped  # newline survives
    assert "\\x0d" in escaped  # carriage return escaped, below U+0100
    assert "\\xa0" in escaped  # NBSP escaped, below U+0100
    assert "\\u200b" in escaped  # zero-width space escaped, below U+10000
    assert "\\U000e0041" in escaped  # Unicode tag character, at/above U+10000
    assert "\r" not in escaped
    assert nbsp not in escaped
    assert zwsp not in escaped


# --------------------------------------------------------------------------
# Truncation (C2: "visible, never silent").
# --------------------------------------------------------------------------


def test_truncation_is_recorded_with_a_byte_count() -> None:
    request = _make_request(
        document="x" * 50,
        max_diagnostic_bytes=10,
    )
    snapshot = request.redacted()
    assert len(snapshot.document.encode("utf-8")) <= 10
    assert any(note.startswith("document: truncated") for note in snapshot.truncated)


def test_no_truncation_note_when_nothing_is_cut() -> None:
    snapshot = _make_request().redacted()
    assert snapshot.truncated == ()


def test_byte_limits_bound_a_large_number_of_non_string_elements() -> None:
    """A non-string leaf (a plain number here) must count against the byte
    budget the same as a string leaf does, or a large enough collection of
    them produces unbounded output. This must hold per field -- a shared
    total budget alone is not enough, because a single oversized field could
    still exhaust it on its own (F01)."""
    request = _make_request(
        variables={"items": list(range(20000))},
        max_diagnostic_bytes=16,
    )
    snapshot = request.redacted()
    fields = (
        snapshot.operation,
        snapshot.document,
        snapshot.url,
        snapshot.method,
        dict(snapshot.headers),
        dict(snapshot.variables),
    )
    for field in fields:
        assert len(json.dumps(field, sort_keys=True).encode("utf-8")) <= 16
    total = sum(
        len(json.dumps(field, sort_keys=True).encode("utf-8")) for field in fields
    )
    assert total <= 32768
    assert snapshot.truncated


def test_byte_limits_bound_an_oversized_key() -> None:
    """A mapping key must count against the byte budget the same as a value
    does, or an oversized key alone produces unbounded output."""
    request = _make_request(
        headers={"x" * 200_000: "value"},
        variables={"k" * 200_000: "value"},
        max_diagnostic_bytes=4096,
    )
    snapshot = request.redacted()
    assert all(len(key) < 200_000 for key in snapshot.headers)
    assert all(len(key) < 200_000 for key in snapshot.variables)
    assert snapshot.truncated


def test_byte_limits_bound_the_method_field() -> None:
    """`method` must be capped like any other request-controlled field
    (F01): the original limiter never included it, so an oversized method
    reached a snapshot, a repr and `as_curl()` unbounded."""
    request = _make_request(method="X" * 100_000, max_diagnostic_bytes=16)
    snapshot = request.redacted()
    assert len(snapshot.method.encode("utf-8")) <= 16
    assert snapshot.truncated
    assert len(request.as_curl()) < 10_000


def test_byte_limits_bound_a_large_matched_header_name() -> None:
    """A redacted header's curl placeholder must derive from a bounded name:
    the original implementation read the live, unbounded name to build both
    the placeholder literal and its variable identifier (F01)."""
    name = "x" * 100_000
    request = _make_request(
        headers={name: "supersecretvalue123456"},
        redact_headers=frozenset({name}),
        max_diagnostic_bytes=16,
    )
    command = request.as_curl()
    assert len(command) < 10_000
    assert "supersecretvalue123456" not in command


def test_byte_limits_bound_a_large_unmatched_header_name() -> None:
    name = "x" * 100_000
    request = _make_request(headers={name: "value"}, max_diagnostic_bytes=16)
    assert len(request.as_curl()) < 10_000


@pytest.mark.parametrize(
    ("field_name", "override"),
    [
        ("document", {"document": '"' * 200}),
        ("document", {"document": "\\" * 200}),
        ("document", {"document": "\t\n" * 100}),
        ("document", {"document": "é" * 200}),
        ("url", {"url": "https://example.test/graphql?q=" + "é" * 200}),
    ],
)
def test_byte_limits_measure_the_actual_json_encoding_not_raw_length(
    field_name: str, override: dict[str, str]
) -> None:
    """A quote, a backslash, a tab or newline, or a non-ASCII character each
    cost more in ``json.dumps``'s encoding than one raw byte; the cap must
    bound that encoded cost, not the character or raw-byte count (F01)."""
    request = _make_request(max_diagnostic_bytes=16, **override)
    snapshot = request.redacted()
    field = getattr(snapshot, field_name)
    assert len(json.dumps(field).encode("utf-8")) <= 16


def test_byte_limits_measure_the_actual_json_encoding_for_variable_values() -> None:
    request = _make_request(
        variables={"note": '"\\' * 200 + "é" * 200}, max_diagnostic_bytes=16
    )
    snapshot = request.redacted()
    assert len(json.dumps(dict(snapshot.variables)).encode("utf-8")) <= 16


def test_byte_limits_bound_a_redacted_headers_placeholder_not_only_its_name() -> None:
    """A redacted header's rendered ``[redacted:<name>]`` placeholder is
    synthesized text with its own bytes; the header field cap must bound the
    placeholder's complete cost, not just the name that feeds it (F01)."""
    name = "x" * 3000
    request = _make_request(
        headers={name: "supersecretvalue123456"},
        redact_headers=frozenset({name}),
        max_diagnostic_bytes=4096,
    )
    snapshot = request.redacted()
    assert (
        len(json.dumps(dict(snapshot.headers), sort_keys=True).encode("utf-8")) <= 4096
    )


def test_curl_headers_shares_the_headers_field_budget() -> None:
    """``curl_headers`` is not a second, uncapped field: its complete
    rendered form (every entry's name and value together) is bounded by the
    same budget the public ``headers`` field is derived from (F01)."""
    headers = {f"x-{i:05d}": "y" * 200 for i in range(50)}
    request = _make_request(headers=headers, max_diagnostic_bytes=4096)
    snapshot = request.redacted()
    total = sum(
        len(entry.name.encode("utf-8")) + len(entry.value.encode("utf-8"))
        for entry in snapshot.curl_headers
    )
    assert total <= 4096


def test_truncated_notes_never_contain_the_request_controlled_key() -> None:
    """A truncation note names only the field and a byte or entry count,
    never the key or value content that was cut, however long (F01)."""
    key = "k" * 100_000
    request = _make_request(variables={key: "v"}, max_diagnostic_bytes=16)
    snapshot = request.redacted()
    assert snapshot.truncated
    for note in snapshot.truncated:
        assert key not in note
        assert len(note) < 200


def test_tiny_field_limit_still_charges_its_unavoidable_minimum() -> None:
    """Below the minimum representable size, a mandatory field that cannot
    be omitted still renders its smallest possible form, and that form's
    real cost is charged, not left invisible to the budget (F01)."""
    request = _make_request(document="hello world", max_diagnostic_bytes=1)
    snapshot = request.redacted()
    assert snapshot.document == ""
    assert snapshot.truncated


def test_header_entry_below_its_structural_minimum_is_dropped_not_forced() -> None:
    """A header whose own structural minimum -- overhead plus an empty name
    and an empty value -- cannot fit is dropped outright, matching "a header
    that cannot fit at all, including its own structural overhead, is
    dropped" (DESIGN_DECISIONS.md section 7), rather than force-rendered
    past the field's own cap the way a mandatory scalar field is (F01)."""
    request = _make_request(headers={"A": "b"}, max_diagnostic_bytes=4)
    snapshot = request.redacted()
    assert dict(snapshot.headers) == {}
    assert snapshot.truncated


def test_header_entry_at_its_structural_minimum_renders_exactly_at_cap() -> None:
    """Once a header's guaranteed minimum fits, it is rendered, and the
    field's real cost never exceeds the cap it was rendered under (F01)."""
    request = _make_request(headers={"A": "b"}, max_diagnostic_bytes=8)
    snapshot = request.redacted()
    rendered = json.dumps(dict(snapshot.headers), sort_keys=True).encode("utf-8")
    assert len(rendered) <= 8


def test_total_cap_is_never_exceeded_even_by_forced_field_minimums() -> None:
    """A field consuming the whole shared total must still leave room for
    every later field's own unavoidable minimum, or the true grand total
    exceeds the documented 32768-byte cap (F01)."""
    request = _make_request(
        operation="Op",
        document="x" * 40000,
        max_diagnostic_bytes=40000,
    )
    snapshot = request.redacted()
    fields = (
        snapshot.operation,
        snapshot.document,
        snapshot.url,
        snapshot.method,
        dict(snapshot.headers),
        dict(snapshot.variables),
    )
    total = sum(
        len(json.dumps(field, sort_keys=True).encode("utf-8")) for field in fields
    )
    assert total <= 32768


def test_curl_variable_is_independently_bounded_not_derived_unbounded() -> None:
    """The derived curl placeholder variable can expand far past its source
    name's JSON-charged cost -- each underscore becomes ``_5F_`` -- so it
    must be bounded on its own against the same header budget, not left to
    grow unbounded because the name's JSON cost happened to stay small
    (F01)."""
    name = "_" * 3000
    request = _make_request(
        headers={name: "supersecretvalue123456"},
        redact_headers=frozenset({name}),
        max_diagnostic_bytes=4096,
    )
    command = request.as_curl()
    assert len(command) < 20000
    assert "supersecretvalue123456" not in command


@pytest.mark.parametrize("value", [123456789, True, None])
def test_dropped_scalar_entry_is_counted_once_not_twice(value: Any) -> None:
    """A scalar, boolean or ``None`` that cannot fit its own budget is
    counted as exactly one dropped entry, by its owning container alone --
    not once by the scalar path and again by the container (F02)."""
    request = _make_request(variables={"a": value}, max_diagnostic_bytes=6)
    snapshot = request.redacted()
    assert dict(snapshot.variables) == {}
    note = next(n for n in snapshot.truncated if n.startswith("variables"))
    assert "1 entry dropped" in note


def test_dropped_nested_mapping_is_counted_once_not_twice() -> None:
    """A nested mapping that cannot fit its own structural overhead is
    counted as exactly one dropped entry by its parent, not additionally by
    its own bracket-charge failure (F02)."""
    request = _make_request(variables={"a": {"b": "c"}}, max_diagnostic_bytes=6)
    snapshot = request.redacted()
    assert dict(snapshot.variables) == {}
    note = next(n for n in snapshot.truncated if n.startswith("variables"))
    assert "1 entry dropped" in note


def test_dropped_nested_list_is_counted_once_not_twice() -> None:
    """The list counterpart of the same property (F02)."""
    request = _make_request(variables={"a": ["b"]}, max_diagnostic_bytes=6)
    snapshot = request.redacted()
    assert dict(snapshot.variables) == {}
    note = next(n for n in snapshot.truncated if n.startswith("variables"))
    assert "1 entry dropped" in note


def test_three_secret_fallback_collision_still_never_leaks() -> None:
    """The fixed generic fallback label can itself equal a third qualifying
    secret; that collision must also be checked and escalated, not left to
    reach the placeholder unverified (F03)."""
    first_secret = "firstsecret111"
    second_secret = "secondsecret222"
    third_secret = "redacted-source"
    request = _make_request(
        headers={
            second_secret: first_secret,
            "Authorization": second_secret,
            "X-Third": third_secret,
        },
        redact_headers=frozenset({second_secret.lower(), "authorization", "x-third"}),
        variables={"note": f"echo {first_secret}"},
    )
    snapshot = request.redacted()
    assert first_secret not in _snapshot_text(request)
    assert second_secret not in _snapshot_text(request)
    assert third_secret not in _snapshot_text(request)
    assert snapshot.variables["note"] != f"echo [redacted:{third_secret}]"


def test_alternate_percent_case_spelling_distinct_from_source_is_scrubbed() -> None:
    """Adding only the exact spelling and the canonical uppercase spelling
    covers two of the exponentially many per-digit case combinations; an
    alternate spelling distinct from both must still be caught by
    case-insensitive percent-escape matching, not enumeration (F04)."""
    decoded = "decoded/part:value123"
    original_spelling = "decoded%2fpart%3avalue123"
    alternate_spelling = "decoded%2Fpart%3avalue123"
    assert original_spelling != alternate_spelling
    assert urllib.parse.quote(decoded, safe="") not in (
        original_spelling,
        alternate_spelling,
    )
    request = _make_request(
        url=f"https://user:{original_spelling}@example.test/graphql",
        variables={"note": f"leaked: {alternate_spelling}"},
    )
    assert alternate_spelling not in _snapshot_text(request)


def test_two_secrets_do_not_leak_through_each_others_replacement_label() -> None:
    """A redacted header's own name can be another header's secret value;
    the placeholder built from that name must not smuggle the second secret
    into free-form text the first secret's redaction touches (F03)."""
    first_secret = "firstsecret111"
    second_secret = "secondsecret222"
    request = _make_request(
        headers={second_secret: first_secret, "Authorization": second_secret},
        redact_headers=frozenset({second_secret.lower(), "authorization"}),
        variables={"note": f"echo {first_secret}"},
    )
    snapshot = request.redacted()
    assert first_secret not in _snapshot_text(request)
    assert second_secret not in _snapshot_text(request)
    assert snapshot.variables["note"] == "echo [redacted:redacted-source]"


def test_lowercase_percent_encoded_userinfo_password_is_scrubbed() -> None:
    """A percent-escape's hex digits are case-insensitive (RFC 3986); a URL
    spelled with lowercase hex must be caught the same as the canonical
    uppercase form ``_add_secret`` derives (F04)."""
    password = "decoded%2fsecretvalue123"
    request = _make_request(
        url=f"https://user:{password}@example.test/graphql",
        variables={"note": f"leaked: {password}"},
    )
    assert password not in _snapshot_text(request)


def test_mixed_case_percent_encoded_userinfo_username_is_scrubbed() -> None:
    username = "decoded%2Fmixedcase%2fUSER123"
    request = _make_request(
        url=f"https://{username}:pw@example.test/graphql",
        variables={"note": f"leaked: {username}"},
    )
    assert username not in _snapshot_text(request)


def test_credential_equal_to_the_marker_template_word_is_still_scrubbed() -> None:
    """A credential's value can itself equal the fixed word the replacement
    template is built from ("redacted"), independent of any source label;
    checking only the label can never catch this, since the template text
    is not a label at all. The complete marker is validated instead, and
    when even that cannot be made safe, the match is dropped rather than
    left as a substring of the placeholder that names it (CR-...-3be650f3-F02,
    re-flagging edc5e13e-F03)."""
    request = _make_request(
        headers={"Authorization": "redacted"},
        variables={"note": "echo redacted"},
    )
    snapshot = request.redacted()
    assert "redacted" not in snapshot.variables["note"]
    assert "redacted" not in _snapshot_text(request)


def test_percent_case_label_collision_with_scrub_equivalence_is_caught() -> None:
    """``safe_label`` (now the marker builder) must use the same case-folded
    percent-escape equivalence ``scrub_text`` matches with, not literal
    substring comparison: a header named with the alternate hex-case
    spelling of a qualifying secret must still be treated as colliding
    (CR-...-3be650f3-F02, re-flagging edc5e13e-F03)."""
    secret = "second%2fsecret222"
    label = "second%2Fsecret222"
    request = _make_request(
        headers={label: secret},
        redact_headers=frozenset({label.lower()}),
        variables={"note": f"echo {secret}"},
    )
    snapshot = request.redacted()
    assert secret not in snapshot.variables["note"]
    assert secret not in _snapshot_text(request)


def test_marker_construction_terminates_when_every_short_string_collides() -> None:
    """A secret set that covers every character the random escalation source
    can draw from makes every random attempt collide, forever, under an
    unbounded retry. Construction must instead be total: bounded attempts,
    then a label-free marker, then an empty replacement, so this returns
    promptly instead of hanging (CR-20260918T114303Z-726dee2-3be650f3-F02)."""
    hex_digits = "0123456789abcdef"
    headers = {f"h{i}": digit for i, digit in enumerate(hex_digits)}
    request = _make_request(
        headers=headers,
        redact_headers=frozenset(headers.keys()),
        min_redacted_value_length=1,
    )
    snapshot = request.redacted()
    for entry in snapshot.curl_headers:
        for digit in hex_digits:
            assert digit not in entry.value


def test_curl_variable_truncation_never_collides_two_distinct_headers() -> None:
    """The name-to-variable mapping is injective: truncating a variable's own
    text can make two unrelated headers share a shortened prefix, sending
    one header's exported secret in another header's place. The complete
    variable is charged whole or not at all -- never partially
    (CR-20260918T114303Z-726dee2-3be650f3-F01)."""
    request = _make_request(
        headers={"a": "secretvalueforA", "ab": "secretvalueforAB"},
        redact_headers=frozenset({"a", "ab"}),
        max_diagnostic_bytes=82,
    )
    snapshot = request.redacted()
    variables = [entry.curl_variable for entry in snapshot.curl_headers]
    non_none = [v for v in variables if v is not None]
    assert len(non_none) == len(set(non_none))
    command = request.as_curl()
    assert "secretvalueforA" not in command
    assert "secretvalueforAB" not in command


def test_control_character_escape_cannot_synthesize_a_second_secret() -> None:
    """A marker was validated pre-escape, but escaping always runs on it
    afterward: a source label containing a raw control character can pass
    that pre-escape check and still, once escaped, contain a second
    qualifying credential's own literal spelling as a substring. Here the
    matched variable path's own control character escapes to the four
    characters ``\\x00``, which is exactly the redacted header ``z``'s own
    value. This is not only a marker's own risk: the matched variable's
    *key* carries the same raw control character and goes through the same
    scrub-then-escape order, so it must be checked too, alongside every
    other rendered form -- including ``repr(request)`` and the complete
    ``as_curl()`` body, not only its prefix (CR-20260918T121132Z-726dee2-
    95ce6874 and CR-20260918T124428Z-726dee2-a66dfd92, re-flagging
    edc5e13e-F03 a fourth and fifth time)."""
    credential1 = "seedcredential1valuelong12345"
    credential2 = "\\x00"
    request = _make_request(
        document=f'query {{ field(arg: "{credential1}") }}',
        variables={f"secretfield{chr(0)}": credential1},
        redact_variables=("*",),
        headers={"z": credential2},
        redact_headers=frozenset({"z"}),
        min_redacted_value_length=1,
    )
    assert credential2 not in _snapshot_text(request)


def test_downstream_serializer_escaping_cannot_synthesize_a_second_secret() -> None:
    """A marker safe in the snapshot's own plain-text fields is not
    necessarily safe once a *further* renderer re-escapes it: both
    ``repr()`` and ``json.dumps()`` double a literal backslash. A marker
    built from a label containing exactly one backslash is safe against
    every qualifying secret in the snapshot's own text, but that same
    marker, once ``RequestInfo.__repr__`` or ``as_curl()``'s JSON body
    doubles its one backslash into two, recreates a second qualifying
    credential whose own value is exactly two literal backslashes
    (CR-20260918T124428Z-726dee2-a66dfd92, re-flagging edc5e13e-F03 a fifth
    time)."""
    credential1 = "seedcredential1valuelong12345"
    credential2 = "\\\\"
    request = _make_request(
        headers={"a\\b": credential1, "c": credential2},
        redact_headers=frozenset({"a\\b", "c"}),
        min_redacted_value_length=1,
    )
    assert credential2 not in _snapshot_text(request)


def test_marker_construction_draws_no_randomness_when_the_label_is_already_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authority says random escalation happens only once the real and
    fixed labels collide; generating every random candidate up front spends
    unnecessary cryptographic-random draws on the ordinary, already-safe
    request (CR-20260918T121132Z-726dee2-95ce6874-F01)."""
    draws: list[int] = []
    monkeypatch.setattr(
        "pytest_graphql._core.diagnostics._random_secret_token_hex",
        lambda n: draws.append(n) or "unused",
    )
    request = _make_request(headers={"Authorization": "seedcredential1valuelong12345"})
    request.redacted()
    assert draws == []


# --------------------------------------------------------------------------
# as_curl(): the C48/C54 suite.
# --------------------------------------------------------------------------


def test_curl_header_variable_mapping_examples() -> None:
    assert _curl_header_variable("x-api-key") == "PYTEST_GQL_HEADER_X_2D_API_2D_KEY"
    assert _curl_header_variable("x_api_key") == "PYTEST_GQL_HEADER_X_5F_API_5F_KEY"
    assert _curl_header_variable("x-api-key") != _curl_header_variable("x_api_key")
    assert _curl_header_variable("X-API-Key") == _curl_header_variable("x-api-key")


def _write_curl_stub(bin_dir: Path, out_file: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\0" "$@" > "{out_file}"\n')
    mode = stub.stat().st_mode
    stub.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_curl_command_through_sh(
    command: str, tmp_path: Path, env_overrides: Mapping[str, str] | None = None
) -> list[str]:
    """Execute a generated ``as_curl()`` command under ``/bin/sh``.

    ``curl`` on ``PATH`` is replaced by a stub that dumps its NUL-separated
    argv to a file, so the test observes exactly what the shell handed to
    the command, including any expansion or word-splitting, without making a
    network call. The shell's working directory is ``tmp_path``, so a
    command-injection attempt that runs (it should not) leaves its evidence
    there instead of in the repository checkout.

    Skipped outside a POSIX platform: there is no ``/bin/sh`` to round-trip
    the command through, and ``as_curl()``'s own quoting is verified against
    a POSIX shell by design, not against the platform running the test.
    """
    if os.name != "posix":
        pytest.skip("requires a POSIX shell (/bin/sh), not available on this platform")
    bin_dir = tmp_path / "bin"
    out_file = tmp_path / "argv.bin"
    _write_curl_stub(bin_dir, out_file)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    raw = out_file.read_bytes()
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [part.decode("utf-8") for part in parts]


_TCHAR_SWEEP = list(_TCHAR_SPECIALS) + _TCHAR_ALPHA_DIGIT + _NONCONFORMING_NAMES


@pytest.mark.parametrize("special", _TCHAR_SWEEP)
def test_unmatched_header_name_survives_a_shell_round_trip(
    special: str, tmp_path: Path
) -> None:
    name = f"x{special}header"
    request = _make_request(headers={name: "plainvalue"})
    argv = _run_curl_command_through_sh(request.as_curl(), tmp_path)
    assert f"{name}: plainvalue" in argv


@pytest.mark.parametrize("special", _TCHAR_SWEEP)
def test_redacted_header_name_is_never_expanded_or_executed(
    special: str, tmp_path: Path
) -> None:
    name = f"$HOME{special}x-api-key"
    request = _make_request(
        headers={name: "supersecretvalue123"},
        redact_headers=frozenset({name.strip().lower()}),
    )
    argv = _run_curl_command_through_sh(request.as_curl(), tmp_path)
    # The env var is not exported, so it expands to empty: the captured
    # argument is exactly the literal name text, unexpanded and unexecuted,
    # with nothing appended from the (unset) placeholder.
    assert f"{name}: " in argv
    assert not any("supersecretvalue123" in part for part in argv)
    assert not any(str(Path.home()) in part for part in argv if part)


@pytest.mark.parametrize(
    "name",
    [
        "x`touch pwned`header",
        "x$(touch pwned)header",
    ],
)
def test_redacted_header_name_resists_a_paired_substitution_form(
    name: str, tmp_path: Path
) -> None:
    """A single stray backtick cannot form ``` `cmd` ```, and the earlier
    sweep never places two backticks or a ``$(...)`` in one name. This
    covers the substitution form C48 actually names as its threat."""
    request = _make_request(
        headers={name: "supersecretvalue123"},
        redact_headers=frozenset({name.strip().lower()}),
    )
    argv = _run_curl_command_through_sh(request.as_curl(), tmp_path)
    assert f"{name}: " in argv
    assert not any("supersecretvalue123" in part for part in argv)
    assert not (tmp_path / "pwned").exists()


def test_exported_redacted_header_value_is_not_split_or_glob_expanded(
    tmp_path: Path,
) -> None:
    """The placeholder's double-quoted expansion must deliver the exported
    value as exactly one argument, unsplit and unglobbed, even when a
    matching file sits in the shell's working directory."""
    (tmp_path / "afile.txt").write_text("marker")
    request = _make_request(headers={"x-api-key": "unused-live-value"})
    variable = _curl_header_variable("x-api-key")
    value = "has space and * glob"
    argv = _run_curl_command_through_sh(
        request.as_curl(), tmp_path, env_overrides={variable: value}
    )
    assert f"x-api-key: {value}" in argv
    assert argv.count(f"x-api-key: {value}") == 1


def test_as_curl_survives_an_oversized_unmatched_header_name() -> None:
    """A header name the size limiter truncates must never desync
    `as_curl()` from the bounded representation via a stale key lookup into
    a mapping that no longer has that key (F02: this used to raise
    `KeyError`)."""
    name = "x" * 5000
    request = _make_request(headers={name: "value"}, max_diagnostic_bytes=4096)
    command = request.as_curl()
    assert "-H " in command


def test_as_curl_survives_enough_headers_to_exhaust_the_total_budget() -> None:
    """Enough ordinary headers to exhaust the total budget must be dropped,
    not desync the curl renderer from the report mapping (F02)."""
    headers = {f"x-{i:05d}": "value" for i in range(500)}
    request = _make_request(headers=headers)
    command = request.as_curl()
    assert command.count("-H ") < 500


def test_as_curl_never_contains_a_planted_secret() -> None:
    request = _make_request(
        headers={"Authorization": "Bearer curlsecretvalue1"},
        variables={"input": {"password": "curlsecretvalue2"}},
    )
    command = request.as_curl()
    assert "curlsecretvalue1" not in command
    assert "curlsecretvalue2" not in command
    assert "PYTEST_GQL_HEADER_AUTHORIZATION" in command


def test_final_boundary_collision_fails_closed_instead_of_leaking(
    tmp_path: Path,
) -> None:
    """The rendering-boundary defense used to splice its replacement into
    the *already shell-quoted* command: a qualifying secret chosen equal to
    the quote-and-JSON prefix or suffix that ``--data``'s argument is wrapped
    in matched and removed those exact delimiter characters along with it,
    turning an inert ``$(...)`` in the document into a live, executed shell
    command substitution (CR-20260918T140432Z-726dee2-acfd8932-F01, a P0).

    The fix that closed that P0 checked each value against its own
    downstream serializer's output before quoting, but still substituted
    the *complete* ``--data`` JSON document as one leaf when the outer
    ``shlex.quote`` call collided -- discarding the structured request body
    ``as_curl()`` promises to reproduce for exactly this adversarial case
    (CR-20260918T163912Z-726dee2-00e70e59-F02, a P1). A JSON document cannot
    be replaced by a marker without corrupting it, and the shell-quote
    delimiter it collides with is fixed syntax the command cannot omit
    either, so there is no safe rendering left to construct: this now
    raises ``DiagnosticRenderError`` instead of returning a leaking or
    malformed command."""
    document = "query { field } $(touch pwned)"
    probe_body = {"query": document, "variables": {}}
    probe_quoted = shlex.quote(json.dumps(probe_body, sort_keys=True))
    prefix = probe_quoted[:14]
    suffix = probe_quoted[-14:]

    request = _make_request(
        document=document,
        variables={},
        headers={"x-secret-1": prefix, "x-secret-2": suffix},
        redact_headers=frozenset({"x-secret-1", "x-secret-2"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError):
        request.as_curl()
    assert not (tmp_path / "pwned").exists()


def test_as_curl_data_argument_is_always_valid_json_when_it_succeeds(
    tmp_path: Path,
) -> None:
    """CR-20260918T163912Z-726dee2-00e70e59-F02: the ordinary, non-colliding
    case must still produce a genuinely parseable JSON ``--data`` body, not
    only a command that is safe to run through a shell."""
    request = _make_request(
        document="query Greet { greet }",
        variables={"name": "value"},
        headers={"authorization": "Bearer plaincredential"},
    )
    argv = _run_curl_command_through_sh(request.as_curl(), tmp_path)
    data_index = argv.index("--data") + 1
    payload = json.loads(argv[data_index])
    assert payload["query"] == "query Greet { greet }"
    assert payload["operationName"] == "Greet"
    assert payload["variables"] == {"name": "value"}


def test_repr_fails_closed_on_a_secret_equal_to_fixed_wrapper_text() -> None:
    """CR-20260918T163912Z-726dee2-00e70e59-F01: a leaf-level check cannot
    see the fixed ``repr()`` wrapper text ``__repr__`` adds around its
    leaves. A qualifying secret equal to a substring of ``"RequestInfo("``
    reaches the final text regardless of any leaf's own content, because
    that text is not a leaf at all -- no per-leaf substitution can omit it,
    so this must raise rather than return text containing the secret."""
    request = _make_request(
        headers={"x-secret": "RequestInfo("},
        redact_headers=frozenset({"x-secret"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError):
        repr(request)


def test_as_curl_fails_closed_on_a_secret_spanning_two_quoted_segments() -> None:
    """CR-20260918T163912Z-726dee2-00e70e59-F01: a redacted header's curl
    argument is two independently quoted segments joined by fixed template
    text (module docstring, "as_curl()") that no ``render`` call ever sees
    as a whole. A qualifying secret equal to the literal junction between
    the closing single quote and the opening double quote is present in the
    final command even though neither quoted segment's own render contains
    it, so this must raise rather than return the leaking command."""
    request = _make_request(
        headers={
            "authorization": "Bearer plaincredential",
            "x-secret": "'\"$",
        },
        redact_headers=frozenset({"authorization", "x-secret"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError):
        request.as_curl()


def test_repr_fails_closed_on_a_secret_recreated_by_a_key_disambiguation_suffix() -> (
    None
):
    """CR-20260918T163912Z-726dee2-00e70e59-F01: two distinct variable keys
    that each independently collide with their own qualifying secret
    collapse onto the same shared hardened marker text; the numeric ``#2``
    suffix that keeps them distinct is generated *after* every leaf check
    already ran, so nothing validates it on its own. A third qualifying
    secret equal to the disambiguated key's exact text is present in the
    final repr() even though it was never any leaf's own rendered form, so
    this must raise rather than return the leaking text."""
    request = _make_request(
        variables={"a\\b": "irrelevant", "c\\d": "irrelevant"},
        redact_variables=(),
        headers={
            "x-secret-1": "a\\\\b",
            "x-secret-2": "c\\\\d",
            "x-secret-3": "[redacted:redacted-source]#2",
        },
        redact_headers=frozenset({"x-secret-1", "x-secret-2", "x-secret-3"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError):
        repr(request)


def test_diagnostic_render_error_is_exported_from_the_top_level_package() -> None:
    """CR-20260918T171638Z-726dee2-2a6955c1-F02: ``DiagnosticRenderError`` is
    a caller-facing exception a user must be able to catch through the
    package's supported top-level import, per "Top-level surface"
    (DESIGN_DECISIONS.md section 3): the exception hierarchy belongs in
    ``__all__``, not only at its private ``_core.errors`` implementation
    path."""
    assert pytest_graphql.DiagnosticRenderError is DiagnosticRenderError
    assert "DiagnosticRenderError" in pytest_graphql.__all__


def _assert_exception_state_leaks_nothing(
    exc: DiagnosticRenderError, secret: str
) -> None:
    """CR-20260918T175410Z-726dee2-f55d6151-F01: a qualifying secret unrelated
    to the collision that triggered the fail-closed path must not appear in
    any textual state the exception carries, not only in ``str(exc)``: its
    ``renderer`` attribute, its ``args``, and ``vars(exc)`` as a whole."""
    assert secret not in str(exc)
    assert secret not in repr(exc.renderer)
    assert all(secret not in str(value) for value in exc.args)
    assert all(secret not in repr(value) for value in vars(exc).values())


def test_repr_fail_closed_exception_never_leaks_a_second_qualifying_secret() -> None:
    """CR-20260918T171638Z-726dee2-2a6955c1-F01, CR-20260918T175410Z-726dee2-
    f55d6151-F01: the fail-closed exception's own descriptive message and its
    ``renderer`` label are both fixed, compile-time text, exactly as exposed
    to a boundary collision as any other fixed rendering syntax. A second,
    unrelated qualifying secret equal to a substring of the message
    ("redacted value") must not appear anywhere in the exception's state,
    even though it played no part in triggering the fail-closed path."""
    request = _make_request(
        headers={
            "x-secret-wrapper": "RequestInfo(",
            "x-secret-message": "redacted value",
        },
        redact_headers=frozenset({"x-secret-wrapper", "x-secret-message"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError) as exc_info:
        repr(request)
    _assert_exception_state_leaks_nothing(exc_info.value, "redacted value")


def test_repr_fail_closed_exception_never_leaks_a_secret_equal_to_its_renderer() -> (
    None
):
    """CR-20260918T175410Z-726dee2-f55d6151-F01: a qualifying secret equal to
    the ``repr()`` renderer's own fixed label must not survive in the raised
    exception's ``renderer`` attribute, even though that label originates
    from module-fixed text rather than from the request."""
    request = _make_request(
        headers={
            "x-secret-wrapper": "RequestInfo(",
            "x-secret-renderer": "repr()",
        },
        redact_headers=frozenset({"x-secret-wrapper", "x-secret-renderer"}),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError) as exc_info:
        repr(request)
    _assert_exception_state_leaks_nothing(exc_info.value, "repr()")


def test_as_curl_fail_closed_exception_never_leaks_a_second_qualifying_secret() -> None:
    """CR-20260918T171638Z-726dee2-2a6955c1-F01, CR-20260918T175410Z-726dee2-
    f55d6151-F01: the ``as_curl()`` fail-closed path must not let its own
    exception state repeat a second qualifying secret either, even when the
    collision that triggered it was the adjacent-quoted-segment boundary
    rather than the exception's own text."""
    request = _make_request(
        headers={
            "authorization": "Bearer plaincredential",
            "x-secret-boundary": "'\"$",
            "x-secret-message": "redacted value",
        },
        redact_headers=frozenset(
            {"authorization", "x-secret-boundary", "x-secret-message"}
        ),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError) as exc_info:
        request.as_curl()
    _assert_exception_state_leaks_nothing(exc_info.value, "redacted value")


def test_as_curl_fail_closed_exception_never_leaks_a_secret_equal_to_its_renderer() -> (
    None
):
    """CR-20260918T175410Z-726dee2-f55d6151-F01: a qualifying secret equal to
    the ``as_curl()`` renderer's own fixed label must not survive in the
    raised exception's ``renderer`` attribute."""
    request = _make_request(
        headers={
            "authorization": "Bearer plaincredential",
            "x-secret-boundary": "'\"$",
            "x-secret-renderer": "as_curl()",
        },
        redact_headers=frozenset(
            {"authorization", "x-secret-boundary", "x-secret-renderer"}
        ),
        min_redacted_value_length=1,
    )
    with pytest.raises(DiagnosticRenderError) as exc_info:
        request.as_curl()
    _assert_exception_state_leaks_nothing(exc_info.value, "as_curl()")


def test_marker_source_label_cannot_leave_a_raw_control_character_in_a_key() -> None:
    """A marker's source label is request-controlled text and can carry a
    raw control character; the marker that embeds it is inserted by
    ``_scrub_and_escape``'s *second* scrub pass, which runs after escaping
    and applies no escape pass of its own afterward. Building the marker
    from the label's raw spelling therefore left that one control character
    sitting unescaped in the final snapshot field it landed in -- a plain
    escape-stage violation in its own right, not only a route to a second
    secret (CR-20260918T140432Z-726dee2-acfd8932, follow-up disposition on
    edc5e13e-F03). Escaping the label before it ever enters the marker
    template closes this regardless of what runs afterward."""
    raw_a = "\x01"
    credential1 = "\\x01"
    raw_b = "\x02"
    credential2 = raw_b + "yyyyyyyy"
    request = _make_request(
        variables={f"k{raw_a}ey": "irrelevant"},
        redact_variables=(),
        headers={f"h{raw_b}name": credential1, "other": credential2},
        redact_headers=frozenset({f"h{raw_b}name", "other"}),
        min_redacted_value_length=1,
    )
    snapshot = request.redacted()
    assert not any(raw_b in key for key in snapshot.variables)
    assert credential2 not in _snapshot_text(request)
