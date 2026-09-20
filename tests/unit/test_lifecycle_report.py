"""The three placement products of C40 as amended by C60 and C61 (2.14 part 8).

Each product is run as one test over its whole combination set rather than as one
pytest item per combination. The widest is 93,720 combinations, and an item per
combination would spend more time in collection and reporting than in the code
under test. A failure names the combination through ``Combination.label``, so the
report is no less specific than a parametrised one would be.

The contract these assert is section 9.5 of ``docs/reference/DESIGN_DECISIONS.md``.
For a caller failure, two loss shapes and no others. The second shape is asserted by
membership and by an upper bound on its size, never by identity: its members are
alike in structure, and the order the cleanup list holds them in gives that identity
no separate meaning. Losing fewer than the bound is not a defect and is not treated
as one, because a run that keeps a failure the bound would have let it drop is a
better run.
"""

from __future__ import annotations

import pytest

from pytest_graphql._core import client as lifecycle
from tests.unit.lifecycle_harness import (
    Failure,
    Interrupt,
    Outcome,
    assert_acyclic,
    assert_terminates,
    deadline,
    has_cycle,
    injected,
    reachable,
    run_report,
)
from tests.unit.lifecycle_products import (
    Combination,
    build,
    loser_tuples,
    product_one,
    product_three_bases,
    product_two,
)


