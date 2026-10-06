"""The reports on the Diagnostics page are what pytest prints.

``page_runs`` explains how the page's commands and output blocks are checked.
The page promises that a hidden value is not shown, so the checks that follow
look for the placeholder values of the page in the real output.
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

PAGE = ROOT / "docs" / "diagnostics.md"

#: The values of the page that stand for secrets.
PLACEHOLDERS = ("token-PLACEHOLDER", "session-PLACEHOLDER", "secret-PLACEHOLDER")


def test_the_page_shows_each_report_of_a_command() -> None:
    found = runs(PAGE)
    assert len(found) >= 5, "the page lost a command, so this test checks less"
    assert all(run.shown for run in found)


@pytest.mark.parametrize("run", runs(PAGE), ids=lambda run: run.where)
def test_the_output_on_the_page_is_what_the_command_prints(
    run: Run, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_shown_lines_are_real(run, run_project(pytester, monkeypatch, run))


def _calls_section(output: list[str]) -> list[str]:
    """The lines of the ``GraphQL calls`` section, and the exception lines."""
    section: list[str] = []
    inside = False
    for line in output:
        if "GraphQL calls" in line:
            inside = True
        elif inside and line.startswith("====") and "GraphQL calls" not in line:
            inside = False
        if inside or line.startswith("E "):
            section.append(line)
    return section


def test_the_first_report_hides_the_token_and_shows_the_names_that_are_not_listed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = next(run for run in runs(PAGE) if "[1] query user" in run.shown[1])
    section = "\n".join(_calls_section(run_project(pytester, monkeypatch, run)))
    assert "token-PLACEHOLDER" not in section
    assert "${PYTEST_GQL_HEADER_AUTHORIZATION}" in section
    # Neither is secret by default, which is the point of the page's next section.
    assert "session-PLACEHOLDER" in section
    assert "secret-PLACEHOLDER" in section


def test_the_second_report_hides_every_placeholder(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [run for run in runs(PAGE) if "[redacted:id]" in "\n".join(run.shown)]
    assert len(found) == 1
    section = "\n".join(_calls_section(run_project(pytester, monkeypatch, found[0])))
    assert section, "the run printed no GraphQL calls section"
    page_text = "\n".join(found[0].shown)
    for placeholder in PLACEHOLDERS:
        assert placeholder not in section, placeholder
        assert placeholder not in page_text, placeholder


def test_no_report_on_the_page_shows_the_bearer_token() -> None:
    for run in runs(PAGE):
        assert "token-PLACEHOLDER" not in "\n".join(run.shown), run.where
