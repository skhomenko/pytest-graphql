"""Every contributor script keeps its own self-test passing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

SELF_TESTING_SCRIPTS = [
    "changelog_section",
    "check_artifacts",
    "check_core_purity",
    "check_publication_hygiene",
    "git_hook_checks",
    "verify_release",
]


@pytest.mark.parametrize("script", SELF_TESTING_SCRIPTS)
def test_self_test_passes(script: str) -> None:
    """A failing case is a blocking defect in the check itself."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script}.py"), "--self-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_script_with_a_self_test_is_listed() -> None:
    """A new script with a --self-test mode joins the list above."""
    found = {
        path.stem
        for path in SCRIPTS.glob("*.py")
        if "--self-test" in path.read_text(encoding="utf-8")
    }
    assert found == set(SELF_TESTING_SCRIPTS)
