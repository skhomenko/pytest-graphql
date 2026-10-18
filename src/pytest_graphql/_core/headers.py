"""Header identity, and the one primitive the C4 precedence chain is built from.

C4 states the precedence order, lowest to highest: ``ClientConfig.headers``,
the ``gql_headers`` fixture, ``Auth.apply``, ``with_headers()`` in clone order,
then per-call ``headers=``. It also states how two sources combine: names
compare case-insensitively after stripping, and a later source replaces an
earlier one for the same name instead of adding a second line.

Every site that combines headers goes through :func:`merge_headers`, so the
comparison rule has one implementation. A site that re-implemented it with an
ordinary ``dict`` update would compare names case-sensitively and emit
``Authorization`` beside ``authorization``, which is the defect the rule
exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def normalize_name(name: str) -> str:
    """The comparison form of a header name: stripped, then lower-cased (C4)."""
    return name.strip().lower()


def merge_headers(*sources: Mapping[str, str] | None) -> dict[str, str]:
    """Combine header sources in precedence order, lowest first (C4).

    A later source replaces an earlier one for the same name and takes that
    later source's own spelling with it. The entry keeps the position the
    name first occupied, so adding a clone's header does not reorder the
    ones already there. ``None`` is an absent source and contributes nothing.
    """
    entries: dict[str, tuple[str, str]] = {}
    for source in sources:
        if source is None:
            continue
        for name, value in source.items():
            entries[normalize_name(name)] = (name, value)
    return dict(entries.values())


def parse_header_lines(text: str) -> dict[str, str]:
    """``Name: value`` lines, the format of ``gql_headers`` and ``PYTEST_GQL_HEADERS``.

    A later line replaces an earlier one of that name. A line that is not of the
    form, or a value with a control character, raises ``ValueError``. The message
    says what is accepted and which line, and never shows the line, because a
    header value can be a credential.
    """
    found: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines, start=1):
        name, separator, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or not HEADER_NAME.fullmatch(name):
            raise ValueError(f"lines of the form Name: value (line {index} is not)")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise ValueError(f"header values without control characters (line {index})")
        found[name] = value
    return found
