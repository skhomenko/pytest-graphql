"""The table on the Configuration page matches the table the plugin reads.

``docs/configuration.md`` has one table of every ini option, flag and
environment variable. The plugin builds its ini registration, its flags and its
environment variable names from ``OPTIONS`` in
``src/pytest_graphql/plugin/options.py``. These tests read that table and the
page table and compare them, so a new option, a changed default or a renamed
variable fails here until the page says the same. The page lists nothing the
plugin lacks.

The flags that no option owns (``--gql-log`` and its two siblings) are found by
running the plugin's own ``register`` against a recording parser, so a flag
added there is found without a list kept in this file.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pytest_graphql._core.client import ClientConfig
from pytest_graphql.plugin import options
from pytest_graphql.plugin.options import DEFAULT_SCHEMA_SOURCE, OPTIONS, Option
from tests.docs.blocks import ROOT, blocks_of
from tests.unit.plugin_inner import run_inner

PAGE = ROOT / "docs" / "configuration.md"
NONE = "none"

#: ``--gql-log`` and the other flags that have no option behind them, with the
#: ``Settings`` field that holds the value they set.
FLAG_ONLY_FIELDS = {
    "gql_log": "log",
    "gql_log_level": "log_level",
    "gql_show_schema_stats": "show_schema_stats",
}


@dataclass(frozen=True)
class Row:
    ini: str
    flag: str
    env: str
    default: str


def _cells(line: str) -> list[str]:
    inner = line.strip().strip("|")
    return [part.strip().strip("`") for part in inner.split("|")]


def _page_rows() -> list[Row]:
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("| Ini option")
    )
    rows: list[Row] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = _cells(line)
        assert len(cells) == 5, f"a row of the option table has 5 cells: {line!r}"
        rows.append(Row(cells[0], cells[1], cells[2], cells[3]))
    assert rows, "the option table is empty"
    return rows


class _Recorder:
    """Stands in for ``pytest.Parser`` and records what ``register`` adds."""

    def __init__(self) -> None:
        self.ini: list[str] = []
        self.flags: dict[str, dict[str, Any]] = {}

    def getgroup(self, *_args: object) -> _Recorder:
        return self

    def addini(self, name: str, *_args: object, **_kwargs: object) -> None:
        self.ini.append(name)

    def addoption(self, *names: str, **kwargs: Any) -> None:
        for name in names:
            self.flags[name] = kwargs


def _registered() -> _Recorder:
    recorder = _Recorder()
    options.register(recorder)  # type: ignore[arg-type]
    return recorder


def _words(value: object) -> str:
    """A default the way the page writes it."""
    if value is None:
        return NONE
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (Mapping, Sequence)):
        return ", ".join(str(item) for item in value) if value else "empty"
    raise AssertionError(f"no wording for the default {value!r}")


def _default_of(option: Option) -> str:
    if option.field is None:
        assert option.ini == "gql_schema_source"
        return DEFAULT_SCHEMA_SOURCE
    return _words(getattr(ClientConfig(), option.field))


def _flag_text(name: str, registered: _Recorder) -> str:
    """The flag as the page writes it: its name and its metavar.

    A metavar such as ``N|random`` lists two forms. A pipe does not survive a
    Markdown table inside a code span, so the page writes the first form and
    says in its last column that the flag accepts the other.
    """
    metavar = registered.flags[name].get("metavar")
    return f"{name} {str(metavar).split('|')[0]}" if metavar else name


def _expected_rows() -> list[Row]:
    registered = _registered()
    rows = [
        Row(
            option.ini,
            _flag_text(option.flag.name, registered) if option.flag else NONE,
            option.env,
            _default_of(option),
        )
        for option in OPTIONS
    ]
    owned = {option.flag.name for option in OPTIONS if option.flag}
    settings_defaults = {
        field.name: field.default for field in dataclasses.fields(options.Settings)
    }
    for name, kwargs in registered.flags.items():
        if name in owned:
            continue
        field = FLAG_ONLY_FIELDS[kwargs["dest"]]
        default = _off(settings_defaults[field])
        rows.append(Row(NONE, _flag_text(name, registered), NONE, default))
    return rows


def _off(value: object) -> str:
    """A switch reads ``off`` or ``on``. Anything else reads as ``_words`` says."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return _words(value)


