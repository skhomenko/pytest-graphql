"""The pytest layer.

This package is the only place in the distribution that may import pytest. It is
loaded through the ``pytest11`` entry point, which only pytest reads, so the
entry point is inert when pytest is absent.

The plugin surface lands at M5d and M9. This module declares no hooks yet.
"""

from __future__ import annotations
