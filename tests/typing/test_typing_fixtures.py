"""The static typing fixtures: what the strict check must accept and reject.

Every other ``.py`` file in this directory is a fixture. It is checked by
``mypy --strict`` under the project's configuration and never run. A line
that must fail carries ``# expect: <code>``, and a line that calls
``reveal_type`` carries ``# reveal: <type>``, written without module paths
so the marker stays on one short line. The test runs mypy once over
every fixture and requires the report to match the markers exactly: each
expected error at its line with its code, each revealed type as written,
and nothing else. A marker that mypy stops reporting fails, so does an
error with no marker, so neither a weakened check nor a new error passes.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest
from mypy import api

_HERE = Path(__file__).resolve().parent
_CONFIG = _HERE.parents[1] / "pyproject.toml"
_FIXTURES = sorted(
    path for path in _HERE.glob("*.py") if not path.name.startswith("test_")
)

_MARKER = re.compile(r"#\s*(expect|reveal):\s*(.+?)\s*$")
_REPORTED = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): (?P<level>error|note): (?P<text>.*?)"
    r"(?:  \[(?P<code>[a-z0-9-]+)\])?$"
)
_REVEALED = re.compile(r'^Revealed type is "(?P<type>.*)"$')
#: A dotted module path in front of a name, as in ``builtins.str``.
_MODULE_PATH = re.compile(r"\b(?:[A-Za-z_]\w*\.)+(?=[A-Za-z_])")

#: One finding: the fixture's name, the line, the kind and its value.
Finding = tuple[str, int, str, str]


def _expected(path: Path) -> set[Finding]:
    """The markers in ``path``'s comments, read as tokens, never as code."""
    found: set[Finding] = set()
    source = path.read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        marker = _MARKER.search(token.string)
        if marker is not None:
            kind, value = marker.groups()
            found.add((path.name, token.start[0], kind, value))
    return found


@pytest.fixture(scope="module")
def reported(tmp_path_factory: pytest.TempPathFactory) -> set[Finding]:
    """What one strict run over every fixture reports, as findings."""
    cache = tmp_path_factory.mktemp("mypy-cache")
    stdout, stderr, status = api.run(
        [
            "--config-file",
            str(_CONFIG),
            "--strict",
            "--cache-dir",
            str(cache),
            "--show-error-codes",
            "--no-pretty",
            "--no-color-output",
            "--no-error-summary",
            *(str(path) for path in _FIXTURES),
        ]
    )
    # Status 1 is "the check found errors", which the fixtures intend. Any
    # other status is mypy failing to run, and stderr says why.
    assert status == 1, stderr or stdout
    names = {str(path): path.name for path in _FIXTURES}
    found: set[Finding] = set()
    for line in stdout.splitlines():
        report = _REPORTED.match(line)
        assert report is not None, f"unparsed mypy output: {line}"
        name = names.get(str(Path(report["path"]).resolve()))
        assert name is not None, f"a report outside the fixtures: {line}"
        number = int(report["line"])
        if report["level"] == "error":
            found.add((name, number, "expect", report["code"] or "<no code>"))
            continue
        revealed = _REVEALED.match(report["text"])
        if revealed is not None:
            shown = _MODULE_PATH.sub("", revealed["type"])
            found.add((name, number, "reveal", shown))
    return found


def test_there_are_fixtures_to_check() -> None:
    assert {path.name for path in _FIXTURES} == {
        "derivable_transport.py",
        "factory_callbacks.py",
        "owned_acquisition.py",
    }


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda path: path.stem)
def test_the_strict_check_reports_exactly_the_markers(
    reported: set[Finding], fixture: Path
) -> None:
    expected = _expected(fixture)
    actual = {finding for finding in reported if finding[0] == fixture.name}

    # Each file must both reject something and reveal something, so a file
    # that mypy silently skipped cannot pass with nothing to report.
    assert {kind for _, _, kind, _ in expected} == {"expect", "reveal"}
    assert actual == expected
