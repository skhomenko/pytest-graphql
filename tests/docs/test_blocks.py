"""The extractor and the lint, on small pages written for the purpose."""

from __future__ import annotations

import pytest

from tests.docs.blocks import Block, classify, extract, page_paths


def _only(text: str) -> Block:
    blocks = extract(text, "page.md")
    assert len(blocks) == 1
    return blocks[0]


def test_a_block_has_its_source_and_the_line_of_its_fence() -> None:
    text = "Intro\n\n```python {.exec}\nx = 1\ny = 2\n```\n"
    block = _only(text)
    assert block.line == 3
    assert block.where == "page.md:3"
    assert block.source == "x = 1\ny = 2\n"
    assert block.mode == "exec"
    assert block.problems == ()


def test_the_markers_may_sit_beside_other_attributes() -> None:
    text = '```python {.no-exec title="a .exec b.py" linenums="1"}\nx = 1\n```\n'
    block = _only(text)
    assert block.markers == ("no-exec",)
    assert block.problems == ()


def test_the_language_is_compared_without_regard_to_case() -> None:
    assert _only("```PYTHON {.exec}\nx = 1\n```\n").is_python


def test_a_fence_inside_a_list_loses_its_indent() -> None:
    text = "- item\n\n    ```python {.exec}\n    if True:\n        x = 1\n    ```\n"
    assert _only(text).source == "if True:\n    x = 1\n"


def test_a_tilde_fence_is_a_fence() -> None:
    assert _only("~~~python {.exec}\nx = 1\n~~~\n").source == "x = 1\n"


def test_a_longer_fence_holds_a_shorter_one() -> None:
    text = "````markdown\n```python\nx = 1\n```\n````\n"
    block = _only(text)
    assert block.language == "markdown"
    assert block.source == "```python\nx = 1\n```\n"


def test_only_the_same_fence_character_closes_a_fence() -> None:
    text = "```text\n~~~\nstill inside\n```\n"
    assert _only(text).source == "~~~\nstill inside\n"


def test_inline_code_that_starts_a_line_is_not_a_fence() -> None:
    assert extract("```code``` is inline\n", "page.md") == []


def test_an_unclosed_fence_is_a_problem() -> None:
    block = _only("```python {.exec}\nx = 1\n")
    assert any("never closed" in problem for problem in block.problems)


@pytest.mark.parametrize(
    ("info", "fragment"),
    [
        ("python", "needs {.exec} or {.no-exec}"),
        ("python {.other}", "needs {.exec} or {.no-exec}"),
        ("", "names no language"),
        ("python exec", "not a language followed by one attribute list"),
        ("python no-exec", "not a language followed by one attribute list"),
        ("{ .python .exec }", "not a language followed by one attribute list"),
        ("python {.exec} {.no-exec}", "not a language followed by one"),
        ("python {.exec .no-exec}", "one marker"),
        ("python {.exec .exec}", "one marker"),
        ("py {.exec}", "write the language as python"),
        ("python3 {.exec}", "write the language as python"),
        ("bash {.exec}", "Python blocks only"),
    ],
)
def test_the_lint_rejects(info: str, fragment: str) -> None:
    _language, _markers, problems = classify(info)
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize("info", ["bash", "ini", "text", "graphql", 'ini {title="x"}'])
def test_a_block_in_another_language_needs_no_marker(info: str) -> None:
    assert classify(info)[2] == ()


def test_the_page_set_leaves_out_the_contributor_documentation() -> None:
    paths = {path.as_posix() for path in page_paths()}
    assert not any("/docs/reference/" in path for path in paths)
    assert any(path.endswith("/docs/index.md") for path in paths)
