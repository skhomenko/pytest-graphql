"""The built site: pages, exclusions, third-party loads and the API reference.

The build runs ``mkdocs build --strict`` in a subprocess, once per session, into
a temporary directory. A subprocess keeps MkDocs' logging and strict mode out of
this process. Nothing here needs the network, because the site loads nothing
from a third party and ``mkdocstrings`` reads the source statically.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import mkdocs_hooks
import pytest
from mkdocs.config import load_config
from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File, Files
from packaging.requirements import Requirement

import pytest_graphql
from tests.docs.blocks import ROOT, blocks_of

#: The fifteen pages of SPEC section 13 in its order, with the "Why" page after
#: the Quickstart: file, then nav title. ``index.md`` is the Quickstart, so the
#: site root is its first page.
SPEC_PAGES = (
    ("index.md", "Quickstart"),
    ("why.md", "Why pytest-graphql?"),
    ("configuration.md", "Configuration"),
    ("selections.md", "Selections"),
    ("responses.md", "Responses"),
    ("assertions.md", "Assertions"),
    ("errors.md", "Errors"),
    ("factory.md", "Factory"),
    ("authentication.md", "Authentication"),
    ("polling.md", "Polling"),
    ("diagnostics.md", "Diagnostics"),
    ("extending.md", "Extending"),
    ("without-pytest.md", "Using without pytest"),
    ("cookbook.md", "Cookbook"),
    ("migrating.md", "Migrating from a hand-rolled client"),
    ("faq.md", "FAQ"),
)
API_PAGE = ("api.md", "API reference")

#: Names in ``__all__`` that ``mkdocstrings`` cannot reach through the package,
#: with the object path ``docs/api.md`` renders them from. ``unique`` is both a
#: function and the name of the private module that defines it, and static
#: analysis resolves ``pytest_graphql.unique`` to the module.
RENDERED_FROM = {"unique": "pytest_graphql._core.factory.unique.unique"}

#: Rels that make a browser fetch something. A canonical or alternate link only
#: names a page, so it is not a load.
LOADING_RELS = frozenset(
    {
        "stylesheet",
        "preload",
        "prefetch",
        "modulepreload",
        "preconnect",
        "dns-prefetch",
        "icon",
        "shortcut icon",
        "apple-touch-icon",
        "manifest",
    }
)


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    # The notice is about MkDocs 2, which the docs extra's upper bound keeps
    # out. It adds noise to every build and changes no result.
    env = {**os.environ, "NO_MKDOCS_2_WARNING": "true"}
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(out)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def _nav_pages(items: list[object]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in items:
        assert isinstance(item, dict)
        for title, target in item.items():
            assert isinstance(target, str), "the nav is flat"
            found.append((target, title))
    return found


def test_the_nav_is_the_spec_pages_and_the_why_page_then_the_api_reference() -> None:
    config = load_config(str(ROOT / "mkdocs.yml"))
    assert _nav_pages(config.nav) == [*SPEC_PAGES, API_PAGE]


def test_every_published_page_is_in_the_nav_and_has_its_title() -> None:
    pages = {
        path.relative_to(ROOT / "docs").as_posix(): path
        for path in (ROOT / "docs").rglob("*.md")
        if path.relative_to(ROOT / "docs").parts[0] != "reference"
    }
    assert sorted(pages) == sorted(name for name, _ in [*SPEC_PAGES, API_PAGE])
    for name, title in [*SPEC_PAGES, API_PAGE]:
        first = pages[name].read_text(encoding="utf-8").splitlines()[0]
        assert first == f"# {title}", name


#: Every page after the Quickstart is a guide page, and it opens by saying what the
#: reader can do after it. A page that is still a one-line stub fails here, so a
#: page cannot be added to the site and left unwritten.
GUIDE_PAGES = tuple(name for name, _ in SPEC_PAGES[1:])


@pytest.mark.parametrize("name", GUIDE_PAGES)
def test_a_guide_page_is_no_stub_and_opens_with_what_the_reader_can_do(
    name: str,
) -> None:
    text = (ROOT / "docs" / name).read_text(encoding="utf-8")
    assert "This page will cover" not in text, name
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    assert paragraphs[0].startswith("# "), name
    opening = paragraphs[1] if not paragraphs[1].startswith("#") else ""
    assert opening.startswith("After this page you can "), name
    assert len(paragraphs) > 3, f"{name} has too little in it to be a guide page"


def test_a_broken_link_or_anchor_fails_the_strict_build() -> None:
    """The guide pages link to headings of the generated API reference.

    MkDocs reports a missing anchor at the level ``info`` unless told
    otherwise, and ``--strict`` fails only on a warning. So the config raises it.
    """
    warning = 30
    validation = load_config(str(ROOT / "mkdocs.yml")).validation
    assert validation["links"]["anchors"] >= warning
    assert validation["links"]["not_found"] >= warning
    assert validation["links"]["unrecognized_links"] >= warning
    assert validation["nav"]["omitted_files"] >= warning


def test_the_build_produces_each_page(site: Path) -> None:
    assert (site / "index.html").is_file()
    for name, title in [*SPEC_PAGES[1:], API_PAGE]:
        html = (site / name.removesuffix(".md") / "index.html").read_text("utf-8")
        assert f">{title}</h1>" in html or f">{title}<" in html, name


def test_the_build_holds_nothing_from_docs_reference(site: Path) -> None:
    reference = sorted((ROOT / "docs" / "reference").glob("*.md"))
    assert reference, "docs/reference/ has no pages, so this test proves nothing"
    assert not (site / "reference").exists()
    # Neither the page files nor the sitemap nor the search index may name one.
    # The search index lists every page the build rendered, by location.
    stems = {path.stem.lower() for path in reference}
    named = [
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if {part.lower() for part in path.relative_to(site).parts} & stems
    ]
    assert not named
    sitemap = (site / "sitemap.xml").read_text("utf-8")
    assert "reference" not in sitemap
    index = json.loads((site / "search" / "search_index.json").read_text("utf-8"))
    locations = {entry["location"] for entry in index["docs"]}
    assert locations
    assert not [loc for loc in locations if loc.lower().startswith("reference")]
    # No page is the rendering of a reference document: its title is not the
    # title of any page.
    titles = {
        path.read_text("utf-8").splitlines()[0].removeprefix("# ") for path in reference
    }
    rendered = {
        re.sub(r"<[^>]+>", "", match)
        for page in site.rglob("*.html")
        for match in re.findall(r"<h1[^>]*>(.*?)</h1>", page.read_text("utf-8"), re.S)
    }
    assert not titles & {title.strip() for title in rendered}


class _Loads(HTMLParser):
    """Every URL in a page that a browser fetches by itself."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link":
            rels = (values.get("rel") or "").lower().split()
            if LOADING_RELS & {*rels, " ".join(rels)} and values.get("href"):
                self.urls.append(values["href"] or "")
        elif tag in {"script", "img", "iframe", "source", "video", "audio", "embed"}:
            if values.get("src"):
                self.urls.append(values["src"] or "")


