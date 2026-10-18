"""``python -m pytest_graphql``: the same command as ``pytest-graphql``."""

from __future__ import annotations

import sys

from pytest_graphql._cli import main

if __name__ == "__main__":
    sys.exit(main())
