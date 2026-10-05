"""Run the Python blocks of a documentation page.

``check`` is the one entry point, for the real pages and for the tests that
feed it a deliberately broken block. What it does, by block:

- a block with a lint problem fails, whatever it contains;
- a Python block marked ``no-exec`` is compiled and never run;
- a Python block marked ``exec`` that defines a test (a top-level ``test*``
  function or ``Test*`` class) runs as a real pytest file in an inner session,
  with the plugin loaded, so the ``gql`` fixture and the other plugin fixtures
  work as they do for a user;
- any other Python block marked ``exec`` runs in a fresh namespace in this
  process.

A block in another language is not checked.

Every block runs over the test schema in ``tests/schema/``, through the
in-process fake transport, so no example opens a socket.
"""

from __future__ import annotations

import ast
from typing import Any

import pytest

from pytest_graphql import build_client
from tests.docs.blocks import EXEC, NO_EXEC, Block
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema
from tests.unit.plugin_inner import LABEL_URL, run_inner

#: ``__name__`` of the namespace an ``exec`` block runs in.
NAMESPACE_NAME = "__docs_example__"

#: The names an ``exec`` block can use without importing them, besides the
#: builtins and ``__name__``. Pages are written against this list. A block
#: imports everything else it needs, as a reader's file would.
#:
#: ``gql``: a ``GraphQLClient`` over the test schema, built with
#: ``build_client(url=..., transport=..., schema=...)``. It is new for every
#: block and closed when the block ends.
NAMESPACE_NAMES = ("gql",)


class ExampleError(AssertionError):
    """A documentation block failed its check."""


def check(
    block: Block, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check one block, or raise ``ExampleError`` naming where it is."""
    if block.problems:
        raise ExampleError(f"{block.where}: " + "; ".join(block.problems))
    if not block.is_python:
        return
    if block.mode == NO_EXEC:
        _compile(block)
    elif block.mode == EXEC:
        tree = _compile(block)
        if _defines_tests(tree):
            _run_as_tests(block, pytester, monkeypatch)
        else:
            _run_in_namespace(block, tree)
    else:
        # classify() reports every other case, so this guards a new one.
        raise ExampleError(f"{block.where}: the block has no usable marker")


def _compile(block: Block) -> ast.Module:
    try:
        tree = ast.parse(block.source, filename=block.where)
    except SyntaxError as error:
        raise ExampleError(f"{block.where}: not valid Python: {error}") from error
    # The first line of source is the line after the fence, so a traceback
    # names the line of the page.
    ast.increment_lineno(tree, block.line)
    return tree


def _defines_tests(tree: ast.Module) -> bool:
    return any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )
        or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        for node in tree.body
    )


def _run_in_namespace(block: Block, tree: ast.Module) -> None:
    schema = build_schema()
    client = build_client(
        url=LABEL_URL, transport=FakeGraphQLTransport(schema), schema=schema
    )
    namespace: dict[str, Any] = {"__name__": NAMESPACE_NAME, "gql": client}
    code = compile(tree, block.where, "exec")
    try:
        exec(code, namespace)
    except (Exception, SystemExit) as error:
        raise ExampleError(
            f"{block.where}: raised {type(error).__name__}: {error}"
        ) from error
    finally:
        client.close()


def _run_as_tests(
    block: Block, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_inner(pytester, monkeypatch, test=block.source)
    outcomes = result.parseoutcomes()
    if result.ret != 0 or not outcomes.get("passed"):
        output = "\n".join(result.outlines[-40:])
        raise ExampleError(
            f"{block.where}: the tests in the block did not all pass "
            f"(exit {int(result.ret)}, {outcomes})\n{output}"
        )
