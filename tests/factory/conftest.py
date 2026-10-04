"""The factory suite is pure computation, so it gets the same socket guard as
the unit suite. The guard is imported rather than copied, so there is one
definition of "loopback only" (``docs/reference/DESIGN_DECISIONS.md``, section
10, "Test constraints").
"""

from __future__ import annotations

from tests.unit.conftest import _no_non_loopback_sockets

__all__ = ["_no_non_loopback_sockets"]
