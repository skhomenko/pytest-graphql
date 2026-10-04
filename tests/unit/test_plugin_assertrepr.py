"""``pytest_assertrepr_compare`` and the matcher diff (SPEC 7.5, DESIGN section 5).

A failed ``actual == matcher`` shows the M6 diff. pytest indents every line after
the first by two spaces and the diff carries those two already, so the hook
drops them and the block reads as SPEC 7.5 prints it. The first line names the
two sides without showing a value that a diff does not.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from pytest_graphql._core.diagnostics import WITHHELD_TEXT
from pytest_graphql._core.errors import DiagnosticRenderError
from pytest_graphql._core.matching import ExpectNamespace, Matcher, contains
from pytest_graphql.plugin import pytest_assertrepr_compare
from tests.unit.m9b_support import reports, run_scripted
from tests.unit.matching_support import SCHEMA, user_node
from tests.unit.test_matching_render import wide_user_node

SPEC_7_5_BLOCK = [
    "User does not match (2 of 5 compared fields differ; 34 response fields ignored)",
    "  first_name        'Jhon'  !=  'John'",
    "  orders[0].total   100     !=  150",
    "matched: id, email, status",
]


def wide_case() -> tuple[Any, Matcher]:
    node, schema = wide_user_node()
    matcher = ExpectNamespace(schema).User(
        id="123",
        email="john@example.test",
        status="ACTIVE",
        first_name="John",
        orders=[{"total": 150}],
    )
    return node, matcher


def test_the_hook_returns_the_spec_7_5_block_after_one_summary_line(
    pytestconfig: pytest.Config,
) -> None:
    node, matcher = wide_case()
    lines = pytest_assertrepr_compare(pytestconfig, "==", node, matcher)
    assert lines is not None
    assert lines[1:] == SPEC_7_5_BLOCK


def test_the_summary_names_both_sides_by_their_repr(
    pytestconfig: pytest.Config,
) -> None:
    node, matcher = wide_case()
    lines = pytest_assertrepr_compare(pytestconfig, "==", node, matcher)
    assert lines is not None
    assert lines[0] == f"{node!r} == {matcher!r}"


def test_the_matcher_may_be_on_the_left(pytestconfig: pytest.Config) -> None:
    node, matcher = wide_case()
    lines = pytest_assertrepr_compare(pytestconfig, "==", matcher, node)
    assert lines is not None
    assert lines[0] == f"{matcher!r} == {node!r}"
    assert lines[1:] == SPEC_7_5_BLOCK


def test_the_summary_of_a_plain_dict_shows_a_count_and_no_value(
    pytestconfig: pytest.Config,
) -> None:
    matcher = ExpectNamespace(SCHEMA).User(first_name="Jon")
    actual = {"firstName": "John", "password": "p4ss-w0rd-value"}
    lines = pytest_assertrepr_compare(pytestconfig, "==", actual, matcher)
    assert lines is not None
    assert lines[0] == f"{{2 fields}} == {matcher!r}"
    assert "p4ss-w0rd-value" not in "\n".join(lines[:1])


@pytest.mark.parametrize("op", ["!=", "<", "in", "is"])
def test_only_equality_is_explained(pytestconfig: pytest.Config, op: str) -> None:
    node, matcher = wide_case()
    assert pytest_assertrepr_compare(pytestconfig, op, node, matcher) is None


def test_a_comparison_with_no_matcher_is_left_to_pytest(
    pytestconfig: pytest.Config,
) -> None:
    assert pytest_assertrepr_compare(pytestconfig, "==", {"a": 1}, {"a": 2}) is None
    assert pytest_assertrepr_compare(pytestconfig, "==", "x", "y") is None


def test_two_matchers_are_left_to_pytest(pytestconfig: pytest.Config) -> None:
    first = ExpectNamespace(SCHEMA).User(first_name="a")
    second = ExpectNamespace(SCHEMA).User(first_name="b")
    assert pytest_assertrepr_compare(pytestconfig, "==", first, second) is None


def test_a_value_that_matches_has_nothing_to_explain(
    pytestconfig: pytest.Config,
) -> None:
    matcher = ExpectNamespace(SCHEMA).User(first_name="John")
    assert pytest_assertrepr_compare(pytestconfig, "==", user_node(), matcher) is None


def test_a_list_matcher_is_explained_too(pytestconfig: pytest.Config) -> None:
    lines = pytest_assertrepr_compare(pytestconfig, "==", [1, 2, 3], contains(9))
    assert lines == [
        "[3 items] == contains(1 item)",
        "contains(1 item) does not match "
        "(1 of 1 compared field differs; 0 response fields ignored)",
        "  value   [3 items]  !=  contains(1 item)",
        "    no element left for item 0: 9",
    ]


def test_a_render_refusal_shows_the_withheld_notice(
    pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Matcher, actual: object, options: object = None) -> list[str]:  # noqa: ARG001
        raise DiagnosticRenderError("matcher diff", "refused")

    monkeypatch.setattr(Matcher, "explain", refuse)
    node, matcher = wide_case()
    assert pytest_assertrepr_compare(pytestconfig, "==", node, matcher) == [
        WITHHELD_TEXT
    ]


def test_any_other_fault_falls_back_to_pytests_own_report(
    pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: Matcher, actual: object, options: object = None) -> list[str]:  # noqa: ARG001
        raise RuntimeError("a bug")

    monkeypatch.setattr(Matcher, "explain", fail)
    node, matcher = wide_case()
    assert pytest_assertrepr_compare(pytestconfig, "==", node, matcher) is None


# -- in a real session ----------------------------------------------------------

FAILING_ASSERT = """
def test_fails(gql):
    response = gql.execute(USER_QUERY, {"id": "123"})
    assert response.data.user == gql.expect.User(name="Jon")
"""


def test_a_failed_assert_shows_the_diff_in_the_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, FAILING_ASSERT)
    text = reports("test_fails", "call")[0][5]
    lines = [line.removeprefix("E").rstrip() for line in text.splitlines()]
    title = next(
        line
        for line in lines
        if "User does not match (1 of 1 compared field differs; 1 response field"
        in line
    )
    indent = len(title) - len(title.lstrip())
    row = lines[lines.index(title) + 1]
    assert row.strip() == "name   'John'  !=  'Jon'"
    assert len(row) - len(row.lstrip()) == indent + 2
    assert "matched:" not in text


def test_the_assert_line_names_the_sides_without_a_value(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(pytester, monkeypatch, FAILING_ASSERT)
    text = reports("test_fails", "call")[0][5]
    assert re.search(r"assert User\(id, name\) == User\(name=<str>\)", text)


def test_the_diff_shows_the_spec_block_through_pytests_indent(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_scripted(
        pytester,
        monkeypatch,
        """
def test_fails(gql):
    response = gql.execute(USER_QUERY, {"id": "123"})
    assert response.data.user == gql.expect.User(id="123", name="Jon")
""",
    )
    text = reports("test_fails", "call")[0][5]
    lines = [line.removeprefix("E").rstrip() for line in text.splitlines()]
    start = next(i for i, line in enumerate(lines) if "User does not match" in line)
    block = lines[start : start + 3]
    base = len(block[0]) - len(block[0].lstrip())
    relative = [line[base:] for line in block]
    assert relative == [
        "User does not match "
        "(1 of 2 compared fields differ; 0 response fields ignored)",
        "  name   'John'  !=  'Jon'",
        "matched: id",
    ]
