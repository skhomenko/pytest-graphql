#!/usr/bin/env python3
"""No-pytest import check, which doubles as the release smoke test.

``pytest`` is an optional dependency. The ``pytest11`` entry point is always
declared and is inert when pytest is absent, because only pytest reads it. This
script proves both halves in an environment installed without the ``pytest``
extra: pytest is not importable, and importing ``pytest_graphql`` does not pull
it into ``sys.modules``.

Usage::

    python3 scripts/check_no_pytest.py
    python3 scripts/check_no_pytest.py --allow-pytest-installed

Exit codes: 0 clean, 1 the check failed.

Stdlib only, so it runs in a clean environment that holds the distribution and
its required dependencies and nothing else.

``--allow-pytest-installed`` keeps the ``sys.modules`` half of the check and
drops the "pytest is not installed" half, for an environment where pytest is
present on purpose.

Per C41 in the build plan this check does not build a client. No client exists
before M5c, and the client half of the check joins it there.

Run it with the interpreter of the environment under test, from a directory
that does not expose the source tree, so that the installed distribution is the
one imported.
"""

from __future__ import annotations

import importlib.util
import sys


def main(argv: list[str]) -> int:
    allow_installed = False
    for arg in argv:
        if arg == "--allow-pytest-installed":
            allow_installed = True
        else:
            print(f"error: unknown argument: {arg}", file=sys.stderr)
            return 1

    if not allow_installed and importlib.util.find_spec("pytest") is not None:
        print(
            "FAIL: pytest is installed in an environment that must not have it",
            file=sys.stderr,
        )
        return 1

    if "pytest" in sys.modules:
        print("FAIL: pytest was already imported before the check ran", file=sys.stderr)
        return 1

    import pytest_graphql

    if "pytest" in sys.modules:
        print(
            "FAIL: importing pytest_graphql pulled pytest into sys.modules",
            file=sys.stderr,
        )
        return 1

    where = getattr(pytest_graphql, "__file__", "unknown location")
    print(f"no-pytest check passed: pytest_graphql {pytest_graphql.__version__}")
    print(f"imported from {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
