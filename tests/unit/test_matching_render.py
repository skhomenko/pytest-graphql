"""The matcher diff renderer (SPEC 7.5 "Matcher diff", DESIGN_DECISIONS section 7).

The golden test reproduces the SPEC 7.5 block character for character. The
rest pin what the SPEC leaves open: wording at a count of one, a missing field,
and, most of all, that nothing the renderer prints skips the diagnostics scrub.
"""

from __future__ import annotations

from typing import Any

import pytest
from graphql import build_schema, parse

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.errors import DiagnosticRenderError
from pytest_graphql._core.matching import (
    ExpectNamespace,
    RenderOptions,
    contains,
    gt,
    length,
    matches,
    unordered,
)
from pytest_graphql._core.response import build_response
from pytest_graphql._core.transport.base import RawResponse
from tests.unit.matching_support import (
    SCHEMA,
    SECRET,
    order,
    orders_nodes,
    request_for,
    respond,
    user_node,
)

expect = ExpectNamespace(SCHEMA)

SPEC_7_5_DIFF = """\
  User does not match (2 of 5 compared fields differ; 34 response fields ignored)
    first_name        'Jhon'  !=  'John'
    orders[0].total   100     !=  150
  matched: id, email, status"""

EXTRA_FIELDS = 32


def wide_user_node() -> Any:
    """A user with 37 fields and one order with 3, so 34 are ignored."""
    extras = " ".join(f"extra{i}: String" for i in range(EXTRA_FIELDS))
    schema = build_schema(
        f"""
        type Order {{ id: ID! total: Int! status: String }}
        type User {{
          id: ID! email: String status: String first_name: String
          orders: [Order!]! {extras}
        }}
        type Query {{ user: User }}
        """
    )
    selection = "id email status first_name orders { id total status } " + " ".join(
        f"extra{i}" for i in range(EXTRA_FIELDS)
    )
    query = "{ user { " + selection + " } }"
    data: dict[str, Any] = {
        "id": "123",
        "email": "john@example.test",
        "status": "ACTIVE",
        "first_name": "Jhon",
        "orders": [{"id": "o1", "total": 100, "status": "PAID"}],
    }
    data.update({f"extra{i}": f"x{i}" for i in range(EXTRA_FIELDS)})
    raw = RawResponse(
        status_code=200,
        media_type="application/json",
        data={"user": data},
        errors=(),
        extensions=None,
        headers={},
    )
    response = build_response(
        raw, request=request_for(query), schema=schema, document=parse(query)
    )
    node = response.data.user
    assert len(node) == 5 + EXTRA_FIELDS
    return node, schema


def test_the_diff_reproduces_spec_7_5_exactly() -> None:
    node, schema = wide_user_node()
    wide = ExpectNamespace(schema)
    matcher = wide.User(
        id="123",
        email="john@example.test",
        status="ACTIVE",
        first_name="John",
        orders=[{"total": 150}],
    )
    lines = matcher.explain(node)
    assert "\n".join(lines) == SPEC_7_5_DIFF


def test_the_counts_come_from_the_evaluation() -> None:
    node, schema = wide_user_node()
    result = (
        ExpectNamespace(schema)
        .User(id="123", first_name="John", orders=[{"total": 150}])
        .evaluate(node)
    )
    assert (result.compared, result.differ, result.ignored) == (3, 2, 36)
    assert [m.path for m in result.mismatches] == [
        ("first_name",),
        ("orders", 0, "total"),
    ]


def test_a_passing_match_renders_nothing() -> None:
    assert expect.User(first_name="John").explain(user_node()) == []


def test_counts_of_one_read_as_singular() -> None:
    lines = expect.User(first_name="Jhon").explain(user_node())
    assert lines[0].startswith("  User does not match (1 of 1 compared field differs;")
    assert "response fields ignored)" in lines[0]
    single = expect.Settings(theme="x").explain({"theme": "dark"})
    assert single[0] == (
        "  Settings does not match "
        "(1 of 1 compared field differs; 0 response fields ignored)"
    )
    one_ignored = expect.Settings(theme="x").explain({"theme": "dark", "locale": "en"})
    assert "1 response field ignored)" in one_ignored[0]


