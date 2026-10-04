"""The bounded text a recorded call keeps for the data it received (DESIGN section 7).

A failure report shows the data a call returned, cut to a size. Response data is
free-form and may hold a credential, and a response keeps only a digest-only
snapshot of its request, so no later code can scrub it. The text is therefore
built here, at the one moment the live request is in hand, and handed to the
recorder as finished text.

The stages follow section 7. A key that matches ``redact_variables`` shows a
marker instead of its value, whatever shape that value has. Every other key and
string passes the request's scrub, which also escapes control characters. The
cut comes last and lands on an entry boundary, so a cut never leaves part of a
secret behind. The text never exceeds the limit, which is
``max_diagnostic_bytes`` unless the caller gives another.

The shape is the JSON ``json.dumps`` writes with its default separators, with
``...`` standing in for what was left out, as SPEC 7.5 shows it::

    {"user": {"id": "123", "name": "John", ...}}

``fields`` counts every object member of the whole value, cut or not, so a
reader can see how much the text leaves out.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pytest_graphql._core.diagnostics import (
    HISTORY_WITHHELD,
    RequestInfo,
    has_history_gap,
    path_is_redacted,
    text_tools,
)

_MARKER = "[redacted]"
_ELLIPSIS = "..."
#: ``", ..."``: the most the cut itself can add.
_CUT_RESERVE = len(", ...")
#: The least room worth spending on a shortened string, so a cut value still
#: shows a few characters and the visible count.
_MIN_SHORTENED = 24


@dataclass(frozen=True)
class DataExcerpt:
    """The finished text, the whole value's member count, and whether it was cut."""

    text: str
    fields: int
    cut: bool


class _FullError(Exception):
    """The next piece does not fit. Internal: it never leaves this module."""


class _Frame:
    __slots__ = ("closer", "entries", "mark", "open_entry")

    def __init__(self, closer: str) -> None:
        self.closer = closer
        self.entries = 0
        #: ``(parts, size)`` at the start of the entry being written, so a
        #: partial entry can be taken back.
        self.mark: tuple[int, int] = (0, 0)
        self.open_entry = False


class _Writer:
    """Collects pieces and guarantees the finished text fits the limit.

    A writer that is ``cutting`` checks every piece against the limit minus what
    the cut would still need to add: ``, ...`` and one closing bracket for each
    open container. One that is not checks each piece, and each closing bracket,
    against the limit itself, so a value that fits whole is never cut.
    """

    def __init__(self, limit: int, *, cutting: bool) -> None:
        self._limit = limit
        self._cutting = cutting
        self._parts: list[str] = []
        self._size = 0
        self._stack: list[_Frame] = []
        #: True once a string was written shortened, which is a cut too.
        self.shortened = False

    def _reserve(self) -> int:
        return _CUT_RESERVE + len(self._stack) if self._cutting else 0

    def room(self) -> int:
        """Bytes a piece may still take."""
        return self._limit - self._size - self._reserve()

    def put(self, text: str) -> None:
        size = len(text.encode("utf-8"))
        if size > self.room():
            raise _FullError
        self._parts.append(text)
        self._size += size

    def open(self, opener: str, closer: str) -> None:
        self._stack.append(_Frame(closer))
        try:
            self.put(opener)
        except _FullError:
            self._stack.pop()
            raise

    def close(self) -> None:
        frame = self._stack.pop()
        # A cutting writer reserved this bracket when the frame opened.
        if not self._cutting and self._size + 1 > self._limit:
            self._stack.append(frame)
            raise _FullError
        self._parts.append(frame.closer)
        self._size += 1

    def begin_entry(self) -> None:
        frame = self._stack[-1]
        frame.mark = (len(self._parts), self._size)
        frame.open_entry = True
        if frame.entries:
            self.put(", ")

    def end_entry(self) -> None:
        frame = self._stack[-1]
        frame.open_entry = False
        frame.entries += 1

    def finish(self, full: bool) -> str:
        """The text. ``full`` means a piece did not fit, so the cut is marked."""
        if full:
            if self._stack:
                frame = self._stack[-1]
                if frame.open_entry:
                    count, size = frame.mark
                    del self._parts[count:]
                    self._size = size
                self._parts.append(_ELLIPSIS if not frame.entries else ", " + _ELLIPSIS)
            else:
                self._parts.append(_ELLIPSIS)
        for frame in reversed(self._stack):
            self._parts.append(frame.closer)
        return "".join(self._parts)