def _external(url: str) -> bool:
    return bool(re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", url, re.IGNORECASE)) or (
        url.lower().startswith(("http:", "https:", "data:text/html"))
    )


def test_the_site_loads_nothing_from_a_third_party(site: Path) -> None:
    pages = sorted(site.rglob("*.html"))
    assert pages
    for page in pages:
        parser = _Loads()
        parser.feed(page.read_text("utf-8"))
        external = [url for url in parser.urls if _external(url)]
        assert not external, f"{page.relative_to(site)} loads {external}"
    for sheet in sorted(site.rglob("*.css")):
        text = sheet.read_text("utf-8")
        for url in re.findall(r"url\(\s*['\"]?([^'\")\s]+)", text):
            assert not _external(url), f"{sheet.relative_to(site)} loads {url}"
        assert "@import" not in text, sheet.name


#: Every file the build may publish, with whose it is. A file outside this table
#: has no recorded licence basis, so it blocks the deployment until someone adds
#: the basis here and to "Documentation dependencies" in DESIGN_DECISIONS.md. A
#: theme upgrade or a new plugin that copies a vendored file lands here first.
SITE_FILES = {
    r"(?:[a-z0-9-]+/)?index\.html": "a page built from docs/ and the source",
    r"404\.html": "the not-found page the theme renders",
    r"sitemap\.xml(?:\.gz)?": "generated by MkDocs",
    r"objects\.inv": "generated by mkdocstrings",
    r"search/search_index\.json": "generated from the pages",
    r"assets/_mkdocstrings\.css": "mkdocstrings, ISC",
    r"assets/images/favicon\.png": "Material for MkDocs, MIT",
    r"assets/javascripts/bundle\.[0-9a-f]{8}\.min\.js(?:\.map)?": (
        "Material for MkDocs, MIT, bundling the libraries DESIGN_DECISIONS.md lists"
    ),
    r"assets/javascripts/workers/search\.[0-9a-f]{8}\.min\.js(?:\.map)?": (
        "Material for MkDocs, MIT, bundling Lunr, MIT"
    ),
    r"assets/stylesheets/(?:main|palette)\.[0-9a-f]{8}\.min\.css(?:\.map)?": (
        "Material for MkDocs, MIT"
    ),
}

