"""The exception tree on the Errors page matches the exception classes.

``docs/errors.md`` shows the hierarchy as a tree and lists each class in a table.
Both are read from the page and compared with the classes themselves, taken from
``pytest_graphql.__all__`` and their ``__bases__``. There is no second list in
this file, so a class that is added, removed, renamed or moved fails here until
the page says the same.
"""

from __future__ import annotations

import re

import pytest_graphql
from tests.docs.blocks import ROOT, blocks_of

PAGE = ROOT / "docs" / "errors.md"
ROOT_NAME = "GraphQLTestError"

#: One line of the tree: the drawing characters, then a class name.
TREE_LINE = re.compile(r"^(?P<drawing>(?:[│ ]   )*(?:[├└]── )?)(?P<name>\w+)$")
#: A row of the table: the class name is the text of the link in the first cell.
TABLE_ROW = re.compile(
    r"^\| \[`(?P<name>\w+)`\]\(api\.md#pytest_graphql\.(?P<anchor>\w+)\) \|"
)


def classes_in_code() -> dict[str, str | None]:
    """Each exported exception class, with the name of its parent in the tree.

    The parent is the first base that is itself a ``GraphQLTestError``. A class
    such as ``GraphQLFieldError`` has other bases, ``AttributeError`` and
    ``KeyError``, which are not part of this tree.
    """
    root = pytest_graphql.GraphQLTestError
    found: dict[str, str | None] = {}
    for name in pytest_graphql.__all__:
        value = getattr(pytest_graphql, name)
        if not (isinstance(value, type) and issubclass(value, root)):
            continue
        parents = [base for base in value.__bases__ if issubclass(base, root)]
        assert len(parents) <= 1, f"{name} has two bases in the tree"
        found[name] = parents[0].__name__ if parents else None
    return found


def tree_on_page() -> dict[str, str | None]:
    """Each class of the page's tree block, with the name of its parent."""
    trees = [
        block
        for block in blocks_of(PAGE)
        if block.language == "text" and block.source.startswith(ROOT_NAME)
    ]
    assert len(trees) == 1, "the page must hold exactly one exception tree"
    found: dict[str, str | None] = {}
    stack: list[str] = []
    for line in trees[0].source.splitlines():
        match = TREE_LINE.match(line)
        assert match, f"not a line of the tree: {line!r}"
        drawing = match["drawing"]
        assert len(drawing) % 4 == 0, line
        depth = len(drawing) // 4
        assert depth <= len(stack), f"the tree jumps down a level at {line!r}"
        del stack[depth:]
        name = match["name"]
        assert name not in found, f"{name} is in the tree twice"
        found[name] = stack[-1] if stack else None
        stack.append(name)
    return found


def table_on_page() -> list[tuple[str, str]]:
    rows = []
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if match:
            rows.append((match["name"], match["anchor"]))
    return rows


def test_the_tree_is_the_hierarchy_of_the_code() -> None:
    assert tree_on_page() == classes_in_code()


def test_the_tree_holds_the_whole_hierarchy_and_not_only_a_part() -> None:
    # A page that listed too little would pass the comparison above only if the
    # code listed as little, so the size is read from the module that defines
    # the classes.
    from pytest_graphql._core import errors

    defined = {
        name
        for name, value in vars(errors).items()
        if isinstance(value, type)
        and issubclass(value, errors.GraphQLTestError)
        and value.__module__ == errors.__name__
    }
    assert set(tree_on_page()) == defined


def test_the_root_is_the_only_class_without_a_parent() -> None:
    tree = tree_on_page()
    assert [name for name, parent in tree.items() if parent is None] == [ROOT_NAME]


def test_the_table_lists_each_class_of_the_tree_once_and_links_to_its_entry() -> None:
    rows = table_on_page()
    names = [name for name, _ in rows]
    assert sorted(names) == sorted(set(names)), "a class is in the table twice"
    assert set(names) == set(tree_on_page())
    assert [(name, anchor) for name, anchor in rows if name != anchor] == []
