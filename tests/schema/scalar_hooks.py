"""Install scalar hooks on a built scalar, on either graphql-core line.

A scalar built from SDL has the default hooks, and a test schema replaces them
after the build. graphql-core 3.2 reads ``parse_value`` and ``serialize``.
graphql-core 3.3 reads ``coerce_input_value`` and ``coerce_output_value``, and
keeps the old names only as deprecated aliases that execution no longer calls,
so setting the old names alone leaves a 3.3 schema running the default hooks.
Nothing here imports ``pytest``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def set_scalar_parser(scalar: Any, parser: Callable[[Any], Any]) -> None:
    """Make ``scalar`` parse an external input value with ``parser``."""
    scalar.parse_value = parser
    if hasattr(scalar, "coerce_input_value"):
        scalar.coerce_input_value = parser


def set_scalar_serializer(scalar: Any, serializer: Callable[[Any], Any]) -> None:
    """Make ``scalar`` serialize an internal value with ``serializer``."""
    scalar.serialize = serializer
    if hasattr(scalar, "coerce_output_value"):
        scalar.coerce_output_value = serializer
