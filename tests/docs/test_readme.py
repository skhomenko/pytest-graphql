"""The README is the Quickstart, a feature list and links, and nothing more.

It is also the long description on PyPI, which cannot follow a relative link,
so every link in it is absolute. ``docs/index.md`` holds the same Quickstart, and
the two must not drift apart.
"""

from __future__ import annotations

import re

import yaml

from tests.docs.blocks import ROOT, blocks_of
from tests.docs.test_site import API_PAGE, SPEC_PAGES

README = ROOT / "README.md"
LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")


def _site_url() -> str:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    return str(config["site_url"])


def test_the_readme_quickstart_is_the_quickstart_of_the_site() -> None:
    readme = [(b.language, b.mode, b.source) for b in blocks_of(README)]
    site = [
        (b.language, b.mode, b.source) for b in blocks_of(ROOT / "docs" / "index.md")
    ]
    assert site, "the site Quickstart has no blocks"
    # The README leads with the Quickstart blocks, then the install blocks.
    assert readme[: len(site)] == site


def test_the_readme_shows_one_python_example_and_the_guides_hold_the_rest() -> None:
    python = [block for block in blocks_of(README) if block.is_python]
    assert len(python) == 1
    assert python[0].mode == "exec"


def test_every_link_in_the_readme_is_absolute() -> None:
    targets = [m["target"] for m in LINK.finditer(README.read_text(encoding="utf-8"))]
    assert targets
    relative = [t for t in targets if not t.startswith("https://")]
    assert not relative, relative


def test_the_readme_links_every_page_of_the_site() -> None:
    site_url = _site_url()
    assert site_url.endswith("/")
    text = README.read_text(encoding="utf-8")
    targets = {m["target"] for m in LINK.finditer(text)}
    pages = [name for name, _ in [*SPEC_PAGES, API_PAGE] if name != "index.md"]
    missing = [
        name
        for name in pages
        if f"{site_url}{name.removesuffix('.md')}/" not in targets
    ]
    assert not missing, missing
    assert site_url in text, "the README never links the site itself"


def test_the_readme_links_only_the_site_and_the_repository() -> None:
    site_url = _site_url()
    text = README.read_text(encoding="utf-8")
    for match in LINK.finditer(text):
        target = match["target"]
        assert target.startswith(
            (site_url, "https://github.com/skhomenko/pytest-graphql/")
        ), target
