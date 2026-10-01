"""Small immutable value types carried by ``GraphQLResponse`` (SPEC 3.4)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class GraphQLErrorInfo:
    """One entry of the envelope's ``errors`` array.

    ``message`` and ``extensions`` are server text and are never shown by a
    ``repr``: only their presence and the error ``code`` are. ``path`` is
    server text too, so a response's error entries carry a ``repr`` that
    was rendered once, through the scrub of the request that produced them
    (C16), and is returned as it is: a hostile server can echo a credential
    into a path as easily as into a message, and Python's own ``repr`` of a
    scrubbed segment doubles backslashes after the scrub ran.
    """

    message: str = field(repr=False)
    path: tuple[str | int, ...] | None = None
    locations: tuple[tuple[int, int], ...] | None = None
    extensions: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    #: The complete ``repr``, already through the scrub. ``None`` means no
    #: scrub was supplied, and ``repr`` is built from the fields.
    _rendered: str | None = field(default=None, repr=False, compare=False, kw_only=True)

    def __repr__(self) -> str:
        if self._rendered is not None:
            return self._rendered
        return _repr_of(self.path, self.locations)

    @property
    def code(self) -> str | None:
        """``extensions["code"]`` when it is a string, else ``None``."""
        code = self.extensions.get("code")
        return code if isinstance(code, str) else None

    @classmethod
    def from_mapping(
        cls,
        item: Mapping[str, Any],
        *,
        scrub: Callable[[str], str] | None = None,
    ) -> GraphQLErrorInfo:
        """Build from one error object. The transport has validated its shape;
        anything unexpected here degrades to ``None`` rather than raising.

        ``scrub`` is the free-form text scrub of the request that produced
        the error. When given, the ``repr`` is rendered once, through it.
        """
        message = item.get("message")
        raw_path = item.get("path")
        path = tuple(raw_path) if isinstance(raw_path, list) else None
        locations = item.get("locations")
        extensions = item.get("extensions")
        info = cls(
            message=message if isinstance(message, str) else "",
            path=path,
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
        if scrub is None:
            return info
        # Each segment is scrubbed first, so a credential becomes its labelled
        # marker; the finished text is scrubbed again as a whole, because
        # ``repr`` of a clean segment can still spell one.
        safe_path = (
            None
            if path is None
            else tuple(
                scrub(segment) if isinstance(segment, str) else segment
                for segment in path
            )
        )
        return replace(info, _rendered=scrub(_repr_of(safe_path, info.locations)))


def _repr_of(
    path: tuple[str | int, ...] | None,
    locations: tuple[tuple[int, int], ...] | None,
) -> str:
    return f"GraphQLErrorInfo(path={path!r}, locations={locations!r})"


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
