"""Source-level guards for cross-version stability (risk 2 of M7).

The determinism promise holds on any supported Python only if nothing that
reaches output depends on the interpreter: no `random` module, no builtin
`hash()` (salted per process), no iteration over a set (order follows that
salted hash), and no `int.to_bytes` or `from_bytes` without an explicit byte
order (Python 3.10 has no default). A scan cannot prove stability, so this is
a tripwire next to the golden vectors, which are the real check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FACTORY = (
    Path(__file__).resolve().parents[2] / "src" / "pytest_graphql" / "_core" / "factory"
)
MODULES = sorted(FACTORY.glob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_factory_package_has_modules_to_scan() -> None:
    names = {path.name for path in MODULES}
    assert {"rng.py", "seed.py", "scalars.py", "unique.py", "generate.py"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_the_random_module(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "random" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "random"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_calls_the_builtin_hash(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "hash", f"{path.name}:{node.lineno}"


def _is_set_expression(node: ast.AST) -> bool:
    if isinstance(node, (ast.Set, ast.SetComp)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"set", "frozenset"}
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_iterates_a_set(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            assert not _is_set_expression(node.iter), f"{path.name}:{node.lineno}"
        if isinstance(node, ast.comprehension):
            assert not _is_set_expression(node.iter), f"{path.name}:{node.iter.lineno}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_byte_conversion_names_its_byte_order(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in {"to_bytes", "from_bytes"}:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        assert len(node.args) >= 2 or "byteorder" in keywords, (
            f"{path.name}:{node.lineno} leaves byteorder to a default that "
            "Python 3.10 does not have"
        )


def test_the_scan_catches_what_it_is_written_to_catch() -> None:
    bad = ast.parse(
        "import random\n"
        "from random import choice\n"
        "x = hash('a')\n"
        "for item in {1, 2}:\n"
        "    pass\n"
        "y = [i for i in set([1])]\n"
        "z = (5).to_bytes(8)\n"
    )
    assert any(isinstance(n, ast.Import) for n in ast.walk(bad))
    sets = [
        n
        for n in ast.walk(bad)
        if isinstance(n, (ast.For, ast.comprehension)) and _is_set_expression(n.iter)
    ]
    assert len(sets) == 2
    calls = [
        n
        for n in ast.walk(bad)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "to_bytes"
        and len(n.args) < 2
        and not n.keywords
    ]
    assert len(calls) == 1
