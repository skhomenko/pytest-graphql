"""The version is single-sourced and reaches the installed metadata."""

from __future__ import annotations

import re
from importlib.metadata import version

import pytest_graphql

# PEP 440, restricted to the forms this project uses.
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.dev\d+)?$")


def test_version_is_pep440() -> None:
    assert VERSION_PATTERN.match(pytest_graphql.__version__)


def test_installed_metadata_matches_the_module() -> None:
    assert version("pytest-graphql") == pytest_graphql.__version__
