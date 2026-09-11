"""Unit test guards.

``docs/reference/DESIGN_DECISIONS.md`` section 10 states that unit tests never
open a non-loopback socket. Loopback stays allowed, so an integration test may
run a real local HTTP server. The guard is installed here rather than in
``tests/conftest.py`` so it covers unit tests only.

A socket carries a destination in two ways. A connected socket names it once, in
``connect`` or ``connect_ex``, and every later send inherits it. A connectionless
socket names it on each call, in ``sendto`` or ``sendmsg``, and never connects at
all. The guard covers both, because covering only the first leaves UDP free to
reach any host.

The guard patches this process. A test that starts a subprocess, such as the
script self-tests in ``test_scripts.py``, runs outside it, because the child
inherits no patch. Give any subprocess test that could reach the network its own
isolation, at the point where one first needs it.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class NonLoopbackSocketError(RuntimeError):
    """A unit test tried to reach something other than loopback."""


def host_of(address: Any) -> str | None:
    """Return the host part of a socket address, or None for a local address."""
    if isinstance(address, (bytes, str)):
        # An AF_UNIX path. Local by construction.
        return None
    if isinstance(address, tuple) and address:
        host = address[0]
        return host if isinstance(host, str) else str(host)
    return None


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    return host.startswith("127.") or host == "::ffff:127.0.0.1"


def check_address(address: Any) -> None:
    """Raise when ``address`` names a host that is not loopback."""
    host = host_of(address)
    if host is not None and not is_loopback(host):
        raise NonLoopbackSocketError(
            f"unit tests may not reach {host!r}; use loopback or a fake"
        )


def connect_address(args: Sequence[Any]) -> Any:
    """The address argument of ``connect`` and ``connect_ex``."""
    return args[0] if args else None


def sendto_address(args: Sequence[Any]) -> Any:
    """The address argument of ``sendto(data, address)`` or ``(data, flags, address)``.

    It is always the last positional argument, and a one-argument call is the
    connected form, which carries no address.
    """
    return args[-1] if len(args) >= 2 else None


def sendmsg_address(args: Sequence[Any]) -> Any:
    """The address argument of ``sendmsg(buffers, ancdata, flags, address)``."""
    return args[3] if len(args) >= 4 else None


def guarded(
    real: Callable[..., Any], pick: Callable[[Sequence[Any]], Any]
) -> Callable[..., Any]:
    """Wrap ``real`` so the address ``pick`` selects is checked before the call.

    Taking the real primitive as an argument is what lets the guard be tested
    against recorders, so a test can prove the refusal without sending anything.
    """

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        address = pick(args)
        if address is not None:
            check_address(address)
        return real(self, *args, **kwargs)

    return wrapper


# Every socket method that can name a destination, with the position it names it
# in. A method added here is guarded everywhere the guard is installed.
GUARDED_METHODS: tuple[tuple[str, Callable[[Sequence[Any]], Any]], ...] = (
    ("connect", connect_address),
    ("connect_ex", connect_address),
    ("sendto", sendto_address),
    ("sendmsg", sendmsg_address),
)


@pytest.fixture(autouse=True)
def _no_non_loopback_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any unit test that reaches a non-loopback address."""
    for name, pick in GUARDED_METHODS:
        real = getattr(socket.socket, name, None)
        if real is None:
            # sendmsg is absent on a platform without it. Nothing to guard.
            continue
        monkeypatch.setattr(socket.socket, name, guarded(real, pick))
    yield
