"""The three placement products of C40, and the shapes they cross.

The products are exhaustive by intent, so they are built here as data and run
by ``test_lifecycle_report.py`` rather than expanded into pytest parameters:
the second one is 93,720 combinations, and one pytest item per combination
would spend more time in collection and reporting than in the code under test.

Each combination is a fresh set of exception objects. Nothing is reused
between runs, because reporting writes on the objects it is given and a shared
object would carry one combination's links into the next.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from tests.unit.lifecycle_harness import (
    Failure,
    set_cause,
    set_context,
    suppress,
)

#: The reported exception's own state on arrival. ``_report`` reads both of its
#: links once, before the first write, so each of these is a different question
#: about what it must not destroy.
REPORTED_STATES = ("free", "context", "cause", "suppressed")

#: Whether the call is inside an active ``except`` block, and what that block's
#: own exception carries. This is the case the coming ``raise`` can undo.
HANDLER_STATES = ("none", "plain", "with-context")

#: A loser's own chain, relative to the reported exception. ``cause-back`` is
#: the one link reporting never cuts, which is why it is the shape that can
#: cost a failure; ``context-back`` leads back through a link that can be
#: spliced out instead.
LOSER_SHAPES = ("free", "cause-back", "context-back", "own-chain", "own-chain-back")

#: C40's first product narrows the loser to the three the text names: leading
#: back through a context link, an explicit cause link, or neither.
PRODUCT_ONE_SHAPES = ("free", "cause-back", "context-back")


@dataclass
class Combination:
    """One fully built combination, ready to report."""

    reported_state: str
    handler_state: str
    record_present: bool
    loser_shapes: tuple[str, ...]
    refuse_writes: bool
    write_ordinal: int | None

    preferred: BaseException
    losers: tuple[BaseException, ...]
    handler: BaseException | None
    arrived: tuple[BaseException, ...]
    extras: tuple[BaseException, ...]

    @property
    def label(self) -> str:
        return (
            f"reported={self.reported_state} handler={self.handler_state} "
            f"record={'on' if self.record_present else 'off'} "
            f"losers={','.join(self.loser_shapes)} "
            f"refuse={'all' if self.refuse_writes else self.write_ordinal or 'none'}"
        )

    @property
    def cause_back_losers(self) -> tuple[BaseException, ...]:
        """The losers whose explicit cause leads back to the reported exception.

        This is the one shape the chain cannot make room for more than once,
        because reporting will not replace an explicit cause to free a link.
        """
        return tuple(
            loser
            for loser, shape in zip(self.losers, self.loser_shapes, strict=True)
            if shape == "cause-back"
        )


def _build_reported(state: str) -> tuple[BaseException, tuple[BaseException, ...]]:
    """The reported exception and the links it arrives carrying."""
    preferred = Failure("construction failed")
    if state == "free":
        return preferred, ()
    carried = Failure("already carried")
    if state == "context":
        set_context(preferred, carried)
    elif state == "cause":
        set_cause(preferred, carried)
    elif state == "suppressed":
        suppress(preferred, carried)
    else:  # pragma: no cover -- the state list is closed
        raise AssertionError(state)
    return preferred, (carried,)


def _build_handler(
    state: str,
) -> tuple[BaseException | None, tuple[BaseException, ...]]:
    if state == "none":
        return None, ()
    handler = Failure("unrelated outer")
    if state == "plain":
        return handler, ()
    own = Failure("handler's own context")
    set_context(handler, own)
    return handler, (own,)


def _build_loser(
    shape: str, index: int, reported: BaseException
) -> tuple[BaseException, tuple[BaseException, ...]]:
    loser = Failure(f"cleanup {index} failed")
    if shape == "free":
        return loser, ()
    if shape == "cause-back":
        set_cause(loser, reported)
        return loser, ()
    if shape == "context-back":
        set_context(loser, reported)
        return loser, ()
    middle = Failure(f"cleanup {index}'s own context")
    if shape == "own-chain":
        set_context(loser, middle)
        return loser, (middle,)
    if shape == "own-chain-back":
        # The link back is a context link, so it is one reporting can splice
        # out. That is what keeps this shape out of the permitted loss set.
        set_context(loser, middle)
        set_context(middle, reported)
        return loser, (middle,)
    raise AssertionError(shape)  # pragma: no cover -- the shape list is closed


def build(
    *,
    reported_state: str,
    handler_state: str,
    record_present: bool,
    loser_shapes: Sequence[str],
    refuse_writes: bool = False,
    write_ordinal: int | None = None,
) -> Combination:
    """One combination, with every exception object freshly made."""
    preferred, arrived = _build_reported(reported_state)
    handler, handler_extras = _build_handler(handler_state)
    losers: list[BaseException] = []
    extras: list[BaseException] = list(handler_extras)
    for index, shape in enumerate(loser_shapes, start=1):
        loser, loser_extras = _build_loser(shape, index, preferred)
        losers.append(loser)
        extras.extend(loser_extras)
    return Combination(
        reported_state=reported_state,
        handler_state=handler_state,
        record_present=record_present,
        loser_shapes=tuple(loser_shapes),
        refuse_writes=refuse_writes,
        write_ordinal=write_ordinal,
        preferred=preferred,
        losers=tuple(losers),
        handler=handler,
        arrived=arrived,
        extras=tuple(extras),
    )


def product_one() -> Iterator[Combination]:
    """One loser, with every chain write refused and with none.

    144 combinations. The permitted loss set is exactly the 36 with the record
    gone and every chain write refused.
    """
    for reported, handler, record, shape, refuse in itertools.product(
        REPORTED_STATES,
        HANDLER_STATES,
        (True, False),
        PRODUCT_ONE_SHAPES,
        (True, False),
    ):
        yield build(
            reported_state=reported,
            handler_state=handler,
            record_present=record,
            loser_shapes=(shape,),
            refuse_writes=refuse,
        )


def loser_tuples(max_losers: int) -> Iterator[tuple[str, ...]]:
    """Every ordered tuple of loser shapes, one to ``max_losers`` long."""
    for count in range(1, max_losers + 1):
        yield from itertools.product(LOSER_SHAPES, repeat=count)


def product_two() -> Iterator[Combination]:
    """One to five losers of every shape, no write refused.

    93,720 combinations: four reported states, three handler states, the
    record present and absent, and 3,905 loser tuples. It is a bound and not a
    proof. It is written over the loser count so that adding a sixth is a
    change to one number, and the loss set it asserts is what was measured at
    five.
    """
    shapes = list(loser_tuples(5))
    for reported, handler, record in itertools.product(
        REPORTED_STATES, HANDLER_STATES, (True, False)
    ):
        for tuple_ in shapes:
            yield build(
                reported_state=reported,
                handler_state=handler,
                record_present=record,
                loser_shapes=tuple_,
            )


def product_three_bases() -> Iterator[Combination]:
    """One and two losers, over the same states, with no write refused yet.

    The caller runs each of these once to count the writes that actually
    happen, then re-runs it refusing each ordinal in turn. Choosing the
    ordinal that way covers the sites that exist rather than a guessed list.
    """
    shapes = list(loser_tuples(2))
    for reported, handler, record in itertools.product(
        REPORTED_STATES, HANDLER_STATES, (True, False)
    ):
        for tuple_ in shapes:
            yield build(
                reported_state=reported,
                handler_state=handler,
                record_present=record,
                loser_shapes=tuple_,
            )
