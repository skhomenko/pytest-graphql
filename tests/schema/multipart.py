"""The server side of a GraphQL multipart request, for the test servers only.

Project-authored and built on the standard library. It reads the body the way a
server that follows the GraphQL multipart request specification does: the
``operations`` part, the ``map`` part, then one part per file. Each part keeps
its raw header lines, so a test can check what arrived byte for byte.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pytest_graphql import UploadFile

_PARAM = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_CONTROL_ESCAPE = re.compile(r"%([01][0-9A-F]|22)")


@dataclass(frozen=True)
class Part:
    """One part of a multipart body, as it arrived."""

    headers: tuple[tuple[str, str], ...]
    content: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def param(self, name: str) -> str | None:
        """A parameter of ``Content-Disposition``, as written on the wire."""
        disposition = self.header("Content-Disposition") or ""
        for key, value in _PARAM.findall(disposition):
            if key == name:
                return value
        return None

    @property
    def name(self) -> str | None:
        return self.param("name")


def boundary(content_type: str) -> bytes:
    match = re.search(r"boundary=\"?([^\";]+)\"?", content_type)
    assert match is not None, "no multipart boundary"
    return match.group(1).encode("ascii")


def parse_parts(body: bytes, content_type: str) -> list[Part]:
    """Every part of ``body``, in order."""
    delimiter = b"--" + boundary(content_type)
    chunks = body.split(delimiter)
    assert chunks[-1].strip() == b"--", "the body does not end with the boundary"
    parts = []
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n")
        head, _, content = chunk[2:-2].partition(b"\r\n\r\n")
        headers = tuple(
            (key.strip(), value.strip())
            for key, _, value in (
                line.decode("utf-8").partition(":") for line in head.split(b"\r\n")
            )
        )
        parts.append(Part(headers=headers, content=content))
    return parts


def decode_filename(raw: str) -> str:
    """The filename from its wire form: HTML5 percent escapes and ``\\\\``."""
    text = _CONTROL_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), raw)
    return text.replace("\\\\", "\\")


def graphql_payload(body: bytes, content_type: str) -> dict[str, Any]:
    """The GraphQL request of a JSON or multipart body, with files placed.

    A file becomes an ``UploadFile`` at every position the ``map`` names.
    """
    if not content_type.startswith("multipart/form-data"):
        payload: dict[str, Any] = json.loads(body)
        return payload
    parts = parse_parts(body, content_type)
    by_name = {part.name: part for part in parts}
    operations: dict[str, Any] = json.loads(by_name["operations"].content)
    file_map: dict[str, list[str]] = json.loads(by_name["map"].content)
    for key, paths in file_map.items():
        part = by_name[key]
        raw = part.param("filename")
        file = UploadFile(
            part.content,
            filename=decode_filename(raw or ""),
            content_type=part.header("Content-Type") or "application/octet-stream",
        )
        for path in paths:
            _place(operations, path.split("."), file)
    return operations


def _place(tree: Any, path: list[str], file: UploadFile) -> None:
    node = tree
    for segment in path[:-1]:
        node = node[int(segment)] if isinstance(node, list) else node[segment]
    last = path[-1]
    if isinstance(node, list):
        assert node[int(last)] is None
        node[int(last)] = file
    else:
        assert node[last] is None
        node[last] = file
