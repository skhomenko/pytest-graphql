"""Every Python block in the published pages and the README is checked.

One test per Python block, plus one per block with a lint problem, so a failure
names the page and the line. ``DESIGN_DECISIONS.md`` section 10 is the rule.
"""

from __future__ import annotations

import pytest

from tests.docs.blocks import ROOT, Block, blocks_of, page_paths
from tests.docs.runner import check

BLOCKS = [
    block
    for page in page_paths()
    for block in blocks_of(page)
    if block.is_python or block.problems
]


@pytest.mark.parametrize("block", BLOCKS, ids=lambda block: block.where)
def test_the_block_passes_its_check(
    block: Block, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    check(block, pytester, monkeypatch)


def test_the_checked_pages_are_found() -> None:
    """A discovery that finds nothing would pass every test above."""
    relative = {page.relative_to(ROOT).as_posix() for page in page_paths()}
    assert "README.md" in relative
    assert "docs/index.md" in relative
    assert not any(path.startswith("docs/reference/") for path in relative)
    assert any(block.mode == "exec" for block in BLOCKS)
