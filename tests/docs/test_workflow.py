"""The workflow controls of DESIGN section 11, checked on the ``docs`` file.

A workflow cannot run in a unit test, so this reads the YAML. It cannot prove
that a deploy happens, but it holds each rule that decides whether one may.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.docs.blocks import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"

pytestmark = pytest.mark.skipif(
    not WORKFLOWS.is_dir(), reason="the sdist does not carry .github/"
)

PINNED = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<action>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s+v\d+(?:\.\d+)*\s*$"
)
USES = re.compile(r"^\s*(?:-\s+)?uses:")


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # YAML 1.1 reads the key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_every_action_in_every_workflow_is_pinned_to_a_commit() -> None:
    seen = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if USES.match(line):
                seen += 1
                assert PINNED.match(line), f"{path.name}:{number}: {line.strip()}"
    assert seen


def test_the_docs_workflow_triggers() -> None:
    triggers = _load("docs.yml")["on"]
    # Pushing a tag or a branch is not a trigger.
    assert set(triggers) == {"pull_request", "workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"] == {
        "workflows": ["release"],
        "types": ["completed"],
    }


def test_the_default_permission_is_a_read_of_the_contents() -> None:
    assert _load("docs.yml")["permissions"] == {"contents": "read"}


def test_only_the_deploy_job_can_write_pages_or_ask_for_an_identity_token() -> None:
    jobs = _load("docs.yml")["jobs"]
    assert set(jobs) == {"gate", "build", "deploy"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["gate"]["permissions"] == {"contents": "read", "actions": "read"}
    assert "permissions" not in jobs["build"]


def test_the_deploy_job_environment_and_dependencies() -> None:
    deploy = _load("docs.yml")["jobs"]["deploy"]
    assert deploy["environment"]["name"] == "github-pages"
    assert set(deploy["needs"]) == {"gate", "build"}
    assert deploy["if"] == "needs.gate.outputs.deploy == 'true'"
    assert deploy["concurrency"]["cancel-in-progress"] is False


def test_the_pages_actions_sit_in_the_jobs_they_belong_to() -> None:
    jobs = _load("docs.yml")["jobs"]

    def used(job: str) -> list[str]:
        return [
            step["uses"].split("@")[0] for step in jobs[job]["steps"] if "uses" in step
        ]

    assert "actions/upload-pages-artifact" in used("build")
    assert "actions/deploy-pages" not in used("build")
    assert used("deploy") == ["actions/deploy-pages"]
    upload = next(
        step
        for step in jobs["build"]["steps"]
        if step.get("uses", "").startswith("actions/upload-pages-artifact")
    )
    assert upload["if"] == "github.event_name != 'pull_request'"


def test_the_gate_asks_for_the_job_that_uploads_to_pypi() -> None:
    """The gate and ``release.yml`` agree on the name of the upload job."""
    release = _load("release.yml")
    names = [job.get("name") for job in release["jobs"].values()]
    assert names.count("upload to PyPI") == 1
    gate = (WORKFLOWS / "docs.yml").read_text(encoding="utf-8")
    assert 'select(.name == "upload to PyPI")' in gate
    assert release["on"]["push"]["tags"] == ["v*"]
