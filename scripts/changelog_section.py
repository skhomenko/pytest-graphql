#!/usr/bin/env python3
"""Extract one version's section from the changelog.

The release workflow creates a GitHub release from the changelog section for the
tag it was given, per ``docs/reference/SPEC.md`` section 12.2. This script is
that extraction, so the workflow carries no parsing logic of its own.

Usage::

    python3 scripts/changelog_section.py --version 0.1.0a1
    python3 scripts/changelog_section.py --tag v0.1.0a1 --file CHANGELOG.md
    python3 scripts/changelog_section.py --self-test

Exit codes: 0 the section was printed, 1 no such section, 2 usage or read error.

The changelog follows Keep a Changelog, so a version section starts with a level
two heading naming the version in brackets, and ends at the next level two
heading. Link reference definitions at the end of the file are not part of any
section and are dropped.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]")
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s")


def extract(text: str, version: str) -> str | None:
    """Return the changelog body for ``version``, or None when absent."""
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        if start is None and match.group("version") == version:
            start = index + 1
        elif start is not None:
            end = index
            break

    if start is None:
        return None

    body = [line for line in lines[start:end] if not LINK_DEFINITION.match(line)]
    return "\n".join(body).strip("\n")


_SAMPLE = """\
# Changelog

## [Unreleased]

Nothing yet.

## [0.1.0a1] - 2026-09-10

### Added

- The first thing.

## [0.0.1] - 2026-09-01

### Added

- An older thing.

[0.1.0a1]: https://example.invalid/compare
"""


def self_test() -> int:
    """Check extraction against the shapes the changelog actually takes."""
    failures = 0
    cases: tuple[tuple[str, str, str | None], ...] = (
        (
            "a middle section stops at the next heading",
            "0.1.0a1",
            "### Added\n\n- The first thing.",
        ),
        (
            "the last section drops link definitions",
            "0.0.1",
            "### Added\n\n- An older thing.",
        ),
        ("the unreleased section is readable", "Unreleased", "Nothing yet."),
        ("an absent version returns None", "9.9.9", None),
    )
    for name, version, expected in cases:
        actual = extract(_SAMPLE, version)
        if actual != expected:
            print(f"FAIL {name}: got {actual!r}")
            failures += 1

    total = len(cases)
    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1
    print(f"self-test: {total} of {total} cases passed.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default="CHANGELOG.md")
    parser.add_argument("--version", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if bool(args.version) == bool(args.tag):
        print("error: give exactly one of --version and --tag", file=sys.stderr)
        return 2
    version = args.version or args.tag.removeprefix("v")

    path = Path(args.file)
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    section = extract(path.read_text(encoding="utf-8"), version)
    if section is None:
        print(f"error: {path} has no section for {version}", file=sys.stderr)
        return 1

    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
