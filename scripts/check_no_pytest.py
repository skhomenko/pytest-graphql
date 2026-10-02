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

The client half (C41) builds a client on an in-process fake transport and
calls it once. The fake runs each request with graphql-core, so the build
loads its schema by introspection through ``send()``, the query goes through
selection, the call flow and response materialization, and no socket opens.
``sys.modules`` is checked again after the client is closed, because a
pytest import on any of those paths would only show up there.

The module itself imports the standard library only. ``pytest_graphql`` and
its required dependency ``graphql`` are imported inside the checks, because
they are the installation under test.

Run it with the interpreter of the environment under test, from a directory
that does not expose the source tree, so that the installed distribution is the
one imported.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

#: The schema the fake transport serves. One query with an argument, so the
#: check covers variables as well as selection.
_SDL = """
type Query {
  greeting(name: String!): Greeting!
}

type Greeting {
  name: String!
  text: String!
}
"""

#: Never contacted. The fake transport answers every request in process.
_URL = "http://localhost/graphql"


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

    failure = _check_client()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    if "pytest" in sys.modules:
        print(
            "FAIL: building and calling a client pulled pytest into sys.modules",
            file=sys.stderr,
        )
        return 1

    where = getattr(pytest_graphql, "__file__", "unknown location")
    print(f"no-pytest check passed: pytest_graphql {pytest_graphql.__version__}")
    print(f"imported from {where}")
    print("built a client on a fake transport, queried it and closed it")
    return 0


def _check_client() -> str | None:
    """Build, call and close a client; return what went wrong, or ``None``."""
    from graphql import build_schema, graphql_sync

    from pytest_graphql import RequestInfo, build_client
    from pytest_graphql._core.transport.base import RawResponse

    schema = build_schema(_SDL)

    def greeting(_info: object, name: str) -> dict[str, str]:
        return {"name": name, "text": f"hello, {name}"}

    class FakeTransport:
        """The two-method ``Transport`` protocol, executed in process."""

        def __init__(self) -> None:
            self.sent: list[RequestInfo] = []
            self.close_calls = 0

        def send(self, request: RequestInfo, *, timeout: float) -> RawResponse:  # noqa: ARG002 -- part of the Transport contract
            self.sent.append(request)
            result = graphql_sync(
                schema,
                request.document,
                root_value={"greeting": greeting},
                variable_values=dict(request.variables),
                operation_name=request.operation,
            )
            return RawResponse(
                status_code=200,
                media_type="application/graphql-response+json",
                data=result.data,
                errors=tuple(error.formatted for error in result.errors or ()),
                extensions=None,
                headers={},
            )

        def close(self) -> None:
            self.close_calls += 1

    transport = FakeTransport()
    # No schema is passed, so the build introspects through the transport.
    with build_client(url=_URL, transport=transport) as client:
        greeted: Any = client.query("greeting", name="no-pytest")
        if client.owns_transport:
            return "a client on a supplied transport claims to own it"

    if len(transport.sent) != 2:
        return f"expected an introspection and a query, sent {len(transport.sent)}"
    if (greeted.name, greeted.text) != ("no-pytest", "hello, no-pytest"):
        return f"the query returned the wrong result: {dict(greeted)!r}"
    if transport.close_calls != 0:
        return "closing the client closed the transport the caller owns"
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
