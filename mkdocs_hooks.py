"""MkDocs hooks for the documentation site.

Registered in ``mkdocs.yml``. ``on_files`` keeps a rule from "Documentation
dependencies" of ``docs/reference/DESIGN_DECISIONS.md``: the site publishes only
third-party files it needs and has a recorded licence basis for. ``on_post_build``
writes ``llms.txt`` and ``llms-full.txt`` from the nav, as "Agent and index
discoverability" in the same document describes.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING

from mkdocs.exceptions import PluginError

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.files import Files

#: Material ships the Lunr language packs as theme files, and MkDocs copies every
#: theme file into the site. Search loads one only when its language is not
#: English. The packs carry Mozilla Public License headers, so a pack that is
#: published without being used is a notice and source-offer duty for nothing.
_LUNR_DIR = "assets/javascripts/lunr/"


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Leave the unused Lunr language packs out of the site."""
    languages = list(config.plugins["material/search"].config.lang or [])
    if languages != ["en"]:
        # Dropping the packs would break search in that language, and keeping
        # them would publish MPL files without a notice. Neither is silent.
        raise PluginError(
            f"search languages {languages!r}: this site publishes English search "
            "only. Add a licence notice for the Lunr language packs and change "
            "mkdocs_hooks.py before enabling another language."
        )
    for file in [f for f in files if f.src_uri.startswith(_LUNR_DIR)]:
        files.remove(file)
    return files


#: The changelog is a repository file, so it is not a page of the site.
_CHANGELOG = "https://github.com/skhomenko/pytest-graphql/blob/main/CHANGELOG.md"

_FENCE = re.compile(
    r"^```(?P<language>\w+)[^\n]*\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE
)

#: A link from one page to another page of the site, with an optional anchor.
_PAGE_LINK = re.compile(r"\]\((?P<page>[A-Za-z0-9_-]+)\.md(?P<anchor>#[^)\s]*)?\)")

#: The nav entry that the generated API reference fills. In ``llms-full.txt`` it
#: becomes one entry for each public name, and not the page source.
_API_PAGE = "api.md"


def _nav_pages(config: MkDocsConfig) -> list[tuple[str, str]]:
    """The ``(title, file)`` pairs of the nav, in nav order. The nav must be flat."""
    pages: list[tuple[str, str]] = []
    for item in config.nav or []:
        if not isinstance(item, dict):
            raise PluginError(f"the nav entry {item!r} is not a titled page")
        for title, target in item.items():
            if not isinstance(target, str):
                raise PluginError(f"the nav entry {title!r} is not a single page")
            pages.append((title, target))
    return pages


def _page_url(config: MkDocsConfig, source: str) -> str:
    stem = source.removesuffix(".md")
    return config.site_url if stem == "index" else f"{config.site_url}{stem}/"


def _absolute_links(text: str, config: MkDocsConfig) -> str:
    """Make a link to another page absolute, because a model reads this text away
    from the site."""

    def replace(match: re.Match[str]) -> str:
        url = _page_url(config, f"{match['page']}.md")
        return f"]({url}{match['anchor'] or ''})"

    return _PAGE_LINK.sub(replace, text)


def _first_paragraph(text: str) -> str:
    """The first paragraph after the title, on one line."""
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    return " ".join(paragraphs[1].split()) if len(paragraphs) > 1 else ""


def _first_sentence(paragraph: str) -> str:
    return re.split(r"(?<=\.)\s", paragraph, maxsplit=1)[0]


def _quickstart(docs_dir: Path) -> tuple[str, str]:
    """The install line and the test of the Quickstart page, read from its source so
    that the three copies of them cannot drift apart."""
    blocks = [
        (match["language"], match["body"])
        for match in _FENCE.finditer((docs_dir / "index.md").read_text("utf-8"))
    ]
    install = next(body for language, body in blocks if language == "bash")
    test = next(body for language, body in blocks if language == "python")
    return install.strip(), test.strip()