#: Terms whose licences ask a distributor for a notice or a source offer that
#: this site does not carry. A published file that names one is a finding.
COPYLEFT = re.compile(
    r"Mozilla Public|\bMPL\b|GNU (?:General|Lesser|Affero)|\b[AL]?GPL\b|\bSSPL\b"
    r"|creativecommons\.org/licenses/by-(?:sa|nc)",
    re.IGNORECASE,
)


def test_the_site_holds_only_files_with_a_recorded_licence_basis(site: Path) -> None:
    files = sorted(
        path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()
    )
    unknown = [
        name
        for name in files
        if not any(re.fullmatch(pattern, name) for pattern in SITE_FILES)
    ]
    assert not unknown, unknown
    # A pattern that matches nothing is a stale record, not a safe one.
    unused = [
        pattern
        for pattern in SITE_FILES
        if not any(re.fullmatch(pattern, name) for name in files)
    ]
    assert not unused, unused


def test_the_site_holds_no_copyleft_licensed_file(site: Path) -> None:
    scanned = 0
    for path in sorted(site.rglob("*")):
        if path.suffix not in {".js", ".css", ".map", ".json", ".html"}:
            continue
        scanned += 1
        found = COPYLEFT.search(path.read_text("utf-8"))
        assert not found, f"{path.relative_to(site)} names {found and found.group()!r}"
    assert scanned


def test_the_site_publishes_no_lunr_language_pack(site: Path) -> None:
    assert not (site / "assets" / "javascripts" / "lunr").exists()
    index = json.loads((site / "search" / "search_index.json").read_text("utf-8"))
    assert index["config"]["lang"] == ["en"]


def _hook_inputs(languages: list[str] | None) -> tuple[Files, SimpleNamespace]:
    names = [
        "assets/javascripts/bundle.min.js",
        "assets/javascripts/lunr/min/lunr.da.min.js",
        "assets/javascripts/lunr/min/lunr.stemmer.support.min.js",
        "assets/javascripts/lunr/tinyseg.js",
        "index.md",
    ]
    files = Files([File(name, "src", "out", use_directory_urls=True) for name in names])
    search = SimpleNamespace(config=SimpleNamespace(lang=languages))
    return files, SimpleNamespace(plugins={"material/search": search})


def test_the_hook_drops_the_lunr_packs_and_keeps_every_other_file() -> None:
    files, config = _hook_inputs(["en"])
    kept = [file.src_uri for file in mkdocs_hooks.on_files(files, config)]
    assert kept == ["assets/javascripts/bundle.min.js", "index.md"]


