"""The harness the lifecycle report suite is built on (2.14 part 4, C29 to C40).

Reporting is driven by what a caller's exception types permit, so the tests
have to be able to refuse a write, take the record away, and put the call
inside an active handler. On CPython none of reporting's writes can be refused
by a caller's type, because they go through the ``BaseException`` descriptors
themselves, so the refusal paths are driven by injection at ``_write``,
``_record`` and ``_store`` exactly as C31 to C40 state.

Three things here are load-bearing and easy to get wrong:

- Every injection counts what it did, so a case asserts the site actually ran.
  A shape the injection never reaches would otherwise pass vacuously, which is
  worse than failing.
- Acyclicity is tested by walking both links and looking for a closing edge,
  never by trusting that a bounded walk returned distinct nodes. A bounded
  walk returns distinct nodes on a cyclic graph by construction.
- Every reporting test runs under a deadline, so a walk that fails to
  terminate fails the test rather than hanging the suite.
"""

from __future__ import annotations

import signal
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import pytest

from pytest_graphql._core import client as lifecycle

# -- building exceptions with controlled links --------------------------------


class Failure(Exception):  # noqa: N818 -- a failure, not an error class hierarchy
    """An ordinary cleanup or construction failure."""


class Interrupt(BaseException):
    """A non-``Exception`` failure, which the selection rule never downgrades."""


class NoDict(Exception):  # noqa: N818 -- named for the shape it exercises
    """An exception with no instance dictionary, so the record has nowhere to go."""

    __slots__ = ()


def set_context(exc: BaseException, context: BaseException | None) -> None:
    """Give ``exc`` a context link, leaving suppression alone."""
    exc.__context__ = context


def set_cause(exc: BaseException, cause: BaseException | None) -> None:
    """Give ``exc`` an explicit cause, as ``raise ... from cause`` would."""
    exc.__cause__ = cause


def suppress(exc: BaseException, context: BaseException | None) -> None:
    """Leave ``exc`` as ``raise ... from None`` would, over a populated slot."""
    exc.__context__ = context
    exc.__cause__ = None
    exc.__suppress_context__ = True


# -- reading the graph the caller is handed -----------------------------------
#
# These read through the same real slots reporting writes, so a test never
# asks an exception type a question the type could answer with a lie.


def links(node: BaseException) -> tuple[BaseException | None, BaseException | None]:
    cause = lifecycle._read(node, "__cause__")
    context = lifecycle._read(node, "__context__")
    return (
        cause if isinstance(cause, BaseException) else None,
        context if isinstance(context, BaseException) else None,
    )


def reachable(head: BaseException) -> list[BaseException]:
    """Every exception a walk of both public links reaches, ``head`` first.

    Both links, and a suppressed one too: a suppressed link is out of the
    traceback and still in the graph the caller is handed.
    """
    out: list[BaseException] = []
    stack = [head]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        out.append(node)
        for found in links(node):
            if found is not None:
                stack.append(found)
    return out


def has_cycle(head: BaseException) -> bool:
    """Whether a walk of both links from ``head`` closes an edge.

    Looks for the closing edge itself rather than trusting that a bounded walk
    returned distinct nodes, because a bounded walk always does.
    """
    on_path: set[int] = set()
    done: set[int] = set()

    def visit(node: BaseException) -> bool:
        if id(node) in on_path:
            return True
        if id(node) in done:
            return False
        on_path.add(id(node))
        for found in links(node):
            if found is not None and visit(found):
                return True
        on_path.discard(id(node))
        done.add(id(node))
        return False

    return visit(head)


def closure(roots: Iterable[BaseException | None]) -> list[BaseException]:
    """Every exception reachable from any of ``roots``, each named once.

    Order follows the roots and then the walk, so a failure message names the
    same object in the same place on every run.
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    for root in roots:
        if root is None:
            continue
        for node in reachable(root):
            if id(node) in seen:
                continue
            seen.add(id(node))
            out.append(node)
    return out


def cycle_into(head: BaseException, target: BaseException) -> bool:
    """Whether some node reachable from ``head`` links back into ``target``."""
    for node in reachable(head):
        if node is target:
            continue
        if any(found is target for found in links(node)):
            return True
    return False


def rendered_text(exc: BaseException) -> str:
    """What ``traceback.format_exception`` prints for ``exc``.

    Called under the deadline like everything else, so a chain the standard
    library cannot finish printing fails the test instead of hanging it.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# -- the deadline -------------------------------------------------------------


class Timeout(BaseException):
    """A reporting call or a walk did not terminate inside its deadline."""


