"""The matcher diff renderer (SPEC 7.5, "Matcher diff").

The output shows only the differing fields:

    User does not match (2 of 5 compared fields differ; 34 response fields ignored)
      first_name        'Jhon'  !=  'John'
      orders[0].total   100     !=  150
    matched: id, email, status

A "compared field" is one leaf comparison: a scalar, a helper, or a whole
list-level matcher such as ``contains``. A nested object contributes its own
fields, not itself. "Ignored" counts the fields of every compared object that
the matcher did not name. The lines are returned with the SPEC's own indent, so
the pytest hook (M9) adds only its ``assert`` line above them.

Every piece of text printed from a value goes through one primitive,
``_Printer.show``, which applies the diagnostics rules (section 7) in the order
the design fixes: scrub, escape, then truncate. A field whose path matches a
redaction pattern, or sits below one that does, prints a marker on both sides
and no detail. When a snapshot is supplied, the finished text is checked once
more against its keyed digests and refused if it still holds a secret.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pytest_graphql._core.diagnostics import (
    DEFAULT_REDACT_VARIABLES,
    DiagnosticSnapshot,
    RequestInfo,
    escape_control_characters,
    path_is_redacted,
    require_safe_rendering,
    sanitize_text,
    truncate_text,
)
from pytest_graphql._core.matching.core import (
    MatchResult,
    Mismatch,
    Path,
    Said,
    describe_expected,
    plural,
)
from pytest_graphql._core.response.node import MISSING, Node

if TYPE_CHECKING:
    pass

REDACTED = "[redacted]"
DEFAULT_MAX_VALUE_BYTES = 200
DEFAULT_MAX_LINES = 50
DEFAULT_MAX_MATCHED = 50
_VALUE_LABEL = "value"


@dataclass(frozen=True)
class RenderOptions:
    """How the diff is shown. The defaults escape control characters only.

    ``for_request`` builds the options a failing assertion should use: the
    scrub and redaction patterns of the request that produced the response.
    """

    scrub: Callable[[str], str] = escape_control_characters
    redact_paths: Sequence[str] = DEFAULT_REDACT_VARIABLES
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES
    max_lines: int = DEFAULT_MAX_LINES
    max_matched: int = DEFAULT_MAX_MATCHED
    snapshot: DiagnosticSnapshot | None = None

    @classmethod
    def for_request(cls, request: RequestInfo, **overrides: Any) -> RenderOptions:
        values: dict[str, Any] = {
            "scrub": lambda text: sanitize_text(request, text),
            "redact_paths": request.redact_variables,
            "max_value_bytes": min(
                DEFAULT_MAX_VALUE_BYTES, request.max_diagnostic_bytes
            ),
            "snapshot": request.redacted(),
        }
        values.update(overrides)
        return cls(**values)


def show_value(value: Any, options: RenderOptions | None = None) -> str:
    """One value as bounded, scrubbed, single-line text, as the diff shows it."""
    return _Printer(options if options is not None else RenderOptions()).show(value)


def render_diff(result: MatchResult, options: RenderOptions | None = None) -> list[str]:
    """The diff lines for ``result``. Empty when it matched."""
    if result.ok:
        return []
    opts = options if options is not None else RenderOptions()
    lines = _Printer(opts).lines(result)
    if opts.snapshot is not None:
        require_safe_rendering(opts.snapshot, "\n".join(lines), "matcher diff")
    return lines


class _Printer:
    def __init__(self, options: RenderOptions) -> None:
        self._o = options

    # -- text from values ---------------------------------------------------

    def show(self, value: Any) -> str:
        """One value as bounded, scrubbed, single-line text."""
        text = self._clean(self._text_of(value))
        cut_text, cut = truncate_text(text, max(self._o.max_value_bytes, 0))
        return f"{cut_text}... ({cut} bytes cut)" if cut else cut_text

    def _text_of(self, value: Any) -> str:
        if isinstance(value, Said):
            return value.text
        if value is MISSING:
            return "<missing>"
        if isinstance(value, Node):
            return _safe_repr(value)
        if isinstance(value, Mapping):
            return f"{{{plural(len(value), 'field')}}}"
        if isinstance(value, (list, tuple)):
            return f"[{plural(len(value), 'item')}]"
        if isinstance(value, str):
            # Scrub the raw text first: ``repr`` doubles backslashes and
            # escapes quotes, which can stop a secret from matching.
            return repr(self._o.scrub(value))
        return _safe_repr(value)

    def _clean(self, text: str) -> str:
        scrubbed = self._o.scrub(text)
        return scrubbed.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    # -- the diff -----------------------------------------------------------

    def lines(self, result: MatchResult) -> list[str]:
        title = (
            f"  {self._root_label(result.root)} does not match "
            f"({result.differ} of {result.compared} compared "
            f"{'field differs' if result.compared == 1 else 'fields differ'}; "
            f"{result.ignored} response {'field' if result.ignored == 1 else 'fields'}"
            " ignored)"
        )
        out = [title]
        shown = result.mismatches[: max(self._o.max_lines, 0)]
        rows = [self._row(mismatch) for mismatch in shown]
        path_width = max((len(row[0]) for row in rows), default=0) + 3
        actual_width = max((len(row[1]) for row in rows), default=0)
        for row, mismatch in zip(rows, shown, strict=True):
            path, actual, expected, redacted = row
            out.append(
                f"    {path:<{path_width}}{actual:<{actual_width}}  !=  {expected}"
            )
            if not redacted:
                out.extend(
                    f"      {self._clean(line)}"
                    for line in mismatch.detail_text(self.show)
                )
        hidden = len(result.mismatches) - len(shown) + result.dropped_mismatches
        if hidden:
            noun = "difference" if hidden == 1 else "differences"
            out.append(f"    ... {hidden} more {noun} not shown")
        matched = self._matched_line(result)
        if matched:
            out.append(matched)
        return out

    def _root_label(self, root: Any) -> str:
        type_name = getattr(root, "type_name", None)
        if isinstance(type_name, str) and type_name:
            return self._clean(type_name)
        if hasattr(root, "describe"):
            return self._clean(root.describe(self.show, 0))
        return "value"

    def _row(self, mismatch: Mismatch) -> tuple[str, str, str, bool]:
        path = self._path(mismatch.path)
        if self._redacted(mismatch.path):
            return path, REDACTED, REDACTED, True
        return (
            path,
            self.show(mismatch.actual),
            self._expected(mismatch.expected),
            False,
        )

    def _expected(self, expected: Any) -> str:
        return self._clean(describe_expected(expected, self.show))

    def _matched_line(self, result: MatchResult) -> str:
        names = [self._path(path) for path in result.matched[: self._o.max_matched]]
        if not names:
            return ""
        hidden = len(result.matched) - len(names) + result.dropped_matched
        text = ", ".join(names)
        if hidden:
            text += f", ... {hidden} more"
        return f"  matched: {text}"

    # -- paths ---------------------------------------------------------------

    def _path(self, path: Path) -> str:
        if not path:
            return _VALUE_LABEL
        text = ""
        for segment in path:
            if isinstance(segment, int):
                text += f"[{segment}]"
            else:
                label = self._clean(segment)
                text += f".{label}" if text else label
        return text

    def _redacted(self, path: Path) -> bool:
        names = [segment for segment in path if isinstance(segment, str)]
        return any(
            path_is_redacted(self._o.redact_paths, names[:end])
            for end in range(1, len(names) + 1)
        )


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"
