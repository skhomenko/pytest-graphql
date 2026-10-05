"""Run the ``pytest`` commands that a documentation page shows, and compare.

A page that shows what pytest prints puts a ``bash`` block with the command
before a ``text`` block with the output. This module walks the blocks of one
page in order and keeps the files that its blocks name with ``title="..."``: a
Python block titled ``test_x.py`` is a test file, ``conftest.py`` is the
conftest, and an ``ini`` block titled ``pytest.ini`` is the ini file. A later
block with the same title replaces the earlier one. For each command it builds
that project and runs the command in an inner session.

The comparison rules are the same for every page:

- every line of the ``text`` block is a line of the real output, in the same
  order, after the numbers that change from run to run are replaced
  (milliseconds, the load time, the run id). A line of three dots stands for any
  lines between;
- the project runs over the in-process fake transport, so nothing opens a
  socket.

A page shows only lines that the plugin itself prints, so a new pytest version
cannot change what is compared.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.docs.blocks import Block, blocks_of
from tests.unit.plugin_inner import FAKE_TRANSPORT, QUIET_INNER

TITLE = re.compile(r'title="([^"]+)"')

#: Numbers that differ on every run. Both sides are normalised before they are
#: compared, so the page may show any value.
VARYING = (
    (re.compile(r"\b\d+ms\b"), "Nms"),
    (re.compile(r"\b\d+(?:\.\d+)? ms\b"), "N ms"),
    (re.compile(r"loaded in \d+(?:\.\d+)?s"), "loaded in Ns"),
    (re.compile(r"run id [0-9a-f]{32}"), "run id ID"),
)
ELLIPSIS = "..."


@dataclass(frozen=True)
class Run:
    where: str
    args: tuple[str, ...]
    files: dict[str, str]
    shown: tuple[str, ...]


def _title(block: Block) -> str | None:
    found = TITLE.search(block.info)
    return found.group(1) if found else None


def runs(page: Path) -> list[Run]:
    found: list[Run] = []
    files: dict[str, str] = {}
    blocks = blocks_of(page)
    for index, block in enumerate(blocks):
        title = _title(block)
        if title is not None:
            files[title] = block.source
        first = block.source.splitlines()[0] if block.source else ""
        if block.language != "bash" or not first.startswith("pytest"):
            continue
        shown = blocks[index + 1]
        assert shown.language == "text", f"{block.where}: no output follows"
        found.append(
            Run(
                where=block.where,
                args=tuple(shlex.split(first)[1:]),
                files=dict(files),
                shown=tuple(shown.source.splitlines()),
            )
        )
    return found


def normalise(line: str) -> str:
    for pattern, replacement in VARYING:
        line = pattern.sub(replacement, line)
    return line.rstrip()


def run_project(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, run: Run
) -> list[str]:
    """Build the project of the page and run the command. Returns its output."""
    # An option from the outer run must not reach the inner one.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for name, source in run.files.items():
        if name == "conftest.py":
            continue
        if name == "pytest.ini":
            pytester.makeini(source)
        else:
            (pytester.path / name).write_text(source, encoding="utf-8")
    if "pytest.ini" not in run.files:
        # A page that shows no ini file still needs an endpoint to label calls.
        pytester.makeini("[pytest]\ngql_url = http://localhost:8000/graphql\n")
    pytester.makeconftest(FAKE_TRANSPORT + "\n" + run.files.get("conftest.py", ""))
    result = pytester.runpytest(*QUIET_INNER, *run.args)
    return result.outlines


def assert_shown_lines_are_real(run: Run, output: list[str]) -> None:
    """Every line of the page is a line of the output, in the same order."""
    actual = [normalise(line) for line in output]
    position = 0
    for line in run.shown:
        if line == ELLIPSIS:
            continue
        wanted = normalise(line)
        try:
            position = actual.index(wanted, position) + 1
        except ValueError:
            raise AssertionError(
                f"{run.where}: the page shows a line that the run did not print, "
                f"or printed in another order:\n{wanted}\n\nthe run printed:\n"
                + "\n".join(actual)
            ) from None
