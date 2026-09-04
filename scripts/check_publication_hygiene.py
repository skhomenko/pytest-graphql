#!/usr/bin/env python3
"""Publication hygiene scanner.

Checks text artifacts for the prohibited characters and metadata listed in
``AGENTS.md`` under "Publication hygiene (hard requirements)".

Usage::

    python3 scripts/check_publication_hygiene.py <path> [<path> ...]
    python3 scripts/check_publication_hygiene.py --self-test

Exit codes: 0 clean, 1 findings reported, 2 usage or read error.

Stdlib only. No third-party dependency and no repository tooling, so it runs
before milestone M0 lands and on any platform with Python 3.10 or newer.

Exemptions are deliberately narrow. A missed violation is a published defect,
while a false positive costs one code span, so every ambiguous construct is
reported rather than exempted:

- Code exemptions apply to Markdown artifacts only, by file suffix. In any other
  file every line is visible text.
- A fenced block opens on a run of at least three backticks or tildes indented
  by at most three spaces, and closes only on a run of the same character that
  is at least as long. A different fence character does not close it.
- A line indented by four or more spaces is never a fence marker. CommonMark
  reads it as an indented code block, and its content is still scanned.
- A code span needs a closing backtick run of exactly the opening length.
  An unmatched or unequal run is literal text and stays scanned.
- Hidden payload characters are never exempt, code included.

Every prohibited character is written here as a code point rather than a
literal, so this file stays readable and passes its own scan.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import NamedTuple

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".mdx"}

# Hidden payload codes. Checked everywhere, code spans and fences included.
INVISIBLE = {
    chr(0x00A0): "non-breaking space (U+00A0)",
    chr(0x00AD): "soft hyphen (U+00AD)",
    chr(0x180E): "Mongolian vowel separator (U+180E)",
    chr(0x200B): "zero-width space (U+200B)",
    chr(0x200C): "zero-width non-joiner (U+200C)",
    chr(0x200D): "zero-width joiner (U+200D)",
    chr(0x2060): "word joiner (U+2060)",
    chr(0xFEFF): "byte order mark (U+FEFF)",
}

BIDI = {
    chr(0x200E): "left-to-right mark (U+200E)",
    chr(0x200F): "right-to-left mark (U+200F)",
    chr(0x202A): "left-to-right embedding (U+202A)",
    chr(0x202B): "right-to-left embedding (U+202B)",
    chr(0x202C): "pop directional formatting (U+202C)",
    chr(0x202D): "left-to-right override (U+202D)",
    chr(0x202E): "right-to-left override (U+202E)",
    chr(0x2066): "left-to-right isolate (U+2066)",
    chr(0x2067): "right-to-left isolate (U+2067)",
    chr(0x2068): "first strong isolate (U+2068)",
    chr(0x2069): "pop directional isolate (U+2069)",
}

# Visible-text checks. Exempt only inside Markdown code spans and fences.
TRACKING = re.compile(r"utm_[a-z_]+=|[?&](?:fbclid|gclid|msclkid|igshid|ref)=")

# Split so this file does not contain the marker it searches for and can pass
# its own scan. A Python source file gets no Markdown exemption.
HTML_COMMENT = re.compile(re.escape("<!" + "--"))

# At most three leading spaces, then three or more of one fence character.
# CommonMark treats four spaces as an indented code block, not a fence.
FENCE_OPEN = re.compile(r"^ {0,3}(?P<char>`{3,}|~{3,})(?P<info>.*)$")


class Finding(NamedTuple):
    path: str
    line: int
    column: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.code}: {self.message}"


class Fence(NamedTuple):
    char: str
    length: int


def is_tag_char(char: str) -> bool:
    """True for the Unicode tag block used to smuggle hidden instructions."""
    return 0xE0000 <= ord(char) <= 0xE007F


def open_fence(line: str) -> Fence | None:
    """Return the fence a line opens, or None."""
    match = FENCE_OPEN.match(line)
    if match is None:
        return None
    marker = match.group("char")
    info = match.group("info")
    # A backtick fence's info string may not contain a backtick.
    if marker[0] == "`" and "`" in info:
        return None
    return Fence(marker[0], len(marker))


def closes_fence(line: str, fence: Fence) -> bool:
    """True when a line closes the open fence.

    The closing run must use the same character, be at least as long as the
    opening run, and carry no info string.
    """
    match = FENCE_OPEN.match(line)
    if match is None:
        return False
    marker = match.group("char")
    if marker[0] != fence.char or len(marker) < fence.length:
        return False
    return match.group("info").strip() == ""


def mask_code_spans(line: str) -> str:
    """Blank out Markdown code spans, preserving column positions.

    A code span opens on a backtick run and closes on the next run of exactly
    the same length. An unmatched run is literal text and is left scannable.
    """
    result = list(line)
    index = 0
    length = len(line)

    while index < length:
        if line[index] != "`":
            index += 1
            continue

        start = index
        while index < length and line[index] == "`":
            index += 1
        run = index - start

        search = index
        while search < length:
            if line[search] != "`":
                search += 1
                continue
            close_start = search
            while search < length and line[search] == "`":
                search += 1
            if search - close_start == run:
                for position in range(start, search):
                    result[position] = " "
                index = search
                break
        else:
            # No closing run of equal length. The opening run is literal text.
            continue

    return "".join(result)


def scan_hidden(path: str, line: str, number: int) -> list[Finding]:
    findings: list[Finding] = []
    for index, char in enumerate(line, start=1):
        if char in INVISIBLE:
            findings.append(Finding(path, number, index, "invisible", INVISIBLE[char]))
        elif char in BIDI:
            findings.append(Finding(path, number, index, "bidi", BIDI[char]))
        elif is_tag_char(char):
            findings.append(
                Finding(
                    path,
                    number,
                    index,
                    "tag-char",
                    f"Unicode tag character (U+{ord(char):04X})",
                )
            )
        elif unicodedata.category(char) == "Cf":
            findings.append(
                Finding(
                    path,
                    number,
                    index,
                    "format-char",
                    f"Unicode format character (U+{ord(char):04X})",
                )
            )
    return findings


def in_numeric_range(text: str, position: int) -> bool:
    """True when the dash at ``position`` sits between two digits."""
    before = text[position - 1] if position > 0 else ""
    after = text[position + 1] if position + 1 < len(text) else ""
    return before.isdigit() and after.isdigit()


def scan_visible(path: str, visible: str, number: int) -> list[Finding]:
    findings: list[Finding] = []

    for index, char in enumerate(visible):
        if char == EM_DASH:
            findings.append(
                Finding(path, number, index + 1, "em-dash", "em dash (U+2014)")
            )
        elif char == EN_DASH and not in_numeric_range(visible, index):
            findings.append(
                Finding(
                    path,
                    number,
                    index + 1,
                    "en-dash",
                    "en dash (U+2013) used as punctuation",
                )
            )

    for match in HTML_COMMENT.finditer(visible):
        findings.append(
            Finding(path, number, match.start() + 1, "html-comment", "HTML comment")
        )

    for match in TRACKING.finditer(visible):
        findings.append(
            Finding(
                path,
                number,
                match.start() + 1,
                "tracking",
                f"URL tracking parameter {match.group(0)!r}",
            )
        )

    return findings


def scan_text(path: str, text: str, *, markdown: bool) -> list[Finding]:
    findings: list[Finding] = []
    fence: Fence | None = None

    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_hidden(path, line, number))

        if not markdown:
            findings.extend(scan_visible(path, line, number))
            continue

        if fence is not None:
            if closes_fence(line, fence):
                fence = None
            continue

        opened = open_fence(line)
        if opened is not None:
            fence = opened
            continue

        findings.extend(scan_visible(path, mask_code_spans(line), number))

    return findings


def scan_path(path: Path) -> list[Finding]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        return []  # binary, not a text artifact
    markdown = path.suffix.lower() in MARKDOWN_SUFFIXES
    return scan_text(str(path), raw.decode("utf-8", errors="replace"), markdown=markdown)


SELF_TEST_CASES: list[tuple[str, str, bool, list[str]]] = [
    ("plain em dash", f"A sentence {EM_DASH} here.", True, ["em-dash"]),
    ("code span exempt", f"Quoted `{EM_DASH}` token.", True, []),
    ("fenced block exempt", f"```\n{EM_DASH}\n```", True, []),
    (
        "mismatched fence characters do not close",
        f"```\ncode\n~~~\n{EM_DASH}\n```",
        True,
        [],
    ),
    (
        "tilde fence is not closed by backticks",
        f"~~~\ncode\n```\n{EM_DASH}\n~~~",
        True,
        [],
    ),
    (
        "backtick fence reopened after a tilde marker leaves text visible",
        f"```\n~~~\n```\n{EM_DASH} visible",
        True,
        ["em-dash"],
    ),
    (
        "tilde fence reopened after a backtick marker leaves text visible",
        f"~~~\n```\n~~~\n{EM_DASH} visible",
        True,
        ["em-dash"],
    ),
    (
        "four-space indent is not a fence marker",
        f"    ```\n{EM_DASH} visible\n",
        True,
        ["em-dash"],
    ),
    ("three-space indent is a fence marker", f"   ```\n{EM_DASH}\n   ```", True, []),
    (
        "shorter closing run does not close",
        f"````\ncode\n```\n{EM_DASH}\n````",
        True,
        [],
    ),
    ("longer closing run closes", f"```\ncode\n````\n{EM_DASH}", True, ["em-dash"]),
    ("unequal backtick runs are literal", f"``a` {EM_DASH} b", True, ["em-dash"]),
    ("unmatched backtick run is literal", f"a ` b {EM_DASH} c", True, ["em-dash"]),
    ("backticks give no exemption outside markdown", f"`{EM_DASH}`", False, ["em-dash"]),
    ("fences give no exemption outside markdown", f"```\n{EM_DASH}\n```", False, ["em-dash"]),
    (
        "hidden character inside a fence is still reported",
        f"```\nx{chr(0x200B)}y\n```",
        True,
        ["invisible"],
    ),
    (
        "hidden character inside a code span is still reported",
        f"`x{chr(0x200B)}y`",
        True,
        ["invisible"],
    ),
    ("numeric range en dash is allowed", f"2010{EN_DASH}2020", True, []),
    ("punctuation en dash is reported", f"a {EN_DASH} b", True, ["en-dash"]),
    ("bidi override", f"a{chr(0x202E)}b", True, ["bidi"]),
    ("tag character", f"a{chr(0xE0041)}b", True, ["tag-char"]),
    ("non-breaking space", f"a{chr(0x00A0)}b", True, ["invisible"]),
    ("html comment", "<!" + "-- hidden -->", True, ["html-comment"]),
    # Assembled from parts so this file does not contain the tokens it searches
    # for and can pass its own scan.
    ("tracking parameter", "https://example.com/?" + "utm" + "_source=x", True, ["tracking"]),
    ("fbclid parameter", "https://example.com/?" + "fbclid" + "=1", True, ["tracking"]),
    ("clean markdown", "A clean line.\n\n- bullet\n", True, []),
]


def self_test() -> int:
    failures = 0
    for name, text, markdown, expected in SELF_TEST_CASES:
        found = sorted(f.code for f in scan_text("<case>", text, markdown=markdown))
        if found != sorted(expected):
            failures += 1
            print(f"FAIL {name}: expected {sorted(expected)}, got {found}")

    total = len(SELF_TEST_CASES)
    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1

    print(f"self-test: {total} of {total} cases passed.")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[0] == "--self-test":
        if len(argv) > 1:
            print("error: --self-test takes no paths", file=sys.stderr)
            return 2
        return self_test()

    findings: list[Finding] = []
    for name in argv:
        path = Path(name)
        if not path.is_file():
            print(f"error: not a readable file: {name}", file=sys.stderr)
            return 2
        findings.extend(scan_path(path))

    for finding in findings:
        print(finding.render())

    if findings:
        print(f"\n{len(findings)} finding(s) in {len(argv)} file(s).")
        return 1

    print(f"clean: {len(argv)} file(s) scanned, no findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