@contextmanager
def deadline(seconds: float = 10.0) -> Iterator[None]:
    """Fail rather than hang when a walk does not terminate.

    ``setitimer`` only exists on the main thread of a POSIX platform. Where it
    does not, the deadline is skipped: every walk in this design bounds itself
    with a set of visited identities, so the guard is there to catch a
    regression in that, not to make the suite pass.
    """
    if not hasattr(signal, "setitimer") or sys.platform.startswith("win"):
        yield
        return

    def fire(_signum: int, _frame: Any) -> None:
        raise Timeout("reporting did not terminate inside its deadline")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# -- injection ----------------------------------------------------------------


@dataclass
class Injection:
    """What the injected sites did, so a case can assert each one fired."""

    writes: int = 0
    refusals: list[BaseException] = field(default_factory=list)
    #: For each refusal, how many record writes had already succeeded when it
    #: was made. 9.5 permits a refusal to be omitted only when no later write
    #: could carry it, so this is what tells the two cases apart: a refusal made
    #: before the last successful record has a later write and must be reported.
    refusal_marks: list[int] = field(default_factory=list)
    records: int = 0
    records_ok: int = 0
    record_refusals: int = 0
    stores: int = 0

    @property
    def refused_count(self) -> int:
        return len(self.refusals)


@contextmanager
def injected(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refuse_writes: bool = False,
    write_ordinal: int | None = None,
    refuse_record: bool = False,
    record_ordinal: int | None = None,
    no_store: bool = False,
    refusal: Callable[[], BaseException] = lambda: Failure("refused"),
) -> Iterator[Injection]:
    """Drive reporting's own write sites.

    ``refuse_writes`` refuses every chain write. ``write_ordinal`` refuses only
    the write at that 1-based ordinal, which is how a case covers the sites
    that exist rather than a guessed list. ``no_store`` takes the record away
    entirely, and ``refuse_record`` refuses the record write while leaving the
    store reachable.
    """
    state = Injection()
    real_write = lifecycle._write
    real_record = lifecycle._record
    real_store = lifecycle._store

    def fake_write(node: BaseException, name: str, value: object) -> Any:
        state.writes += 1
        if refuse_writes or (
            write_ordinal is not None and state.writes == write_ordinal
        ):
            made = refusal()
            state.refusals.append(made)
            state.refusal_marks.append(state.records_ok)
            return made
        return real_write(node, name, value)

    def fake_record(node: BaseException, reported: Any) -> Any:
        state.records += 1
        if refuse_record or (
            record_ordinal is not None and state.records == record_ordinal
        ):
            state.record_refusals += 1
            return False, refusal()
        answer = real_record(node, reported)
        if answer[0]:
            state.records_ok += 1
        return answer

    def fake_store(node: BaseException) -> Any:
        state.stores += 1
        if no_store:
            return None
        return real_store(node)

    monkeypatch.setattr(lifecycle, "_write", fake_write)
    monkeypatch.setattr(lifecycle, "_record", fake_record)
    monkeypatch.setattr(lifecycle, "_store", fake_store)
    try:
        yield state
    finally:
        # The real ones go back here rather than at the fixture's own teardown. A
        # product runs thousands of these inside one test, and a patch that outlives
        # its own block is what the next block captures as the real function. The
        # injections would then nest, and a refusal an earlier block asked for would
        # go on refusing for the rest of the test.
        monkeypatch.setattr(lifecycle, "_write", real_write)
        monkeypatch.setattr(lifecycle, "_record", real_record)
        monkeypatch.setattr(lifecycle, "_store", real_store)


# -- running one report -------------------------------------------------------