def test_a_missing_field_is_shown_as_missing() -> None:
    node = respond("{ orders { id } }", {"orders": [{"id": "a"}]}).data.orders[0]
    lines = expect.Order(note="x", id="a").explain(node)
    assert lines == [
        "  Order does not match "
        "(1 of 2 compared fields differ; 0 response fields ignored)",
        "    note   <missing>  !=  'x'",
        "  matched: id",
    ]


def test_a_type_name_mismatch_is_a_compared_field() -> None:
    lines = expect.Order(id="u1").explain(user_node())
    assert lines == [
        "  Order does not match "
        "(1 of 1 compared field differs; 0 response fields ignored)",
        "    __typename   'User'  !=  'Order'",
    ]


def test_a_helper_is_described_not_dumped() -> None:
    lines = expect.User(first_name=gt(5)).explain(user_node())
    assert lines[1].split() == ["first_name", "'John'", "!=", "gt(5)"]


def test_a_composite_actual_is_summarised() -> None:
    node = user_node(orders=[order(), order(id="o2")])
    lines = expect.User(orders=length(3)).explain(node)
    row = next(line for line in lines if "orders" in line)
    assert "[2 items]" in row
    lines = expect.User(settings="flat").explain(node)
    row = next(line for line in lines if "settings" in line)
    assert "Settings(theme, locale)" in row


def test_contains_and_unordered_failures_name_the_unmatched_items() -> None:
    lines = expect.User(orders=contains({"total": 999}, {"total": 100})).explain(
        user_node()
    )
    text = "\n".join(lines)
    assert "contains(2 items)" in text
    assert "no element left for item 0" in text
    assert "999" in text

    lines = expect.User(orders=unordered({"total": 100})).explain(
        user_node(orders=[order(), order(id="o2")])
    )
    assert any("expected 1 element, got 2" in line for line in lines)


# -- the diagnostics rules ---------------------------------------------------


def test_a_redacted_path_prints_a_marker_on_both_sides() -> None:
    node = user_node(password="stored-value-123")
    lines = expect.User(password="expected-value-9").explain(node)
    row = next(line for line in lines if "password" in line)
    assert row.split() == ["password", "[redacted]", "!=", "[redacted]"]
    text = "\n".join(lines)
    assert "stored-value-123" not in text
    assert "expected-value-9" not in text


def test_a_redacted_parent_covers_everything_beneath_it() -> None:
    options = RenderOptions(redact_paths=("settings",))
    lines = expect.User(settings={"theme": "light"}).explain(user_node(), options)
    row = next(line for line in lines if "settings.theme" in line)
    assert row.split() == ["settings.theme", "[redacted]", "!=", "[redacted]"]


def test_path_patterns_apply_to_snake_names_and_wildcards() -> None:
    options = RenderOptions(redact_paths=("*_name",))
    lines = expect.User(first_name="Jhon").explain(user_node(), options)
    assert "[redacted]" in lines[1]
    assert "John" not in "\n".join(lines)


def test_every_value_goes_through_the_scrub() -> None:
    request = request_for("{ user { id } }")
    options = RenderOptions.for_request(request)
    node = user_node(firstName=f"x{SECRET}y")
    lines = expect.User(first_name="Jhon").explain(node, options)
    assert SECRET not in "\n".join(lines)
    assert "[redacted" in "\n".join(lines)


def test_the_expected_side_is_scrubbed_too() -> None:
    options = RenderOptions.for_request(request_for("{ user { id } }"))
    lines = expect.User(first_name=SECRET).explain(user_node(), options)
    assert SECRET not in "\n".join(lines)


def test_a_helper_description_is_scrubbed() -> None:
    options = RenderOptions.for_request(request_for("{ user { id } }"))
    matcher = expect.User(first_name=matches(f"^{SECRET}$"))
    assert SECRET not in "\n".join(matcher.explain(user_node(), options))


def test_a_secret_with_quotes_survives_neither_repr_nor_scrub_order() -> None:
    secret = "a\"b'c\\d-secret-value"
    request = RequestInfo(
        operation=None,
        kind="query",
        document="{ x }",
        variables={},
        headers={"Authorization": f"Bearer {secret}"},
        url="http://example.test/graphql",
    )
    options = RenderOptions.for_request(request)
    lines = expect.User(first_name="Jhon").explain(user_node(firstName=secret), options)
    text = "\n".join(lines)
    assert secret not in text
    assert repr(secret) not in text
    assert repr(secret)[1:-1] not in text