def _run(
    combo: Combination, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> Outcome:
    """One combination, with the record and the write sites driven as it asks."""
    with injected(
        monkeypatch,
        refuse_writes=combo.refuse_writes,
        no_store=not combo.record_present,
        **kwargs,  # type: ignore[arg-type]
    ) as state:
        return run_report(
            combo.losers,
            combo.preferred,
            injection=state,
            handler=combo.handler,
            record_present=combo.record_present,
        )


class _Bound:
    """Product two's second-shape rule: membership and an upper bound, per C61.

    Every other loss set in these products is compared by identity and by set
    equality. This one cannot be: its members are alike in structure, and the
    order the cleanup list happens to hold them in gives that identity no
    separate meaning.
    """


#: The one marker value that selects that rule.
BOUND = _Bound()


def _first_shape_loss(combo: Combination) -> tuple[BaseException, ...]:
    """Exactly what 9.5's first loss shape permits this combination to lose.

    That shape is "every link that could carry the failure is refused, with the
    record gone", so nothing else may lose anything. What goes is every cleanup
    failure, and with it an arrival link the reported exception held on
    ``__context__``: the coming ``raise`` inside an active handler replaces that
    link, and no accepted write is left to put the old value anywhere else. An
    arrival link on ``__cause__`` stays, because ``raise`` does not touch it.
    """
    if not (combo.refuse_writes and not combo.record_present):
        return ()
    gone = list(combo.losers)
    if combo.handler is not None and combo.reported_state in ("context", "suppressed"):
        gone.extend(combo.arrived)
    return tuple(gone)


def _check(
    combo: Combination, outcome: Outcome, *, lost: tuple[BaseException, ...] | _Bound
) -> None:
    """The whole published contract for one combination, asserted in one place.

    Every combination of all three products goes through this. Splitting the
    properties across separate loops is what lets a product stay green while
    reporting selects the wrong object, strands a nested pre-existing exception
    or omits a refusal a later write could have carried, so they are asserted
    together here instead.

    ``lost`` is the exact set of objects 9.5 permits this combination to lose,
    compared by identity, or ``BOUND`` for product two's second shape.
    """
    label = combo.label

    # The selection rule. What leaves is the object ``_primary`` chose, not
    # another one like it. Reporting's own writes run after that choice and
    # never remake it.
    assert outcome.raised is outcome.selected, (
        f"{label}: {outcome.raised!r} left the call, "
        f"not the selected {outcome.selected!r}"
    )

    # Printing the graph the caller is handed ends inside the deadline.
    assert_terminates(outcome)

    if combo.record_present:
        # Everything the caller produced, and both links the reported exception
        # arrived with, are in the record. The handler's own chain is not, and
        # never was: the record covers the failures reporting was given.
        held = {id(node) for node in outcome.report}
        absent = [
            node
            for node in (combo.preferred, *combo.losers, *combo.arrived)
            if id(node) not in held
        ]
        assert not absent, (
            f"{label}: the record left out {[str(one) for one in absent]}"
        )
        # 9.7's one cyclic rule runs only while the record cannot hold the
        # report, so with a working record no acyclic input may come back
        # cyclic. Asserted by looking for the closing edge, from every root of
        # the public graph: a record entry is usually not reachable from the
        # raised exception, so a cycle there is invisible to a walk that starts
        # only at the raised exception.
        try:
            assert_acyclic(outcome)
        except AssertionError as why:
            raise AssertionError(
                f"{label}: an acyclic graph came back cyclic "
                f"with a working record: {why}"
            ) from None

    # 9.5 permits reporting to leave out only the refusals it made after the
    # last record the report carries. Anything earlier had a later write.
    early = outcome.refusals_omitted_too_early()
    assert not early, (
        f"{label}: {len(early)} refusal(s) omitted although a later write "
        f"could have carried them"
    )

    # Nothing observable before the call is missing afterwards, outside the
    # named loss set. ``lost_objects`` walks the report entries as graph roots
    # and counts every nested pre-call node, not only the ones handed in.
    missing = outcome.lost_objects()
    if isinstance(lost, _Bound):
        # With a working record the group is empty, so the same two assertions
        # say that nothing at all may be lost there.
        group = () if combo.record_present else combo.cause_back_losers
        stray = [one for one in missing if not any(one is other for other in group)]
        assert not stray, (
            f"{label}: lost outside the cause-back group: {[str(one) for one in stray]}"
        )
        assert len(missing) <= max(0, len(group) - 1), (
            f"{label}: lost {len(missing)} of a group of {len(group)}"
        )
    else:
        assert {id(one) for one in missing} == {id(one) for one in lost}, (
            f"{label}: lost {[str(one) for one in missing]}, "
            f"permitted {[str(one) for one in lost]}"
        )


# -- the net under the net ----------------------------------------------------


def test_the_shared_check_catches_a_cycle_only_a_record_entry_can_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle confined to a record entry fails ``_check``.

    The products assert their properties over the public graph, and that graph
    has more than one root: the exception that left the call, and every record
    entry. A record entry is usually not reachable from the raised exception, so
    an assertion that walks only from the raised exception cannot see a cycle
    that lives in one. This test is what keeps the two from drifting apart
    again, by building exactly that graph and requiring ``_check`` to reject it.

    Built rather than produced: no combination of the three products makes this
    graph, because the implementation does not create such a cycle. The point is
    the regression net, so the cycle is introduced here on purpose.
    """
    combo = build(
        reported_state="free",
        handler_state="none",
        record_present=True,
        loser_shapes=("cause-back",),
    )
    outcome = _run(combo, monkeypatch)

    # The combination really does produce a record entry that the raised
    # exception cannot reach. Without this the rest of the test proves nothing.
    from_raised = {id(node) for node in reachable(outcome.raised)}
    stranded = [node for node in outcome.report if id(node) not in from_raised]
    assert stranded, "this combination no longer strands a record entry"

    # It passes before the cycle is introduced.
    _check(combo, outcome, lost=BOUND)

    stranded[0].__cause__ = stranded[0]

    # The narrow walk still says the graph is fine, which is the whole defect.
    assert not has_cycle(outcome.raised)

    with pytest.raises(AssertionError, match="came back cyclic"):
        _check(combo, outcome, lost=BOUND)


# -- product 1: one loser, every write refused and none (2.14 part 8) ---------


def test_product_one_loses_only_with_the_record_gone_and_every_write_refused() -> None:
    """144 combinations, each against the whole contract.

    This is the first loss shape of 9.5: nothing is left to carry the failure,
    because every link that could was refused and the record is gone. The set is
    compared by identity, so a run that loses a different object of the same
    shape fails, and the 36 combinations that may lose something are counted so
    that a change which stops producing the shape fails too.
    """
    losing = 0
    total = 0
    with pytest.MonkeyPatch.context() as monkeypatch:
        for combo in product_one():
            total += 1
            permitted = _first_shape_loss(combo)
            if permitted:
                losing += 1
            _check(combo, _run(combo, monkeypatch), lost=permitted)
    assert total == 144
    assert losing == 36


# -- product 2: one to five losers of every shape, nothing refused ------------


def test_product_two_loses_only_inside_the_cause_back_group() -> None:
    """93,720 combinations, no write refused anywhere in it.

    The whole of the second loss shape of 9.5, asserted the way C61 states it.
    With the record present nothing may be lost at all. With the record gone the
    loss must be a subset of the cleanup failures whose explicit cause leads back
    to the reported exception, and no larger than one fewer than that group holds.

    Nothing here asserts *which* member of the group is lost. Nothing here treats
    a smaller loss as a failure either: an arrival link on the reported exception,
    or an active handler, gives the chain a link it can use, and 11,376 of these
    combinations keep a failure the bound would have let them drop.

    The rest of the contract is asserted here too, over the same single pass:
    the selection rule, that printing terminates, that the record holds every
    caller failure and both arrival links while the record is there, and 9.7's
    promise that no acyclic input comes back cyclic while it is.
    """
    total = 0
    record_present = 0
    with pytest.MonkeyPatch.context() as monkeypatch:
        for combo in product_two():
            total += 1
            if combo.record_present:
                record_present += 1
            _check(combo, _run(combo, monkeypatch), lost=BOUND)
    assert total == 93_720
    assert record_present == 46_860


# -- product 3: one refused write at a time, at the ordinals that exist -------


def test_product_three_refuses_each_write_that_actually_happens() -> None:
    """Each base is run once to count its writes, then once per ordinal.

    Choosing the ordinal from the writes a run actually makes covers the sites
    that exist rather than a guessed list. One refused write costs nothing: the
    selected failure still leaves, nothing the caller produced is lost, and the
    refusal itself is carried, so the permitted loss set of every run here is
    empty and is compared as one.
    """
    bases = 0
    runs = 0
    silent = 0
    with pytest.MonkeyPatch.context() as monkeypatch:
        for combo in product_three_bases():
            bases += 1
            counted = _run(combo, monkeypatch)
            _check(combo, counted, lost=())
            attempts = counted.injection.writes
            if attempts == 0:
                # Not every shape needs a chain write. A loser whose explicit cause
                # already points at the reported exception is reachable as it arrived,
                # and with the record carrying the report there is nothing to write.
                # There is then no ordinal to refuse.
                silent += 1
                continue
            for ordinal in range(1, attempts + 1):
                fresh = build(
                    reported_state=combo.reported_state,
                    handler_state=combo.handler_state,
                    record_present=combo.record_present,
                    loser_shapes=combo.loser_shapes,
                    write_ordinal=ordinal,
                )
                runs += 1
                outcome = _run(fresh, monkeypatch, write_ordinal=ordinal)
                assert outcome.injection.refused_count == 1, (
                    f"{fresh.label}: the refusal at ordinal {ordinal} did not fire"
                )
                _check(fresh, outcome, lost=())
                assert not outcome.refusals_omitted(), (
                    f"{fresh.label}: the refusal at ordinal {ordinal} was dropped"
                )
    assert bases == 720
    assert runs == 2_469
    assert silent == 8


def test_product_three_interrupt_always_leaves_the_call() -> None:
    """A refused write that is not an ``Exception`` becomes what leaves.

    ``_primary``'s rule applied to reporting's own writes. Run at one ordinal per
    base rather than at all of them, because the promotion is made at most once
    however many writes are refused. A base with no write site is skipped, because
    there is no write there to refuse.

    This is the one case ``_check`` does not cover, because the promotion is the
    point: what leaves is the refusal and not the object ``_primary`` chose from
    the caller's failures. Printing is still asserted to terminate.
    """
    checked = 0
    with pytest.MonkeyPatch.context() as monkeypatch:
        for combo in product_three_bases():
            if _run(combo, monkeypatch).injection.writes == 0:
                continue
            fresh = build(
                reported_state=combo.reported_state,
                handler_state=combo.handler_state,
                record_present=combo.record_present,
                loser_shapes=combo.loser_shapes,
                write_ordinal=1,
            )
            outcome = _run(
                fresh,
                monkeypatch,
                write_ordinal=1,
                refusal=lambda: Interrupt("refused"),
            )
            checked += 1
            assert isinstance(outcome.raised, Interrupt), (
                f"{fresh.label}: an interrupt was downgraded"
            )
            assert_terminates(outcome)
    assert checked == 712


# -- the cost bound of 9.6 ----------------------------------------------------


@pytest.mark.parametrize("interrupt", [False, True], ids=["ordinary", "interrupt"])
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
def test_cost_bound_against_a_surface_that_refuses_every_write(
    monkeypatch: pytest.MonkeyPatch, inside: bool, interrupt: bool
) -> None:
    """56 attempts outside an active handler and 59 inside, either refusal alike.

    9.6 states these as numbers and says a change that moves either has changed
    behavior, so they are asserted as numbers. C60 holds them by not offering the
    caller's failures to the last-resort placement on a pass where the chain has
    already refused: without that condition the bound moves to 60 and 63, and the
    ordinary and interrupt cases stop agreeing.
    """
    combo = build(
        reported_state="free",
        handler_state="plain" if inside else "none",
        record_present=False,
        loser_shapes=("free",),
        refuse_writes=True,
    )
    made: type[BaseException] = Interrupt if interrupt else Failure
    with (
        injected(
            monkeypatch,
            refuse_writes=True,
            refuse_record=True,
            refusal=lambda: made("refused"),
        ) as state,
        deadline(),
    ):
        outcome = run_report(
            combo.losers,
            combo.preferred,
            injection=state,
            handler=combo.handler,
            record_present=False,
        )
    assert state.records == 4
    assert state.writes == (55 if inside else 52)
    assert state.writes + state.records == (59 if inside else 56)
    if interrupt:
        assert isinstance(outcome.raised, Interrupt)


# -- the product shapes themselves --------------------------------------------


def test_loser_tuples_cover_every_shape_at_every_length() -> None:
    """3,905 tuples, which is the sum of five to the n for n of one to five."""
    tuples = list(loser_tuples(5))
    assert len(tuples) == 3_905
    assert len(set(tuples)) == 3_905


def test_the_report_always_raises_and_never_returns() -> None:
    """``_report`` is typed ``NoReturn``, so this is the runtime half of that."""
    with pytest.raises(Failure):
        lifecycle._report([Failure("cleanup")], Failure("construction"))