@dataclass
class Outcome:
    """Everything one reporting call is asked about afterwards."""

    raised: BaseException
    produced: tuple[BaseException, ...]
    arrived_with: tuple[BaseException, ...]
    handler: BaseException | None
    selected: BaseException
    preserved: tuple[BaseException, ...]
    injection: Injection
    record_present: bool

    @property
    def report(self) -> tuple[BaseException, ...]:
        return lifecycle.reported_errors(self.raised)

    @property
    def roots(self) -> tuple[BaseException, ...]:
        """Every entry point the caller has into the graph the call handed back.

        The exception that left the call, and every record entry, because a
        record entry is the root of another public exception graph rather than a
        terminal observation. A record entry is often not reachable from the
        raised exception at all, so a walk that starts only there misses it.

        This is the one definition of the public graph. Every property asserted
        about that graph starts from this set, so that no two of them can end up
        disagreeing about what the caller can reach.
        """
        out: list[BaseException] = []
        seen: set[int] = set()
        for node in (self.raised, *lifecycle.reported_errors(self.raised)):
            if id(node) in seen:
                continue
            seen.add(id(node))
            out.append(node)
        return tuple(out)

    @property
    def observed(self) -> set[int]:
        """Every exception the caller can still reach from what left the call.

        The closure over ``roots``, so a whole stranded chain is not called
        observed because the object at its head happens to be listed.
        """
        return {id(node) for node in closure(self.roots)}

    @property
    def expected(self) -> set[int]:
        """Every exception that existed before the call, however deep it sat.

        Taken as the closure over the reported exception, every cleanup failure
        and the active handler, so a nested node inside one of those chains
        counts exactly as the root does.
        """
        return {id(node) for node in self.preserved}

    @property
    def lost(self) -> set[int]:
        return self.expected - self.observed

    def lost_objects(self) -> list[BaseException]:
        missing = self.lost
        return [node for node in self.preserved if id(node) in missing]

    def refusals_in_report(self) -> set[int]:
        carried = self.observed
        return {id(exc) for exc in self.injection.refusals if id(exc) in carried}

    def refusals_omitted(self) -> list[BaseException]:
        carried = self.observed
        return [exc for exc in self.injection.refusals if id(exc) not in carried]

    def refusals_omitted_too_early(self) -> list[BaseException]:
        """Omitted refusals that a later write could still have carried.

        9.5 permits reporting to leave out only the refusals it made after the
        last record the report carries. A refusal made before that record had a
        later write available, so leaving it out is a defect and not a shape.
        """
        carried = self.observed
        final = self.injection.records_ok
        return [
            exc
            for exc, mark in zip(
                self.injection.refusals, self.injection.refusal_marks, strict=True
            )
            if id(exc) not in carried and mark < final
        ]


def run_report(
    errors: Sequence[BaseException],
    preferred: BaseException,
    *,
    injection: Injection,
    handler: BaseException | None = None,
    record_present: bool = True,
) -> Outcome:
    """Call ``_report`` and collect what left, under the deadline.

    ``handler`` puts the call inside an active ``except`` block, which is the
    case the coming ``raise`` can undo: it replaces ``__context__`` on the
    exception it raises whatever that link already held.
    """
    cause, context = links(preferred)
    arrived = tuple(node for node in (cause, context) if node is not None)
    produced = (preferred, *errors)
    # Taken before the first write, because reporting is about to change the
    # links this walk follows. Everything reachable now is what the caller could
    # see before the call, which is what 2.14 part 4 says must still be there.
    preserved = tuple(closure((preferred, *errors, handler)))
    selection = lifecycle._primary(errors, preferred)

    raised: BaseException | None = None
    with deadline():
        if handler is None:
            try:
                lifecycle._report(errors, preferred)
            except BaseException as left:  # the report is the subject here
                raised = left
        else:
            try:
                raise handler
            except BaseException:
                try:
                    lifecycle._report(errors, preferred)
                except BaseException as left:  # the report is the subject here
                    raised = left
    assert raised is not None, "_report must always raise"
    return Outcome(
        raised=raised,
        produced=produced,
        arrived_with=arrived,
        handler=handler,
        selected=selection,
        preserved=preserved,
        injection=injection,
        record_present=record_present,
    )


def assert_terminates(outcome: Outcome) -> str:
    """Printing the graph ends inside the deadline and names what left.

    Every root of ``Outcome.roots`` is printed, not the raised exception alone.
    A record entry that the raised exception cannot reach is still something the
    caller prints, and a chain that hangs there hangs just as badly.

    The deadline is the assertion here: a graph the standard library cannot
    finish printing fails the test instead of hanging it. Nothing asserts that
    the walk returned distinct nodes, because ``reachable`` skips an identity it
    has already seen and would return distinct nodes on a cyclic graph too.
    Acyclicity is ``has_cycle``'s question, and it is asked where 9.7 promises
    an answer.
    """
    with deadline():
        parts = [rendered_text(root) for root in outcome.roots]
    text = "".join(parts)
    assert type(outcome.raised).__name__ in text
    return text


def assert_acyclic(outcome: Outcome) -> None:
    """No root of the public graph closes an edge.

    Asked of every root of ``Outcome.roots``. A cycle confined to a record entry
    the raised exception cannot reach is as much a cycle as one on the raised
    exception itself, and 9.7 permits neither while the record works.
    """
    for index, root in enumerate(outcome.roots):
        assert not has_cycle(root), (
            f"root {index} ({root!r}) closes an edge; "
            f"it is {'the raised exception' if index == 0 else 'a record entry'}"
        )


def selected(
    errors: Sequence[BaseException], preferred: BaseException
) -> BaseException:
    """What the selection rule picks, before reporting's own writes run."""
    return lifecycle._primary(errors, preferred)


def traceback_of(exc: BaseException) -> TracebackType | None:
    return exc.__traceback__