def count_fields(value: Any) -> int:
    """Every object member of ``value``, at every depth, counted without copying."""
    total = 0
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            total += len(current)
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return total


def _text_safe(text: str) -> str:
    """``text`` with any lone surrogate written out, so it always encodes."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def render_data_excerpt(
    request: RequestInfo, data: Any, limit: int | None = None
) -> DataExcerpt:
    """The bounded, scrubbed text of ``data``, which ``request`` produced."""
    cap = request.max_diagnostic_bytes if limit is None else limit
    if has_history_gap(request):
        # Data is the server's text, and the secret set is missing a credential.
        withheld = HISTORY_WITHHELD
        if len(withheld.encode("utf-8")) > max(cap, 0):
            withheld = _ELLIPSIS[: max(cap, 0)]
        return DataExcerpt(text=withheld, fields=count_fields(data), cut=True)
    sanitize, _ = text_tools(request)
    context = _Context(
        sanitize=lambda text: _text_safe(sanitize(text)),
        patterns=request.redact_variables,
        marker=json.dumps(sanitize(_MARKER)),
    )
    limit = max(cap, 0)
    # First as a whole. Only a value that does not fit is written again, with
    # room kept for the cut, so a value that fits exactly is never cut.
    writer = _Writer(limit, cutting=False)
    full = False
    try:
        _write(writer, data, (), context)
    except _FullError:
        writer = _Writer(limit, cutting=True)
        full = True
        with contextlib.suppress(_FullError):
            _write(writer, data, (), context)
    # A last pass over the finished text: the JSON escaping adds backslashes
    # that can spell a secret the scrub did not see.
    text = sanitize(writer.finish(full))
    cut = full or writer.shortened
    if len(text.encode("utf-8")) > limit:
        # Only a cut marker that does not fit a tiny limit, or a scrub that
        # lengthened the text, gets here. The limit is a hard bound.
        text, cut = _ELLIPSIS[:limit], True
    return DataExcerpt(text=text, fields=count_fields(data), cut=cut)


@dataclass(frozen=True)
class _Context:
    sanitize: Callable[[str], str]
    patterns: tuple[str, ...]
    marker: str


def _write(
    writer: _Writer, value: Any, path: tuple[str, ...], context: _Context
) -> None:
    if isinstance(value, Mapping):
        writer.open("{", "}")
        for key, sub in value.items():
            writer.begin_entry()
            writer.put(json.dumps(context.sanitize(str(key)), ensure_ascii=False))
            writer.put(": ")
            segments = (*path, str(key))
            if path_is_redacted(context.patterns, segments):
                writer.put(context.marker)
            else:
                _write(writer, sub, segments, context)
            writer.end_entry()
        writer.close()
    elif isinstance(value, (list, tuple)):
        writer.open("[", "]")
        for item in value:
            writer.begin_entry()
            _write(writer, item, path, context)
            writer.end_entry()
        writer.close()
    elif isinstance(value, str):
        _write_string(writer, context.sanitize(value))
    else:
        writer.put(json.dumps(value, ensure_ascii=False))


def _write_string(writer: _Writer, text: str) -> None:
    """One string leaf. One that cannot fit whole is shortened, with the cut stated."""
    try:
        writer.put(json.dumps(text, ensure_ascii=False))
        return
    except _FullError:
        pass
    room = writer.room()
    if room < _MIN_SHORTENED:
        raise _FullError

    def shortened(count: int) -> str:
        kept = text[:count]
        cut = len(text.encode("utf-8")) - len(kept.encode("utf-8"))
        note = f"... (truncated, {cut} byte(s) cut)"
        return json.dumps(kept + note, ensure_ascii=False)

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(shortened(middle).encode("utf-8")) <= room:
            low = middle
        else:
            high = middle - 1
    candidate = shortened(low)
    if len(candidate.encode("utf-8")) > room:
        raise _FullError
    writer.put(candidate)
    writer.shortened = True
