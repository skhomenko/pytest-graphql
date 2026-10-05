"""The runner, fed blocks written to be wrong.

A broken ``exec`` example must fail CI. These tests send broken and unmarked
blocks through the same ``check`` the real pages use, and expect it to refuse
each one. Without them the real pages passing would prove little, because a
runner that checked nothing would pass them too.
"""

from __future__ import annotations

from typing import Any

import pytest

from pytest_graphql import GraphQLClient, build_client
from tests.docs import runner
from tests.docs.blocks import extract
from tests.docs.runner import NAMESPACE_NAMES, ExampleError, check


def _check(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    info: str = "python {.exec}",
) -> None:
    (block,) = extract(f"```{info}\n{body}```\n", "page.md")
    check(block, pytester, monkeypatch)


def test_an_exec_block_that_passes_is_accepted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _check(pytester, monkeypatch, 'assert gql.query("user", id="u1").name\n')


def test_an_exec_block_that_raises_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExampleError, match=r"page\.md:1: raised ZeroDivisionError"):
        _check(pytester, monkeypatch, "1 / 0\n")


def test_an_exec_block_with_a_wrong_expectation_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = 'assert gql.query("user", id="u1").name == "Not Ada"\n'
    with pytest.raises(ExampleError, match="AssertionError"):
        _check(pytester, monkeypatch, body)


def test_an_exec_block_with_a_syntax_error_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExampleError, match="not valid Python"):
        _check(pytester, monkeypatch, "def broken(:\n")


def test_an_exec_block_that_exits_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExampleError, match="SystemExit"):
        _check(pytester, monkeypatch, "raise SystemExit(0)\n")


def test_a_traceback_names_the_line_of_the_page(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Intro\n\n```python {.exec}\nx = 1\nraise KeyError(x)\n```\n"
    (block,) = extract(text, "p.md")
    with pytest.raises(ExampleError) as caught:
        check(block, pytester, monkeypatch)
    assert caught.value.__cause__ is not None
    assert caught.value.__cause__.__traceback__ is not None
    frames = []
    tb = caught.value.__cause__.__traceback__
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_lineno))
        tb = tb.tb_next
    assert ("p.md:3", 5) in frames


def test_an_exec_block_sees_exactly_the_documented_names(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "names = sorted(n for n in globals() if not n.startswith('__'))\n"
        f"assert names == {sorted(NAMESPACE_NAMES)!r}, names\n"
        "assert __name__ == '__docs_example__'\n"
    )
    _check(pytester, monkeypatch, body)


def test_each_exec_block_starts_from_a_fresh_namespace(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _check(pytester, monkeypatch, "leftover = 1\n")
    with pytest.raises(ExampleError, match="NameError"):
        _check(pytester, monkeypatch, "assert leftover\n")


def test_each_exec_block_gets_its_own_client(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[GraphQLClient] = []

    def spy(**kwargs: Any) -> GraphQLClient:
        client = build_client(**kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(runner, "build_client", spy)
    _check(pytester, monkeypatch, "assert gql.query('user', id='u1')\n")
    _check(pytester, monkeypatch, "assert gql.query('user', id='u1')\n")
    assert len(built) == 2
    assert built[0] is not built[1]


def test_a_no_exec_block_is_compiled_and_never_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _check(pytester, monkeypatch, "1 / 0\nnever_defined\n", "python {.no-exec}")


def test_a_no_exec_block_with_a_syntax_error_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExampleError, match="not valid Python"):
        _check(pytester, monkeypatch, "def broken(:\n", "python {.no-exec}")


@pytest.mark.parametrize(
    "info",
    ["python", "python {.other}", "python exec", "python no-exec", "py {.exec}", ""],
)
def test_a_python_block_without_a_usable_marker_fails_even_when_it_is_valid(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, info: str
) -> None:
    with pytest.raises(ExampleError, match=r"page\.md:1: "):
        _check(pytester, monkeypatch, "x = 1\n", info)


def test_a_block_in_another_language_is_not_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _check(pytester, monkeypatch, "not python at all (\n", "text")


def test_a_test_function_block_runs_as_a_pytest_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = 'def test_it(gql):\n    assert gql.query("user", id="u1").name\n'
    _check(pytester, monkeypatch, body)


def test_a_failing_test_function_block_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        'def test_it(gql):\n    assert gql.query("user", id="u1").name == "Not Ada"\n'
    )
    with pytest.raises(ExampleError, match="did not all pass"):
        _check(pytester, monkeypatch, body)


def test_a_test_function_that_names_an_unknown_fixture_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExampleError, match="did not all pass"):
        _check(pytester, monkeypatch, "def test_it(no_such_fixture):\n    pass\n")


def test_a_test_class_block_runs_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "class TestIt:\n    def test_it(self, gql):\n        assert gql.schema\n"
    _check(pytester, monkeypatch, body)
