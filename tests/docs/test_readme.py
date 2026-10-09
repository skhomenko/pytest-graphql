"""The README is the page PyPI shows, and it must let a reader decide and start.

It leads with the position statement, says when to use the package and when not
to, holds the Quickstart and one fuller example, and ends with links. It is also
the long description on PyPI, which cannot follow a relative link, so every link
in it is absolute. ``docs/index.md`` holds the same Quickstart, and the two must
not drift apart. "Agent and index discoverability" in
``docs/reference/DESIGN_DECISIONS.md`` states the order.
"""

from __future__ import annotations

import re

import yaml

from tests.docs.blocks import ROOT, blocks_of
from tests.docs.test_site import API_PAGE, POSITION, SPEC_PAGES

README = ROOT / "README.md"
LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")

#: The sections in the order the design gives them. "More documentation" stays
#: after the feature list, where it was.
SECTIONS = [
    "Use it when",
    "Do not use it when",
    "Quickstart",
    "A fuller example",
    "API at a glance",
    "Compared with other tools",
    "Features",
    "More documentation",
    "Install",
    "Supported versions",
    "Status",
    "License",
]

#: What the "API at a glance" table must cover.
GLANCE = [
    "`gql`",
    "gql.query(",
    "gql.mutation(",
    "gql.execute(",
    "fields=",
    "Selection",
    "gql.expect.",
    "gql.expect_error(",
    "gql.fake.",
    "unique()",
    "gql.as_(",
    "gql.with_headers(",
    "gql.wait_until(",
    "build_client(",
]


def _site_url() -> str:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    return str(config["site_url"])


def _section(name: str) -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, name
    return match[1]


def _bullets(name: str) -> list[str]:
    return [line for line in _section(name).splitlines() if line.startswith("- ")]


def _table(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


def test_the_readme_sections_follow_the_design_order() -> None:
    text = README.read_text(encoding="utf-8")
    assert re.findall(r"^## (.+)$", text, re.M) == SECTIONS


def test_the_readme_leads_with_the_position_statement() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["# pytest-graphql", "", POSITION]


def test_the_readme_says_when_to_use_the_package_and_when_not_to() -> None:
    assert 3 <= len(_bullets("Use it when")) <= 5
    avoid = _bullets("Do not use it when")
    assert 3 <= len(avoid) <= 5
    text = " ".join(avoid)
    for topic in ("REST", "production client", "mock", "fuzz"):
        assert topic in text, topic


def test_the_readme_quickstart_is_the_quickstart_of_the_site() -> None:
    readme = [(b.language, b.mode, b.source) for b in blocks_of(README)]
    site = [
        (b.language, b.mode, b.source) for b in blocks_of(ROOT / "docs" / "index.md")
    ]
    assert site, "the site Quickstart has no blocks"
    # The Quickstart blocks come in one run, in the order of the site page.
    start = readme.index(site[0])
    assert readme[start : start + len(site)] == site
    assert start == 0, "no block comes before the Quickstart"


def test_the_readme_shows_the_quickstart_and_one_fuller_example_and_no_more() -> None:
    python = [block for block in blocks_of(README) if block.is_python]
    assert [block.mode for block in python] == ["exec", "exec"]
    site = [b for b in blocks_of(ROOT / "docs" / "index.md") if b.is_python]
    assert python[0].source == site[0].source


def test_the_fuller_example_is_short_and_shows_the_main_calls() -> None:
    example = next(
        block
        for block in blocks_of(README)
        if block.is_python and "fuller" in _heading_before(block.source)
    ).source
    assert len(example.strip().splitlines()) <= 30
    for call in (
        "gql.query(",
        "gql.mutation(",
        "gql.fake.",
        "gql.expect.",
        "gql.expect_error(",
        "fields=",
    ):
        assert call in example, call


def _heading_before(source: str) -> str:
    text = README.read_text(encoding="utf-8")
    head = text[: text.index(source)]
    return re.findall(r"^## (.+)$", head, re.M)[-1].lower()


def test_the_glance_table_covers_the_public_calls_a_test_uses() -> None:
    table = "\n".join(_table(_section("API at a glance")))
    missing = [call for call in GLANCE if call not in table]
    assert not missing, missing


def test_the_readme_comparison_is_the_comparison_of_the_why_page() -> None:
    site_url = _site_url()
    why = (ROOT / "docs" / "why.md").read_text(encoding="utf-8")
    section = re.search(
        r"^## Compared with other approaches\n(.*?)^## ", why, re.M | re.S
    )
    assert section
    expected = [
        re.sub(r"\]\(([a-z-]+)\.md\)", lambda m: f"]({site_url}{m[1]}/)", line)
        for line in _table(section[1])
    ]
    assert len(expected) == 2 + 4, "a header, a rule and four approaches"
    assert _table(_section("Compared with other tools")) == expected


def test_the_readme_status_still_names_the_features_that_are_not_released() -> None:
    status = " ".join(_section("Status").split())
    assert "not part of `0.1.x`" in status
    for feature in ("subscriptions", "file uploads", "async clients"):
        assert feature in status, feature


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
