"""What the session knows about itself: the worker id and the run id.

Both come from xdist when it is running and from this process when it is not.
They sit here, apart from the fixtures, because the report header reads the same
run id that ``unique()`` values carry, and the fixtures and the reporting module
both need it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from typing import Any, Final

import pytest

#: The run id of a session that is not distributed, chosen once. A fresh
#: ``uuid`` per call would give the header one id and ``unique()`` another.
RUN_ID: Final = pytest.StashKey[str]()


def worker_input(config: Any, key: str) -> str | None:
    """A non-empty string from the xdist worker input, or ``None``."""
    workerinput = getattr(config, "workerinput", None)
    if isinstance(workerinput, Mapping):
        value = workerinput.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def worker_id(config: Any) -> str:
    """The xdist worker id, or ``"main"`` when the run is not distributed."""
    return (
        worker_input(config, "workerid")
        or os.environ.get("PYTEST_XDIST_WORKER")
        or "main"
    )


def run_id(config: Any) -> str:
    """The id of this run. xdist gives every worker the same one.

    Outside xdist it is new for each session and the same for the whole of it,
    so ``unique()`` values differ between runs and still trace back to the run
    that made them. A config with no stash, which only a test builds, gets a
    fresh id on each call.
    """
    shared = worker_input(config, "testrunuid")
    if shared:
        return shared
    stash = getattr(config, "stash", None)
    if stash is None:
        return uuid.uuid4().hex
    found: str | None = stash.get(RUN_ID, None)
    if found is None:
        found = stash[RUN_ID] = uuid.uuid4().hex
    return found