@pytest.mark.parametrize("languages", [["de"], ["en", "de"], ["ja"], [], None])
def test_the_hook_refuses_a_search_language_other_than_english(
    languages: list[str] | None,
) -> None:
    files, config = _hook_inputs(languages)
    with pytest.raises(PluginError, match="English search only"):
        mkdocs_hooks.on_files(files, config)
    assert len(list(files)) == 5, "a refused build leaves the files as they were"


#: Text that only makes sense to someone with the contributor documents: the
#: design and review files, the numbered rules in them (B and C numbers), the
#: milestone names, and a Sphinx role that Markdown does not render.
CONTRIBUTOR_ONLY = re.compile(
    r"\bSPEC\b|DESIGN_DECISIONS|CODE_REVIEW_HANDOFF|\bPLAN\.md\b|docs/reference"
    r"|\b[BC]\d{1,3}\b|\bM\d+[a-z]?\b|:(?:mod|class|func|meth|attr|exc|data):"
)


class _Article(HTMLParser):
    """The visible text of a page's main content."""

    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self._depth = 0
        self._hidden = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],  # noqa: ARG002
    ) -> None:
        if tag == "article":
            self._depth += 1
        elif tag in {"script", "style"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._depth -= 1
        elif tag in {"script", "style"}:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if self._depth and not self._hidden:
            self.text.append(data)


def _page_output(source: str) -> str:
    stem = source.removesuffix(".md")
    return "index.html" if stem == "index" else f"{stem}/index.html"


@pytest.mark.parametrize(
    "source",
    [*(name for name, _ in SPEC_PAGES), API_PAGE[0]],
)
def test_a_published_page_cites_no_contributor_document(
    site: Path, source: str
) -> None:
    parser = _Article()
    parser.feed((site / _page_output(source)).read_text("utf-8"))
    text = " ".join("".join(parser.text).split())
    assert text, f"{source} has no article text, so this test proves nothing"
    found = sorted({match.group() for match in CONTRIBUTOR_ONLY.finditer(text)})
    assert not found, found


def test_the_api_page_renders_every_name_in_all(site: Path) -> None:
    html = (site / "api" / "index.html").read_text("utf-8")
    headings = set(re.findall(r'<h2 id="([^"]+)"', html))
    missing = [
        name
        for name in pytest_graphql.__all__
        if RENDERED_FROM.get(name, f"pytest_graphql.{name}") not in headings
    ]
    assert not missing, missing


def test_the_api_page_names_no_private_path_in_a_heading(site: Path) -> None:
    html = (site / "api" / "index.html").read_text("utf-8")
    texts = re.findall(r'<h2 id="[^"]+"[^>]*>(.*?)</h2>', html, re.DOTALL)
    visible = [re.sub(r"<[^>]+>", "", text) for text in texts]
    assert not any("_core" in text for text in visible), visible


def test_the_quickstart_meets_spec_13() -> None:
    page = ROOT / "docs" / "index.md"
    assert len(page.read_text("utf-8").splitlines()) < 30
    blocks = blocks_of(page)
    assert [b.language for b in blocks] == ["bash", "ini", "python"]
    assert blocks[0].source.startswith("pip install ")
    assert blocks[1].source.count("gql_url") == 1
    assert blocks[2].mode == "exec"
    assert blocks[2].source.count("def test_") == 1


def _requirements() -> Iterator[Requirement]:
    for text in importlib.metadata.requires("pytest-graphql") or []:
        yield Requirement(text)


def test_no_docs_package_is_a_runtime_dependency() -> None:
    docs = {
        requirement.name.lower()
        for requirement in _requirements()
        if requirement.marker is not None
        and "extra == 'docs'" in str(requirement.marker).replace('"', "'")
    }
    assert {"mkdocs", "mkdocs-material", "mkdocstrings", "mkdocstrings-python"} <= docs
    runtime = {
        requirement.name.lower()
        for requirement in _requirements()
        if requirement.marker is None or "extra" not in str(requirement.marker)
    }
    assert not runtime & docs
    assert not any(name.startswith("mkdocs") for name in runtime)
