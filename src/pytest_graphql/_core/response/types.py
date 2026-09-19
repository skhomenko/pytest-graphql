"""Small immutable value types carried by ``GraphQLResponse`` (SPEC 3.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class GraphQLErrorInfo:
    """One entry of the envelope's ``errors`` array.

    ``message`` and ``extensions`` are server text and are never shown by a
    ``repr``: only their presence and the error ``code`` are.
    """

    message: str = field(repr=False)
    path: tuple[str | int, ...] | None = None
    locations: tuple[tuple[int, int], ...] | None = None
    extensions: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )

    @property
    def code(self) -> str | None:
        """``extensions["code"]`` when it is a string, else ``None``."""
        code = self.extensions.get("code")
        return code if isinstance(code, str) else None

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> GraphQLErrorInfo:
        """Build from one error object. The transport has validated its shape;
        anything unexpected here degrades to ``None`` rather than raising."""
        message = item.get("message")
        path = item.get("path")
        locations = item.get("locations")
        extensions = item.get("extensions")
        return cls(
            message=message if isinstance(message, str) else "",
            path=tuple(path) if isinstance(path, list) else None,
            locations=(
                tuple(
                    (int(loc["line"]), int(loc["column"]))
                    for loc in locations
                    if isinstance(loc, Mapping)
                    and isinstance(loc.get("line"), int)
                    and isinstance(loc.get("column"), int)
                )
                if isinstance(locations, list)
                else None
            ),
            extensions=MappingProxyType(
                dict(extensions) if isinstance(extensions, Mapping) else {}
            ),
        )


@dataclass(frozen=True)
class HttpInfo:
    """What the transport reported about the HTTP exchange.

    ``headers`` may carry ``Set-Cookie`` values, so it is excluded from
    ``repr``.
    """

    status_code: int
    media_type: str
    url: str
    headers: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
