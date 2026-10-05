"""What the later guide pages state about themselves is true.

Most of a guide page is checked by running its blocks (``test_examples.py``).
This file holds what a block cannot show: a statement that the page promises, a
file that is not Python, and the rules for the page that opens the package to
people who do not use pytest.
"""

from __future__ import annotations

import ast

import yaml

from pytest_graphql.plugin import options
from tests.docs.blocks import ROOT, blocks_of

DOCS = ROOT / "docs"


def _text(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


# -- Using without pytest ------------------------------------------------------


def test_the_page_opens_with_the_import_and_says_pytest_is_not_installed() -> None:
    blocks = [
        block for block in blocks_of(DOCS / "without-pytest.md") if block.is_python
    ]
    assert blocks[0].mode == "exec"
    assert blocks[0].source.strip() == "from pytest_graphql import build_client"
    text = _text("without-pytest.md")
    assert "This package does not install pytest." in text
    # It is the first thing after the opening paragraph, before any other section.
    assert text.index("This package does not install pytest.") < text.index("## ")


def test_the_unittest_example_builds_the_client_in_setupclass_and_closes_it() -> None:
    suites = [
        block
        for block in blocks_of(DOCS / "without-pytest.md")
        if block.is_python and "unittest.TestCase" in block.source
    ]
    assert len(suites) == 1
    suite = suites[0]
    # `exec` is the point: the suite runs, and its result is asserted.
    assert suite.mode == "exec"
    assert "def setUpClass(cls)" in suite.source
    assert "def tearDownClass(cls)" in suite.source
    assert "cls.client.close()" in suite.source
    assert "unittest.main(" not in suite.source, "SystemExit fails the docs runner"
    assert "result.wasSuccessful()" in suite.source
    assert "GraphQLTestCase" not in _text("without-pytest.md")


def test_no_example_on_the_page_imports_pytest() -> None:
    """The page promises that pytest is not installed, so no block may need it."""
    imported: set[str] = set()
    for block in blocks_of(DOCS / "without-pytest.md"):
        if not block.is_python:
            continue
        for node in ast.walk(ast.parse(block.source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert "pytest_graphql" in imported, "the page lost its examples"
    assert not imported & {"pytest", "_pytest"}, imported


# -- Cookbook ------------------------------------------------------------------


def test_the_cookbook_says_each_xdist_worker_loads_the_schema_once() -> None:
    assert "Each worker loads the schema once." in _text("cookbook.md")


def _workflow() -> dict[str, object]:
    blocks = [
        block for block in blocks_of(DOCS / "cookbook.md") if block.language == "yaml"
    ]
    assert len(blocks) == 1, "the cookbook shows one CI configuration"
    loaded = yaml.safe_load(blocks[0].source)
    assert isinstance(loaded, dict)
    return loaded


def test_the_ci_configuration_is_valid_yaml_with_the_parts_of_a_workflow() -> None:
    workflow = _workflow()
    # YAML 1.1 reads the key `on` as the boolean True.
    assert workflow.get("on", workflow.get(True))
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["api"]["steps"]
    assert any("pytest" in str(step.get("run", "")) for step in steps)
    assert any(
        str(step.get("uses", "")).startswith("actions/checkout@") for step in steps
    )


def test_the_ci_environment_variables_are_settings_of_the_plugin() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    names = set(jobs["api"]["env"])
    known = {option.env for option in options.OPTIONS}
    assert names, "the job sets no environment variable"
    assert names <= known, names - known


def test_the_ci_configuration_takes_the_token_from_the_secret_store() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    headers = jobs["api"]["env"]["PYTEST_GQL_HEADERS"]
    assert "${{ secrets." in headers
