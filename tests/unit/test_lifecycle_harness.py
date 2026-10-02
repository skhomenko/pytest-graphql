"""The lifecycle harness's own deadline, on every platform the suite runs on.

Every reporting test relies on ``deadline()`` to turn a walk that never ends
into a failure. A deadline that silently does nothing passes every one of
those tests, so these check that it fires, that it leaves nothing armed
behind it, and that it does not swallow a real interrupt.

The handler it installs is process-wide, so every way out of the block must
restore it: a normal exit, a timeout, a timer that fails to start, the timer
firing while the block closes, and an interrupt landing in the cleanup
itself. The last three are timing windows, so they are driven
deterministically by replacing the deadline's lock or timer with one that
interrupts at that exact step.
"""

from __future__ import annotations

import _thread
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.unit.lifecycle_harness import Deadline, Failure, Timeout, deadline


def _spin(seconds: float) -> None:
    """Run bytecode for ``seconds``, so a pending signal has a boundary to land on."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def _walk_without_a_visited_set(exc: BaseException | None) -> None:
    """The regression the deadline exists for: a walk with no identity bound.

    It gives up after five seconds, so a deadline that never fires fails the
    test that uses it rather than hanging the suite in its place.
    """
    end = time.monotonic() + 5
    while exc is not None and time.monotonic() < end:
        exc = exc.__context__


def test_the_deadline_fails_a_walk_that_never_ends() -> None:
    first, second = Failure("first"), Failure("second")
    first.__context__ = second
    second.__context__ = first
    started = time.monotonic()

    with pytest.raises(Timeout), deadline(0.2):
        _walk_without_a_visited_set(first)

    assert time.monotonic() - started < 4


def test_the_deadline_leaves_nothing_armed_after_the_block() -> None:
    before = signal.getsignal(signal.SIGINT)

    with deadline(0.05):
        pass
    _spin(0.3)  # well past the deadline: nothing may fire now

    assert signal.getsignal(signal.SIGINT) is before


def test_a_real_interrupt_inside_the_deadline_still_interrupts() -> None:
    before = signal.getsignal(signal.SIGINT)
    if not callable(before):
        pytest.skip("SIGINT has no Python handler to hand the interrupt to")

    with pytest.raises(KeyboardInterrupt), deadline(10):
        _thread.interrupt_main(signal.SIGINT)
        _spin(5)
        pytest.fail("the interrupt was never delivered")


# -- every exit restores the handler -------------------------------------------


@contextmanager
def _restores_the_handler() -> Iterator[None]:
    before = signal.getsignal(signal.SIGINT)
    yield
    assert signal.getsignal(signal.SIGINT) is before


class _InterruptsOnAcquire:
    """A lock that runs ``interrupt`` when teardown first takes it.

    Teardown takes the deadline's lock to disarm the timer, so this is the
    moment a timer firing at the boundary would interrupt the main thread.
    The inner lock is reentrant because the timer's own ``expire`` takes the
    same lock while it fires.
    """

    def __init__(self, interrupt: Callable[[], None]) -> None:
        self.interrupt: Callable[[], None] | None = interrupt
        self.inner = threading.RLock()

    def __enter__(self) -> None:
        self.inner.acquire()
        interrupt, self.interrupt = self.interrupt, None
        if interrupt is not None:
            interrupt()

    def __exit__(self, *_exc: object) -> None:
        self.inner.release()


class _CancelInterrupted:
    """The deadline's real timer, with an interrupt landing in its first cancel."""

    def __init__(self, timer: threading.Timer) -> None:
        self.timer = timer
        self.raised = False

    def cancel(self) -> None:
        if not self.raised:
            self.raised = True
            raise KeyboardInterrupt("landed in the cleanup")
        self.timer.cancel()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.timer, name)


def test_a_timer_that_fails_to_start_restores_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_timer: threading.Timer) -> None:
        raise RuntimeError("no thread for the timer")

    monkeypatch.setattr(threading.Timer, "start", refuse)
    with _restores_the_handler(), pytest.raises(RuntimeError), deadline(60):
        pytest.fail("the block ran without a timer")


def test_a_timeout_in_the_block_restores_the_handler() -> None:
    first, second = Failure("first"), Failure("second")
    first.__context__ = second
    second.__context__ = first

    with _restores_the_handler(), pytest.raises(Timeout), deadline(0.2):
        _walk_without_a_visited_set(first)


def test_the_timer_firing_while_the_block_closes_restores_then_times_out() -> None:
    with _restores_the_handler(), pytest.raises(Timeout):
        with deadline(60) as armed:
            armed.lock = _InterruptsOnAcquire(armed.expire)  # type: ignore[assignment]
        pytest.fail("the timeout that fired while closing was lost")
    assert armed.fired


def test_a_timer_that_fires_while_the_block_fails_keeps_the_failure() -> None:
    failure = Failure("the block's own failure")

    with (
        _restores_the_handler(),
        pytest.raises(Failure) as raised,
        deadline(60) as armed,
    ):
        armed.lock = _InterruptsOnAcquire(armed.expire)  # type: ignore[assignment]
        raise failure
    assert raised.value is failure
    assert armed.fired


def test_an_interrupt_in_the_cleanup_is_kept_and_the_restore_still_runs() -> None:
    with (
        _restores_the_handler(),
        pytest.raises(KeyboardInterrupt) as raised,
        deadline(60) as armed,
    ):
        armed.timer = _CancelInterrupted(armed.timer)  # type: ignore[assignment]
    assert "landed in the cleanup" in str(raised.value)


def test_a_real_interrupt_while_the_block_closes_still_interrupts() -> None:
    def interrupt() -> None:
        _thread.interrupt_main(signal.SIGINT)

    if not callable(signal.getsignal(signal.SIGINT)):
        pytest.skip("SIGINT has no Python handler to hand the interrupt to")
    with _restores_the_handler(), pytest.raises(KeyboardInterrupt):
        with deadline(60) as armed:
            armed.lock = _InterruptsOnAcquire(interrupt)  # type: ignore[assignment]
        pytest.fail("the interrupt that arrived while closing was lost")
    assert not armed.fired


def test_a_real_interrupt_while_a_failing_block_closes_outranks_the_failure() -> None:
    failure = Failure("the block's own failure")

    def interrupt() -> None:
        _thread.interrupt_main(signal.SIGINT)

    if not callable(signal.getsignal(signal.SIGINT)):
        pytest.skip("SIGINT has no Python handler to hand the interrupt to")
    with (
        _restores_the_handler(),
        pytest.raises(KeyboardInterrupt) as raised,
        deadline(60) as armed,
    ):
        armed.lock = _InterruptsOnAcquire(interrupt)  # type: ignore[assignment]
        raise failure
    assert raised.value.__cause__ is failure


def test_the_deadline_object_is_what_the_block_receives() -> None:
    with deadline(60) as armed:
        assert isinstance(armed, Deadline)
        assert armed.timer.is_alive()
    assert not armed.timer.is_alive()
