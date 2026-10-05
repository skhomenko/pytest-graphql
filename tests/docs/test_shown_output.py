"""Output that a page quotes in prose is what pytest prints.

The Configuration page quotes its two messages in ``text`` blocks, and
``test_configuration_table.py`` checks those. A page can also quote a line of
the report in a sentence. These tests run the real session and look for the
same words in the page, so a change to the report header fails here.
"""

from __future__ import annotations

import re

import pytest

from tests.docs.blocks import ROOT
from tests.unit.plugin_inner import run_inner

SEED_HEADER = re.compile(r"seed (\d+) \(chosen by random\)")


def test_the_factory_page_quotes_the_header_line_of_a_random_seed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(
        pytester,
        monkeypatch,
        test="def test_a(gql):\n    pass\n",
        args=("--gql-seed=random",),
    )
    shown = [line for line in result.stdout.lines if SEED_HEADER.search(line)]
    assert len(shown) == 1, result.stdout.str()

    page = (ROOT / "docs" / "factory.md").read_text(encoding="utf-8")
    assert SEED_HEADER.search(page), "the page no longer quotes the random seed header"