def _llms_txt(config: MkDocsConfig, pages: list[tuple[str, str]]) -> str:
    docs_dir = Path(config.docs_dir)
    install, test = _quickstart(docs_dir)
    lines = [
        "# pytest-graphql",
        "",
        f"> {config.site_description}",
        "",
        f"Install it with `{install}`, set `gql_url` in `pytest.ini`, and write a test "
        "with the `gql` fixture:",
        "",
        "```python",
        test,
        "```",
        "",
        "## Docs",
        "",
    ]
    for title, source in pages:
        text = (docs_dir / source).read_text("utf-8")
        line = _absolute_links(_first_sentence(_first_paragraph(text)), config)
        lines.append(f"- [{title}]({_page_url(config, source)}): {line}")
    lines += [
        "",
        "## Optional",
        "",
        f"- [Changelog]({_CHANGELOG}): every change, by version",
        "",
    ]
    return "\n".join(lines)


class _Plain(str):
    """A string that a signature prints as it is, without the quotes of a ``repr``.

    The package postpones the evaluation of its annotations, so each one is the
    text that the author wrote. ``inspect`` would print that text in quotes.
    """

    __repr__ = str.__str__


def _plain(annotation: object) -> object:
    return _Plain(annotation) if isinstance(annotation, str) else annotation


def _signature(value: object) -> inspect.Signature | None:
    """The signature of a class or function, or ``None`` when it has none.

    A constant has no signature. A class built on a builtin one, such as an
    exception that adds no ``__init__``, has none that Python can report.
    """
    if not (inspect.isclass(value) or inspect.isroutine(value)):
        return None
    try:
        signature = inspect.signature(value)
    except ValueError:
        return None
    return signature.replace(
        parameters=[
            parameter.replace(annotation=_plain(parameter.annotation))
            for parameter in signature.parameters.values()
        ],
        return_annotation=_plain(signature.return_annotation),
    )


def _member_path(name: str, value: object) -> str:
    """Where the docstring of a public name lives, relative to the package.

    A class or function is found where it is defined. That matters for
    ``unique``, which is also the name of the module that defines it, so the
    package attribute of that name resolves to the module, as in ``docs/api.md``.
    A constant has no defining module to ask, so its package attribute is used.
    """
    if inspect.isclass(value) or inspect.isroutine(value):
        return f"{value.__module__}.{value.__qualname__}".removeprefix(
            "pytest_graphql."
        )
    return name


def _api_entries(config: MkDocsConfig) -> str:
    """One entry for each public name: the name, its signature and the first
    paragraph of its docstring, read as the API page reads it."""
    import griffe

    import pytest_graphql

    package = griffe.load(
        "pytest_graphql", search_paths=[Path(config.docs_dir).parent / "src"]
    )
    entries = []
    for name in pytest_graphql.__all__:
        value = getattr(pytest_graphql, name)
        docstring = package[_member_path(name, value)].docstring
        summary = (
            inspect.cleandoc(docstring.value).split("\n\n")[0] if docstring else ""
        )
        entry = [f"## {name}", ""]
        signature = _signature(value)
        if signature is not None:
            entry += ["```python", f"{name}{signature}", "```", ""]
        entry += [" ".join(summary.split()), ""]
        entries.append("\n".join(entry))
    return "\n".join(entries)


def _llms_full_txt(config: MkDocsConfig, pages: list[tuple[str, str]]) -> str:
    docs_dir = Path(config.docs_dir)
    sections = []
    for title, source in pages:
        if source == _API_PAGE:
            body = _api_entries(config)
        else:
            text = (docs_dir / source).read_text("utf-8")
            # The page title is already the heading of the section.
            body = _absolute_links(text.split("\n", 1)[1].strip(), config)
        sections.append(f"# {title}\n\n{_page_url(config, source)}\n\n{body}\n")
    return "\n".join(sections)


def on_post_build(config: MkDocsConfig) -> None:
    """Write ``llms.txt`` and ``llms-full.txt`` at the root of the site."""
    pages = _nav_pages(config)
    site_dir = Path(config.site_dir)
    (site_dir / "llms.txt").write_text(_llms_txt(config, pages), encoding="utf-8")
    (site_dir / "llms-full.txt").write_text(
        _llms_full_txt(config, pages), encoding="utf-8"
    )