def test_control_characters_are_escaped() -> None:
    lines = expect.User(first_name="x").explain(user_node(firstName="a\x1b[31mred"))
    text = "\n".join(lines)
    assert "\x1b" not in text
    assert "\\x1b" in text


def test_a_hidden_character_is_escaped() -> None:
    hidden = chr(0x200B)
    value = "a" + hidden + "b"
    lines = expect.User(first_name="x").explain(user_node(firstName=value))
    assert hidden not in "\n".join(lines)
    assert "\\u200b" in "\n".join(lines)


def test_a_value_is_cut_at_the_byte_limit_and_says_so() -> None:
    options = RenderOptions(max_value_bytes=20)
    lines = expect.User(first_name="x").explain(user_node(firstName="y" * 500), options)
    row = lines[1]
    assert "y" * 21 not in row
    assert "bytes cut" in row


def test_a_cut_cannot_split_a_multibyte_character() -> None:
    options = RenderOptions(max_value_bytes=5)
    lines = expect.User(first_name="x").explain(user_node(firstName="é" * 50), options)
    lines[1].encode("utf-8")  # raises on a broken character


def test_the_snapshot_check_fails_closed_when_the_scrub_misses() -> None:
    request = request_for("{ user { id } }")
    options = RenderOptions(snapshot=request.redacted(), scrub=lambda text: text)
    with pytest.raises(DiagnosticRenderError):
        expect.User(first_name="Jhon").explain(user_node(firstName=SECRET), options)


def test_the_snapshot_check_passes_clean_output() -> None:
    request = request_for("{ user { id } }")
    options = RenderOptions.for_request(request)
    assert expect.User(first_name="Jhon").explain(user_node(), options)


def test_the_listing_is_bounded_and_the_counts_stay_exact() -> None:
    rows = [order(id=f"o{i}", total=i) for i in range(200)]
    options = RenderOptions(max_lines=10)
    lines = expect.User(orders=[expect.Order(total=-1)] * 200).explain(
        user_node(orders=rows), options
    )
    assert len(lines) <= 12
    assert "200 of 200 compared fields differ" in lines[0]
    assert lines[-1] == "    ... 190 more differences not shown"


def test_the_matched_list_is_bounded_and_says_how_many_it_left_out() -> None:
    node = orders_nodes(order())[0]
    matcher = expect.Order(id="o1", total=100, status="PAID", note="x")
    lines = matcher.explain(node, RenderOptions(max_matched=2))
    assert lines[-1] == "  matched: id, total, ... 1 more"
    assert "(1 of 4 compared fields differ;" in lines[0]


def test_the_output_has_no_dash_punctuation_and_no_hidden_characters() -> None:
    lines = expect.User(first_name="Jhon", orders=contains({"total": 9})).explain(
        user_node()
    )
    text = "\n".join(lines)
    assert chr(0x2014) not in text
    assert chr(0x2013) not in text
    assert all(ch.isprintable() or ch == "\n" for ch in text)


def test_the_rendering_is_stable_across_runs() -> None:
    first = expect.User(first_name="Jhon", last_name="Smith").explain(user_node())
    second = expect.User(first_name="Jhon", last_name="Smith").explain(user_node())
    assert first == second


def test_unordered_names_the_unmatched_items_and_the_spare_elements() -> None:
    result = unordered(1, 2).evaluate([1, 3])
    (mismatch,) = result.mismatches
    assert mismatch.detail_text(lambda value: repr(value)) == [
        "no element left for item 1: 2",
        "no item left for element 1: 3",
    ]


def test_a_non_string_value_is_scrubbed_through_its_repr() -> None:
    class Leaky:
        def __repr__(self) -> str:
            return f"Leaky({SECRET})"

    options = RenderOptions.for_request(request_for("{ user { id } }"))
    lines = expect.User(first_name=Leaky()).explain(user_node(), options)
    assert "Leaky(" in "\n".join(lines)
    assert SECRET not in "\n".join(lines)


def test_a_field_label_is_scrubbed() -> None:
    options = RenderOptions.for_request(request_for("{ user { id } }"))
    matcher = expect.User(settings={SECRET: "x"})
    lines = matcher.explain(user_node(), options)
    assert any(line.lstrip().startswith("settings.") for line in lines)
    assert SECRET not in "\n".join(lines)