def test_the_page_lists_every_ini_option_the_plugin_registers() -> None:
    registered = _registered()
    on_page = [row.ini for row in _page_rows() if row.ini != NONE]
    assert sorted(on_page) == sorted(registered.ini)
    assert len(on_page) == len(set(on_page)), "an ini option has two rows"


def test_the_page_lists_every_flag_the_plugin_registers_and_no_other() -> None:
    registered = _registered()
    on_page = [row.flag.split()[0] for row in _page_rows() if row.flag != NONE]
    assert sorted(on_page) == sorted(registered.flags)


def test_the_page_lists_every_environment_variable_and_no_other() -> None:
    on_page = [row.env for row in _page_rows() if row.env != NONE]
    assert sorted(on_page) == sorted(option.env for option in OPTIONS)


def test_each_row_has_the_flag_variable_and_default_of_the_plugin() -> None:
    expected = {(row.ini, row.flag): row for row in _expected_rows()}
    actual = {(row.ini, row.flag): row for row in _page_rows()}
    assert actual == expected


def test_the_rows_come_in_the_order_of_the_plugin_table() -> None:
    expected = [row.ini for row in _expected_rows() if row.ini != NONE]
    actual = [row.ini for row in _page_rows() if row.ini != NONE]
    assert actual == expected


def test_the_default_wording_covers_every_kind_of_default() -> None:
    """A default the wording cannot express fails here, not by luck in a row."""
    assert _words(None) == NONE
    assert _words(True) == "true"
    assert _words(30.0) == "30"
    assert _words(2.5) == "2.5"
    assert _words({}) == "empty"
    assert _words(()) == "empty"
    assert _words(("a", "b")) == "a, b"
    with pytest.raises(AssertionError):
        _words(object())


# -- the output the page shows ------------------------------------------------


def _text_blocks() -> list[str]:
    return [
        block.source.strip() for block in blocks_of(PAGE) if block.language == "text"
    ]


def test_the_shown_usage_error_is_what_pytest_prints(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, ini="gql_max_depth = 0\n")
    lines = [line for line in result.stderr.lines if "pytest-graphql" in line]
    assert len(lines) == 1, result.stderr.str()
    assert lines[0] in _text_blocks()


def test_the_shown_missing_endpoint_message_is_what_pytest_prints(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_GQL_URL", raising=False)
    result = run_inner(
        pytester,
        monkeypatch,
        test="def test_a(gql):\n    pass\n",
        url=False,
    )
    message = next(
        line for line in result.outlines if line.startswith("pytest-graphql: no")
    )
    assert message in _text_blocks()


# -- the words a boolean takes ------------------------------------------------


def _boolean_words() -> tuple[list[str], list[str]]:
    """The words the page lists as true and as false."""
    text = PAGE.read_text(encoding="utf-8")
    start = text.index("A boolean is case-insensitive.")
    paragraph = text[start : text.index("\n\n", start)]
    true_part, false_part = paragraph.split(" mean true.")
    words = re.compile(r"`([^`]+)`")
    return words.findall(true_part), words.findall(false_part.split(" mean false")[0])


def test_the_boolean_words_on_the_page_are_the_ones_the_plugin_accepts() -> None:
    true_words, false_words = _boolean_words()
    assert len(true_words) == 6 and len(false_words) == 6
    for word in true_words:
        assert options._boolean(word, Path()) is True, word
        assert options._boolean(word.upper(), Path()) is True, word
    for word in false_words:
        assert options._boolean(word, Path()) is False, word
        assert options._boolean(word.upper(), Path()) is False, word
    # The page lists every word the plugin knows, not a part of them.
    assert set(true_words) == set(options._TRUE)
    assert set(false_words) == set(options._FALSE)
