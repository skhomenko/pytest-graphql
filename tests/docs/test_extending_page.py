"""The report section on the Extending page is what the hook adds.

The hook examples that can pass run as tests in ``test_examples.py``. The
``pytest_graphql_report_section`` example is a test that fails on purpose,
because the hook runs only for a failed test, so it is checked here: the page
shows the section that the real run prints. ``page_runs`` explains the rules.
"""

from __future__ import annotations

import pytest

from tests.docs.blocks import ROOT
from tests.docs.page_runs import (
    Run,
    assert_shown_lines_are_real,
    run_project,
    runs,
)

PAGE = ROOT / "docs" / "extending.md"


def test_the_page_shows_the_report_section_of_the_hook() -> None:
    found = runs(PAGE)
    assert len(found) == 1
    assert "GraphQL report: " in found[0].shown[0]


@pytest.mark.parametrize("run", runs(PAGE), ids=lambda run: run.where)
def test_the_section_on_the_page_is_what_the_hook_adds(
    run: Run, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_shown_lines_are_real(run, run_project(pytester, monkeypatch, run))
