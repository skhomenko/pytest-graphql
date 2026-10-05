"""Fenced code blocks in Markdown pages: extraction, markers and lint.

Pure standard library, and no MkDocs, so the rules here are what the docs tests
enforce and nothing else. ``docs/reference/DESIGN_DECISIONS.md`` section 10
states them.

A Python block carries one marker after the language, written as an attribute
list::

    ```python {.exec}
    ```python {.no-exec}

Material's Markdown extension reads ``{.exec}`` as a CSS class and still
highlights the block as Python. A bare word such as ``python exec`` is not
valid there: the fence stops being a fence, and the rest of the page is
rendered wrongly with no warning, even under ``--strict``. So the lint rejects
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXEC = "exec"
NO_EXEC = "no-exec"
MARKERS = (EXEC, NO_EXEC)

#: Language words that Pygments treats as Python. Only ``python`` is accepted,
#: so there is one spelling to search for and one the lint can require a marker
#: on.
PYTHON = "python"
PYTHON_ALIASES = frozenset({"py", "py3", "python3", "pycon"})

_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# The language is a run of the characters Material's superfences accepts. The
# only thing allowed after it is one attribute list.
_INFO = re.compile(r"^(?P<lang>[\w#+-]+)(?:[ \t]+\{(?P<attrs>[^{}]*)\})?$")
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_CLASS = re.compile(r"(?:^|\s)\.(?P<name>[\w-]+)")


@dataclass(frozen=True)
class Block:
    """One fenced block, with where it came from and what is wrong with it."""

    path: str
    line: int
    info: str
    language: str
    markers: tuple[str, ...]
    source: str
    problems: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def is_python(self) -> bool:
        return self.language == PYTHON

    @property
    def mode(self) -> str | None:
        """``exec``, ``no-exec``, or None when the block has no usable marker."""
        return self.markers[0] if len(self.markers) == 1 else None


def classify(info: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Read an info string as ``(language, markers, problems)``."""
    info = info.strip()
    if not info:
        return "", (), ("the fence names no language; write one, such as text",)
    match = _INFO.match(info)
    if match is None:
        return (
            "",
            (),
            (
                f"the info string {info!r} is not a language followed by one "
                "attribute list; write markers as {.exec} or {.no-exec} after "
                "the language, because MkDocs does not render any other form",
            ),
        )
    language = match["lang"].lower()
    attrs = _QUOTED.sub('""', match["attrs"] or "")
    markers = tuple(c for c in _CLASS.findall(attrs) if c in MARKERS)
    problems: list[str] = []
    if language in PYTHON_ALIASES:
        problems.append(f"write the language as python, not {language}")
    elif language == PYTHON:
        if not markers:
            problems.append(
                "a Python block needs {.exec} or {.no-exec} after the language"
            )
        elif len(markers) > 1:
            problems.append("a block carries one marker, not both or a repeat")
    elif markers:
        problems.append("markers apply to Python blocks only")
    return language, markers, tuple(problems)


def extract(text: str, path: str) -> list[Block]:
    """Every fenced block in ``text``, in order.

    A fence opens on three or more backticks or tildes at any indent, because
    superfences accepts a fence inside a list or an admonition. It closes on a
    line holding only the same character, at least as many times. A backtick
    fence whose info string holds a backtick is inline code, not a fence.
    Content lines lose the indent of the opening fence.
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        opening = _FENCE.match(lines[i])
        if opening is None or (opening["fence"][0] == "`" and "`" in opening["info"]):
            i += 1
            continue
        char = opening["fence"][0]
        width = len(opening["fence"])
        indent = len(opening["indent"])
        body: list[str] = []
        closed = False
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped and set(stripped) == {char} and len(stripped) >= width:
                closed = True
                break
            body.append(_dedent(lines[j], indent))
            j += 1
        language, markers, problems = classify(opening["info"])
        if not closed:
            problems = (*problems, "the fence is never closed")
        blocks.append(
            Block(
                path=path,
                line=i + 1,
                info=opening["info"].strip(),
                language=language,
                markers=markers,
                source="".join(f"{row}\n" for row in body),
                problems=problems,
            )
        )
        i = j + 1
    return blocks


def _dedent(line: str, width: int) -> str:
    head = line[:width]
    return line[width:] if not head.strip() else line.lstrip()


def page_paths(root: Path = ROOT) -> list[Path]:
    """The Markdown files whose Python blocks the docs tests check.

    Every page under ``docs/`` except ``docs/reference/``, which is contributor
    documentation and not published, plus the README. The README is first.
    """
    docs = root / "docs"
    pages = sorted(
        path
        for path in docs.rglob("*.md")
        if path.relative_to(docs).parts[0] != "reference"
    )
    return [root / "README.md", *pages]


def blocks_of(path: Path, root: Path = ROOT) -> list[Block]:
    text = path.read_text(encoding="utf-8")
    return extract(text, path.relative_to(root).as_posix())
