"""Shared state between a plugin lifecycle test and the inner run it drives.

``pytester.runpytest()`` runs the inner session in this process, so the inner
test files import this module and append to the same list the outer test
reads. The outer test also patches the close methods to append here, so one
ordered list shows when each test ran and when each resource closed.
"""

from __future__ import annotations

from typing import Any

EVENTS: list[tuple[Any, ...]] = []
