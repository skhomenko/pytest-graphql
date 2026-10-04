"""Every contributor script keeps its own self-test passing.

The no-pytest check has no self-test, because what it checks is the
installed package. It runs here against the development install instead.
"""

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


def test_the_no_pytest_check_builds_and_calls_a_client(tmp_path: Path) -> None:
    """C41: the check passes here, and its client half ran.

    pytest is installed in this environment, so the check is told to allow
    that and keeps its ``sys.modules`` half. It runs from outside the source
    tree, as CI runs it, so the installed package is the one imported.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_no_pytest.py"),
            "--allow-pytest-installed",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "built a client on a fake transport" in result.stdout


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.1.0a1", "true"),
        ("v0.1.0b1", "true"),
        ("v0.1.0rc1", "true"),
        ("v0.1.0.dev0", "true"),
        ("v0.1.0", "false"),
        ("v0.1.0.post1", "false"),
    ],
)
def test_the_release_job_reads_the_prerelease_flag(tag: str, expected: str) -> None:
    """The release workflow passes exactly this output to gh release create."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_artifacts.py"),
            "--tag",
            tag,
            "--print-prerelease",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"


def test_a_tag_that_is_not_a_version_stops_the_release_job() -> None:
    """The step fails, so no release is created under a guessed flag."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_artifacts.py"),
            "--tag",
            "vlatest",
            "--print-prerelease",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "not a PEP 440 version" in result.stderr
