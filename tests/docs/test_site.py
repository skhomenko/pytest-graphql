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

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - pytest itself requires tomli on Python 3.10
    import tomli as tomllib

#: The pages of SPEC section 13 in its order, with the "Why" page and the agent
#: page after the Quickstart: file, then nav title. ``index.md`` is the
#: Quickstart, so the site root is its first page.
SPEC_PAGES = (
    ("index.md", "Quickstart"),
    ("why.md", "Why pytest-graphql?"),
    ("agents.md", "Using with coding agents"),
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


def test_the_nav_is_the_guide_pages_in_order_then_the_api_reference() -> None:
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
    r"llms\.txt": "written by mkdocs_hooks.py, project-authored text only",
    r"llms-full\.txt": "written by mkdocs_hooks.py, project-authored text only",
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


#: The one statement that leads every public text. "Product position" in
#: ``docs/reference/DESIGN_DECISIONS.md`` states it.
POSITION = (
    "The GraphQL test client for Python whose calls are built from the schema "
    "and checked against it before they are sent."
)


def _site_url() -> str:
    return str(load_config(str(ROOT / "mkdocs.yml")).site_url)


def _page_urls() -> list[tuple[str, str]]:
    """Each nav page as ``(title, url)``, in nav order."""
    site_url = _site_url()
    return [
        (title, site_url if name == "index.md" else f"{site_url}{name[:-3]}/")
        for name, title in [*SPEC_PAGES, API_PAGE]
    ]


def test_the_position_statement_leads_the_public_texts() -> None:
    config = load_config(str(ROOT / "mkdocs.yml"))
    assert config.site_description == POSITION
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["description"] == POSITION
    assert len(project["description"]) <= 200
    assert (ROOT / "README.md").read_text("utf-8").splitlines()[:3] == [
        "# pytest-graphql",
        "",
        POSITION,
    ]
    index = (ROOT / "docs" / "index.md").read_text("utf-8").splitlines()
    assert index[:3] == ["# Quickstart", "", POSITION]
    agents = (ROOT / "docs" / "agents.md").read_text("utf-8")
    assert POSITION in agents
    # The pasted block opens with it too, in a sentence that names the package.
    sentence = f"pytest-graphql is t{POSITION[1:]}"
    assert " ".join(agents.split()).count(sentence) == 2


def test_the_keywords_hold_the_search_terms() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert {
        "pytest",
        "graphql",
        "testing",
        "api",
        "integration",
        "graphql-testing",
        "api-testing",
        "schema",
        "contract-testing",
        "test-client",
        "fixtures",
    } <= set(project["keywords"])


def _outside_fences(text: str) -> list[str]:
    """The lines of a Markdown text that are not inside a fenced block."""
    lines: list[str] = []
    fence = ""
    for line in text.splitlines():
        marker = line.lstrip()[:3]
        if marker in {"```", "~~~"}:
            fence = "" if fence == marker else fence or marker
            continue
        if not fence:
            lines.append(line)
    return lines


def _check_hygiene(*paths: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_publication_hygiene.py"),
            *map(str, paths),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_llms_txt_has_its_header_lines_and_lists_the_nav_in_order(site: Path) -> None:
    text = (site / "llms.txt").read_text("utf-8")
    lines = text.splitlines()
    assert lines[:4] == ["# pytest-graphql", "", f"> {POSITION}", ""]
    outside = _outside_fences(text)
    headings = [line for line in outside if line.startswith("#")]
    assert headings == ["# pytest-graphql", "## Docs", "## Optional"]
    install = next(line for line in outside if "pip install" in line)
    assert "pytest-graphql[pytest]" in install
    assert "def test_user_has_a_name(gql):" in text
    docs = outside[outside.index("## Docs") + 1 : outside.index("## Optional")]
    entries = [line for line in docs if line.strip()]
    assert [re.match(r"- \[(.+?)\]\((\S+?)\): \S", line) for line in entries]
    assert all(entries), "every Docs line has a link and a description"
    found = [re.match(r"- \[(.+?)\]\((\S+?)\): \S", line) for line in entries]
    assert [(m[1], m[2]) for m in found if m] == _page_urls()
    optional = [line for line in outside[outside.index("## Optional") :] if line]
    assert len(optional) == 2
    assert optional[1].startswith(
        "- [Changelog](https://github.com/skhomenko/pytest-graphql/blob/main/"
    )


def _full_sections(text: str) -> list[tuple[str, str]]:
    """The ``(title, url)`` of each page section of ``llms-full.txt``."""
    lines = _outside_fences(text)
    return [
        (line.removeprefix("# "), lines[number + 2])
        for number, line in enumerate(lines)
        if line.startswith("# ") and lines[number + 2].startswith("https://")
    ]


def test_llms_full_txt_holds_each_nav_page_in_order(site: Path) -> None:
    text = (site / "llms-full.txt").read_text("utf-8")
    assert _full_sections(text) == _page_urls()
    for name, _title in SPEC_PAGES:
        source = (ROOT / "docs" / name).read_text("utf-8")
        opening = " ".join(source.split("\n\n")[1].split())
        # A link to another page is made absolute, so compare the text only.
        plain = re.sub(r"\]\([^)]*\)", "]", opening)
        assert plain in re.sub(r"\]\([^)]*\)", "]", " ".join(text.split())), name


def test_llms_full_txt_replaces_the_api_page_with_one_entry_per_name(
    site: Path,
) -> None:
    text = (site / "llms-full.txt").read_text("utf-8")
    api = text[text.index("# API reference\n") :]
    names = [
        line.removeprefix("## ")
        for line in _outside_fences(api)
        if line.startswith("## ")
    ]
    assert names == list(pytest_graphql.__all__)
    # The signature of a call, and the first paragraph of its docstring.
    assert "build_client(*, url: str, transport: Transport | None = None" in api
    assert "Build a client, with its transport and its schema" in api
    # ``unique`` is also a module name, and the entry is the function's.
    assert "Mark a value in a test payload that must be different" in api
    assert "::: pytest_graphql" not in api


@pytest.mark.parametrize("name", ["llms.txt", "llms-full.txt"])
def test_an_llms_file_is_clean_and_holds_no_contributor_text(
    site: Path, name: str
) -> None:
    _check_hygiene(site / name)
    text = (site / name).read_text("utf-8")
    assert not CONTRIBUTOR_ONLY.search(text), CONTRIBUTOR_ONLY.search(text)
    reference = sorted((ROOT / "docs" / "reference").glob("*.md"))
    assert reference
    for document in reference:
        assert document.stem not in text, document.stem
        title = document.read_text("utf-8").splitlines()[0].removeprefix("# ")
        assert f"# {title}\n" not in text, title


def test_the_hook_makes_a_link_to_another_page_absolute() -> None:
    config = SimpleNamespace(site_url="https://example.test/site/")
    text = "See [A](api.md#x.y), [B](why.md) and [C](index.md), not [D](https://e.test/z.md)."
    assert mkdocs_hooks._absolute_links(text, config) == (
        "See [A](https://example.test/site/api/#x.y), "
        "[B](https://example.test/site/why/) and [C](https://example.test/site/), "
        "not [D](https://e.test/z.md)."
    )


class _Head(HTMLParser):
    """What a search index reads from the head of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.canonical: list[str] = []
        self.description: list[str] = []
        self.robots: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link" and (values.get("rel") or "").lower() == "canonical":
            self.canonical.append(values.get("href") or "")
        elif tag == "meta" and (values.get("name") or "").lower() == "description":
            self.description.append(values.get("content") or "")
        elif tag == "meta" and (values.get("name") or "").lower() in {
            "robots",
            "googlebot",
        }:
            self.robots.append(values.get("content") or "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


@pytest.mark.parametrize(
    ("name", "url"),
    [
        (name, url)
        for (name, _title), (_t, url) in zip(
            [*SPEC_PAGES, API_PAGE], _page_urls(), strict=True
        )
    ],
)
def test_a_published_page_is_ready_for_an_index(
    site: Path, name: str, url: str
) -> None:
    head = _Head()
    head.feed((site / _page_output(name)).read_text("utf-8"))
    assert head.canonical == [url]
    assert head.title.strip()
    assert [text.strip() for text in head.description if text.strip()]
    assert not [value for value in head.robots if "noindex" in value.lower()]


def test_the_sitemap_lists_every_nav_page_and_nothing_else(site: Path) -> None:
    sitemap = (site / "sitemap.xml").read_text("utf-8")
    listed = re.findall(r"<loc>(.*?)</loc>", sitemap)
    assert sorted(listed) == sorted(url for _title, url in _page_urls())
    assert "reference" not in sitemap
