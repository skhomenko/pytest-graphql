"""The docstrings of the public API: their examples, and the rule that they exist.

``docs/api.md`` is generated from docstrings, so the examples in them are
published text that the Markdown checks of ``test_examples.py`` never see. This
file runs them, and it enforces the docstring rule of SPEC section 13 on the
public API. ``DESIGN_DECISIONS.md`` section 10, "Documentation examples", is the
rule, in its paragraph "Docstring examples".

Which objects count is read from ``mkdocs.yml`` and ``docs/api.md``, not copied
here: the filters and ``merge_init_into_class`` of the ``mkdocstrings`` options
decide which members the page shows, and the ``:::`` lines of the page decide
where it starts. Griffe reads the source statically, as the site build does.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import griffe
import pytest
import yaml

import pytest_graphql
from tests.docs.blocks import EXEC, ROOT, Block, extract
from tests.docs.runner import check

PACKAGE = "pytest_graphql"


@dataclass(frozen=True)
class Symbol:
    """One object the API page renders, with the file and line of its docstring."""

    path: str
    obj: griffe.Object
    #: A name in ``__all__``, or a method, property or class of one. The SPEC
    #: section 13 rule asks these for an example. A plain data field, such as a
    #: dataclass field, is rendered too and needs a docstring, not an example.
    needs_example: bool

    @property
    def file(self) -> str:
        return Path(str(self.obj.filepath)).relative_to(ROOT).as_posix()

    @property
    def line(self) -> int:
        docstring = self.obj.docstring
        return docstring.lineno if docstring and docstring.lineno else self.obj.lineno

    @property
    def where(self) -> str:
        return f"{self.path} ({self.file}:{self.line})"

    @property
    def text(self) -> str:
        docstring = self.obj.docstring
        return docstring.value if docstring else ""


def _mkdocstrings_options() -> dict[str, Any]:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text("utf-8"))
    for plugin in config["plugins"]:
        if isinstance(plugin, dict) and "mkdocstrings" in plugin:
            options: dict[str, Any] = plugin["mkdocstrings"]["handlers"]["python"][
                "options"
            ]
            return options
    raise AssertionError("mkdocs.yml configures no mkdocstrings plugin")


def _directives() -> list[str]:
    """The object paths ``docs/api.md`` renders, in page order."""
    lines = (ROOT / "docs" / "api.md").read_text("utf-8").splitlines()
    return [
        line.removeprefix("::: ").strip() for line in lines if line.startswith(":::")
    ]


OPTIONS = _mkdocstrings_options()
FILTERS = [
    (re.compile(text.removeprefix("!")), text.startswith("!"))
    for text in OPTIONS["filters"]
]


def kept(name: str) -> bool:
    """Whether the page shows a member of this name, by the rule ``mkdocstrings`` uses.

    The last filter that matches decides. With no match, a name is kept unless
    every filter is an inclusion.
    """
    if name == "__init__" and OPTIONS.get("merge_init_into_class"):
        return False
    keep: bool | None = None
    for regex, exclude in FILTERS:
        if regex.search(name):
            keep = not exclude
    if keep is None:
        return {exclude for _, exclude in FILTERS} != {False}
    return keep


def _members(cls: griffe.Object, path: str) -> Iterator[Symbol]:
    for name, member in cls.members.items():
        if not kept(name):
            continue
        if member.is_alias:
            member = member.final_target
        is_field = member.is_attribute and "property" not in member.labels
        yield Symbol(f"{path}.{name}", member, needs_example=not is_field)


def symbols() -> list[Symbol]:
    """Every object the API page renders."""
    package = griffe.load(PACKAGE, search_paths=[str(ROOT / "src")])
    found = [Symbol(PACKAGE, package, needs_example=True)]
    rendered_elsewhere = {
        directive.removeprefix(f"{PACKAGE}.") for directive in _directives()[1:]
    }
    for name in pytest_graphql.__all__:
        exported = package.members[name]
        target = exported.final_target if exported.is_alias else exported
        if target.is_module:
            # ``unique`` is a function and also the name of its module, and
            # static analysis resolves the package attribute to the module. The
            # second directive of the page renders the function from its own
            # path, so the symbol is that function.
            (match,) = (r for r in rendered_elsewhere if r.endswith(f".{name}"))
            target = package[match]
        found.append(Symbol(f"{PACKAGE}.{name}", target, needs_example=True))
        if target.is_class:
            found.extend(_members(target, f"{PACKAGE}.{name}"))
    return found


SYMBOLS = symbols()


def _blocks(symbol: Symbol) -> list[Block]:
    """The fenced blocks of a docstring, placed at their lines in the source.

    ``Block.where`` then reads as the symbol, the file and the line of the
    fence, and a traceback inside an example names the source line.
    """
    first = symbol.line
    return [
        dataclasses.replace(
            block,
            path=f"{symbol.file} ({symbol.path})",
            line=first + block.line - 1,
        )
        for block in extract(symbol.text, symbol.path)
    ]


BLOCKS = [block for symbol in SYMBOLS for block in _blocks(symbol)]
CHECKED = [block for block in BLOCKS if block.is_python or block.problems]


@pytest.mark.parametrize("block", CHECKED, ids=lambda block: block.where)
def test_the_example_passes_its_check_and_is_run_when_it_says_exec(
    block: Block, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = check(block, pytester, monkeypatch)
    # Passing proves little if the block was only compiled, so the outcome is
    # asserted: an exec block ran, and nothing else did.
    assert outcome == ("ran" if block.mode == EXEC else "compiled"), block.where


def test_every_block_sits_on_its_fence_line_in_the_source() -> None:
    sources: dict[str, list[str]] = {}
    wrong = []
    for symbol in SYMBOLS:
        lines = sources.setdefault(
            symbol.file, (ROOT / symbol.file).read_text("utf-8").splitlines()
        )
        for block in _blocks(symbol):
            if not lines[block.line - 1].lstrip().startswith(("```", "~~~")):
                wrong.append(f"{symbol.where}: block line {block.line}")
    assert BLOCKS, "no docstring has a block, so this test proves nothing"
    assert not wrong, wrong


def test_the_selection_is_what_the_api_page_renders() -> None:
    paths = {symbol.path for symbol in SYMBOLS}
    assert [PACKAGE, *(f"{PACKAGE}.{name}" for name in pytest_graphql.__all__)] == [
        symbol.path for symbol in SYMBOLS if symbol.path.count(".") <= 1
    ]
    # The filters of mkdocs.yml, applied as the page applies them.
    assert kept("__version__")
    assert kept("__enter__")
    assert kept("name")
    assert not kept("_private")
    assert not kept("_")
    assert not kept("__repr__")
    assert not kept("__init__"), "the constructor is shown with its class"
    assert f"{PACKAGE}.GraphQLClient.query" in paths
    assert f"{PACKAGE}.GraphQLClient._clone" not in paths
    assert f"{PACKAGE}.GraphQLClient.__init__" not in paths


def test_every_public_symbol_has_a_docstring_with_an_example_that_runs() -> None:
    """SPEC section 13: every public symbol has a docstring with an example."""
    without_docstring = []
    without_example = []
    for symbol in SYMBOLS:
        if not symbol.text.strip():
            without_docstring.append(symbol.where)
        elif symbol.needs_example and not any(
            block.is_python and block.mode == EXEC for block in _blocks(symbol)
        ):
            without_example.append(symbol.where)
    assert not without_docstring, without_docstring
    assert not without_example, without_example


def test_every_object_the_page_renders_has_a_docstring() -> None:
    """``show_if_no_docstring`` is on, so a gap would be visible on the page."""
    assert not [s.where for s in SYMBOLS if not s.text.strip()]


def test_each_public_symbol_has_an_example_that_runs() -> None:
    ran = [b for b in CHECKED if b.is_python and b.mode == EXEC]
    # A block that is only compiled is code that needs a server. The rule above
    # asks each public symbol for an exec block, so there are at least as many.
    assert len(ran) >= len([s for s in SYMBOLS if s.needs_example])
