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

from collections.abc import Mapping


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
