"""File uploads: the value, where it may sit, and how it is shown (DESIGN 13.2).

The client sends files by the GraphQL multipart request specification, version
2.1.0. This module owns the parts every other module shares:

- ``UploadFile``, the value a test passes. Its content is read once, when it is
  built, so a retry after a connect failure sends the same bytes again.
- ``UploadRules``, the upload scalars of one schema and the size limit, which
  the one serialization path (``serialization.py``) reads.
- ``upload_files``, the depth-first walk that finds each distinct file and the
  positions it fills. The transport, the size check and ``as_curl()`` all use
  it, so they agree on the order and on the numbering of the parts.
- ``upload_marker``, the text every rendered view shows in place of a file.
  The content never reaches it.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graphql import GraphQLScalarType, GraphQLSchema

from pytest_graphql._core.errors import ArgumentError, caller_message

#: The type of a file whose type is not given and cannot be guessed.
DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: The scalars that carry a file unless a project names others.
DEFAULT_UPLOAD_SCALARS: tuple[str, ...] = ("Upload",)

#: The default bound on the total size of the files of one request: 32 MiB.
DEFAULT_MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: One position of a file: the variable name, then field names and indexes.
UploadPath = tuple[str | int, ...]


@dataclass(frozen=True, repr=False)
class UploadFile:
    """A file to send as a GraphQL upload.

    Pass it as the value of an argument or an input field whose type is an
    upload scalar, such as `Upload`. The request then goes out as a multipart
    request by the GraphQL multipart request specification. The same object at
    two places is sent once.

    The content is held in memory, so a retry after a connect failure sends the
    same bytes. `repr()` and every report show the filename, the size and the
    type, never the content.

    `content_type` must be printable ASCII. A control character, such as a line
    break, or a character outside ASCII raises `ArgumentError`, because the type
    is written into a header line of the request.

    Examples:
        ```python {.exec}
        from pytest_graphql import UploadFile

        avatar = UploadFile(
            b"\\x89PNG...", filename="avatar.png", content_type="image/png"
        )
        assert repr(avatar) == (
            "UploadFile(filename='avatar.png', size=7, content_type='image/png')"
        )
        assert UploadFile(b"x", filename="a.bin").content_type == (
            "application/octet-stream"
        )
        ```
    """

    content: bytes
    """The bytes of the file.

    Examples:
        ```python {.exec}
        from pytest_graphql import UploadFile

        assert UploadFile(b"hello", filename="a.txt").content == b"hello"
        ```
    """
    filename: str
    """The name the server receives for the file.

    Examples:
        ```python {.exec}
        from pytest_graphql import UploadFile

        assert UploadFile(b"hello", filename="a.txt").filename == "a.txt"
        ```
    """
    content_type: str = field(default=DEFAULT_CONTENT_TYPE)
    """The media type of the file. The default is `application/octet-stream`.

    Examples:
        ```python {.exec}
        from pytest_graphql import UploadFile

        file = UploadFile(b"hello", filename="a.txt", content_type="text/plain")
        assert file.content_type == "text/plain"
        ```
    """

    def __post_init__(self) -> None:
        # Here, so that ``dataclasses.replace()`` and ``from_path`` are checked
        # too. The type is written into a header line of the multipart body,
        # and httpx writes it as it is, so a line break would add a header.
        for index, char in enumerate(self.content_type):
            if ord(char) < 0x20 or ord(char) == 0x7F:
                found = "a control character"
            elif not char.isascii():
                found = "a character outside ASCII"
            else:
                continue
            raise ArgumentError._from_message(
                caller_message(
                    f"UploadFile.content_type holds {found} at index {index}.",
                    context=[
                        "The type is written into a header line of the "
                        "multipart request."
                    ],
                    fix=(
                        "pass a media type made of printable ASCII characters, "
                        'such as "image/png"'
                    ),
                ),
                kind="call",
                operation_name="UploadFile",
                bad_name="content_type",
            )

    def __repr__(self) -> str:
        return (
            f"UploadFile(filename={self.filename!r}, size={len(self.content)}, "
            f"content_type={self.content_type!r})"
        )

    @classmethod
    def from_path(
        cls, path: str | os.PathLike[str], content_type: str | None = None
    ) -> UploadFile:
        """Read a file from disk, once, and return it as an `UploadFile`.

        The file is read when this is called. The filename is the last part of
        the path only, so `"fixtures/img/a.png"` is sent as `a.png` and no local
        directory reaches the server. With no `content_type`, the type is
        guessed from the filename with `mimetypes`, and is
        `application/octet-stream` when it cannot be guessed.

        Args:
            path: The path of the file.
            content_type: The media type, or `None` to guess it.

        Returns:
            The file, with its content in memory.

        Raises:
            OSError: When the file cannot be read.
            ArgumentError: When `content_type` is not printable ASCII.

        Examples:
            ```python {.exec}
            import tempfile
            from pathlib import Path

            from pytest_graphql import UploadFile

            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "notes.txt"
                path.write_bytes(b"hello")
                file = UploadFile.from_path(path)

            assert file.filename == "notes.txt"
            assert file.content == b"hello"
            assert file.content_type == "text/plain"
            ```
        """
        source = Path(path)
        content = source.read_bytes()
        filename = source.name
        if content_type is None:
            content_type = mimetypes.guess_type(filename)[0] or DEFAULT_CONTENT_TYPE
        return cls(content, filename=filename, content_type=content_type)


@dataclass(frozen=True)
class UploadRules:
    """What one client accepts as a file: the scalars and the size limit.

    ``scalars`` are the names ``ClientConfig.upload_scalars`` gives, and
    ``present`` the ones the schema defines, which are the only places a file
    is accepted. ``max_bytes`` bounds the total of the distinct files of one
    request, and ``None`` means no bound.
    """

    scalars: tuple[str, ...] = ()
    present: frozenset[str] = frozenset()
    max_bytes: int | None = None

    @classmethod
    def for_schema(
        cls, schema: GraphQLSchema, scalars: Iterable[str], max_bytes: int | None
    ) -> UploadRules:
        names = tuple(scalars)
        present = frozenset(
            name
            for name in names
            if isinstance(schema.type_map.get(name), GraphQLScalarType)
        )
        return cls(scalars=names, present=present, max_bytes=max_bytes)


#: A file is accepted nowhere: a caller that names no rules, such as the
#: describe command, which builds a document with no values.
NO_UPLOADS = UploadRules()


def upload_files(
    variables: Mapping[str, Any],
) -> list[tuple[UploadFile, list[UploadPath]]]:
    """Each distinct file and its positions, in depth-first order.

    The order is the order in which each file is first met, which numbers the
    parts ``"0"``, ``"1"`` and so on. The same object at two positions is one
    entry with two paths. Two equal objects are two entries, because the
    specification sends one part per object a caller passed.
    """
    found: dict[int, tuple[UploadFile, list[UploadPath]]] = {}

    def walk(value: Any, path: UploadPath) -> None:
        if isinstance(value, UploadFile):
            entry = found.setdefault(id(value), (value, []))
            entry[1].append(path)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                walk(item, (*path, str(key)))
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                walk(item, (*path, index))

    for name, value in variables.items():
        walk(value, (name,))
    return list(found.values())


def upload_bytes(files: Sequence[tuple[UploadFile, Sequence[UploadPath]]]) -> int:
    """The total size of the distinct files, each counted once."""
    return sum(len(file.content) for file, _ in files)


def upload_marker(file: UploadFile) -> str:
    """``<upload "<filename>", <n> bytes, <content type>>``: a file in a report.

    The content never enters it. The caller scrubs and escapes the text, as it
    does every other string it renders.
    """
    return f'<upload "{file.filename}", {len(file.content)} bytes, {file.content_type}>'


def map_path(path: UploadPath) -> str:
    """The ``map`` entry of one position: ``variables.files.0``."""
    return ".".join(["variables", *(str(segment) for segment in path)])
