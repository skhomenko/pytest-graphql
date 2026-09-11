"""The unit-test network guard refuses every non-loopback destination.

Each case wraps a recorder instead of a real socket method, so a refusal is
proved without sending anything and an allowed call is proved by the recorder
seeing it. Two cases use a real socket object, to show that the autouse fixture
patched the methods it claims to patch. Those calls are refused before the
primitive runs, so no packet leaves this process either.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tests.unit.conftest import (
    NonLoopbackSocketError,
    connect_address,
    guarded,
    sendmsg_address,
    sendto_address,
)

PUBLIC = ("93.184.216.34", 53)
LOOPBACK = ("127.0.0.1", 53)


class Recorder:
    """Stands in for a real socket method and remembers every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, _self: Any, *args: Any) -> str:
        self.calls.append(args)
        return "sent"


ALLOWED: tuple[tuple[str, Any, tuple[Any, ...]], ...] = (
    ("connect to loopback", connect_address, (LOOPBACK,)),
    ("connect to a unix path", connect_address, ("/tmp/socket",)),
    ("sendto loopback", sendto_address, (b"x", LOOPBACK)),
    ("sendto loopback with flags", sendto_address, (b"x", 0, LOOPBACK)),
    ("sendto on a connected socket", sendto_address, (b"x",)),
    ("sendmsg to loopback", sendmsg_address, ([b"x"], [], 0, LOOPBACK)),
    ("sendmsg on a connected socket", sendmsg_address, ([b"x"],)),
    ("sendmsg with flags only", sendmsg_address, ([b"x"], [], 0)),
)

REFUSED: tuple[tuple[str, Any, tuple[Any, ...]], ...] = (
    ("connect to a public host", connect_address, (PUBLIC,)),
    ("connect to a hostname", connect_address, (("example.com", 80),)),
    ("sendto a public host", sendto_address, (b"x", PUBLIC)),
    ("sendto a public host with flags", sendto_address, (b"x", 0, PUBLIC)),
    ("sendmsg to a public host", sendmsg_address, ([b"x"], [], 0, PUBLIC)),
)


@pytest.mark.parametrize(
    ("pick", "args"),
    [case[1:] for case in ALLOWED],
    ids=[case[0] for case in ALLOWED],
)
def test_a_local_destination_reaches_the_real_method(
    pick: Any, args: tuple[Any, ...]
) -> None:
    recorder = Recorder()
    assert guarded(recorder, pick)(object(), *args) == "sent"
    assert recorder.calls == [args]


@pytest.mark.parametrize(
    ("pick", "args"),
    [case[1:] for case in REFUSED],
    ids=[case[0] for case in REFUSED],
)
def test_a_remote_destination_never_reaches_the_real_method(
    pick: Any, args: tuple[Any, ...]
) -> None:
    recorder = Recorder()
    with pytest.raises(NonLoopbackSocketError):
        guarded(recorder, pick)(object(), *args)
    assert recorder.calls == []


def test_the_fixture_guards_a_real_datagram_socket() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NonLoopbackSocketError),
    ):
        sock.sendto(b"x", PUBLIC)


def test_the_fixture_guards_a_real_stream_socket() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NonLoopbackSocketError),
    ):
        sock.connect(PUBLIC)
