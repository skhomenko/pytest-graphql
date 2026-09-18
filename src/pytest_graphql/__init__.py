"""Schema-aware GraphQL test client with an optional pytest plugin.

The public API is re-exported from this module. Per C9, only names a user
constructs, catches, annotates or calls belong in ``__all__``; most of the
top-level surface still lands in later milestones. ``__version__`` is the
single source of the distribution version, and the release workflow checks
it against the tag.
"""

from __future__ import annotations

from pytest_graphql._core.diagnostics import DiagnosticSnapshot, RequestInfo
from pytest_graphql._core.errors import DiagnosticRenderError

__all__ = [
    "DiagnosticRenderError",
    "DiagnosticSnapshot",
    "RequestInfo",
    "__version__",
]

__version__ = "0.1.0a1.dev0"
