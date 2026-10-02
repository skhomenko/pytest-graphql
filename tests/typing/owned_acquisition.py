"""C25 and C26: ``_Owned`` keeps the acquisition's types under the strict check.

``_Owned`` takes the cleanup list, an acquisition callable and that
callable's own arguments through a ``ParamSpec``, so the wrapper is typed by
what it acquires and the arguments are checked against the callable. The
factory acquires the root pool and each derived transport this way, and both
must keep their real type rather than ``Any``. A wrong argument type, a
missing or unknown argument, and an acquisition whose result has no
``close()`` must each fail where the wrapper is built.

This file is checked by ``tests/typing/test_typing_fixtures.py`` and never
run. A ``# expect:`` marker names the error code mypy must report on that
line, and a ``# reveal:`` marker names the type ``reveal_type`` must show,
without module paths.
"""

from __future__ import annotations

from pytest_graphql._core.client import ClientConfig, _derive, _new_root_pool
from pytest_graphql._core.lifecycle import Closable, _Owned

cleanup: list[Closable] = []

# The factory's own acquisitions, as `build_client` makes them.
pool = _Owned(cleanup, _new_root_pool, ClientConfig())
owner = _Owned(cleanup, _derive, pool.value, own_pool=False)
reveal_type(pool)  # reveal: _Owned[HttpxTransport]
reveal_type(owner.value)  # reveal: HttpxTransport

# A wrong argument type, for a positional and for a keyword argument.
_Owned(cleanup, _derive, "not a pool")  # expect: arg-type
_Owned(cleanup, _derive, pool.value, own_pool="yes")  # expect: arg-type

# A missing required argument, and one the callable does not take.
_Owned(cleanup, _new_root_pool)  # expect: call-arg
_Owned(cleanup, _derive)  # expect: call-arg
_Owned(cleanup, _new_root_pool, ClientConfig(), extra=1)  # expect: call-arg


def _count() -> int:
    return 0


# An acquisition whose result has no `close()`, as a function and a lambda.
_Owned(cleanup, _count)  # expect: type-var
_Owned(cleanup, lambda: "no close")  # expect: type-var
