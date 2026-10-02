"""Reporting against refused writes: policy, sealing, crowning, the terminal case.

On CPython a caller's exception type cannot refuse any of reporting's writes,
because they go through the ``BaseException`` descriptors themselves. These
tests therefore refuse them by injection at ``_write``, ``_record``, ``_store``
and ``_load``, through ``lifecycle_harness.injected``. Each case asserts that
the site it names actually fired, so a shape the injection never reaches
fails the test instead of passing as clean.

Sections, in the order ``PLAN.md`` M5c lists them:

- the refusal policy, per C31 to C40;
- each write ``_seal`` can make, probed on its own;
- crowning;
- the terminal contract of 9.6.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from pytest_graphql._core import lifecycle
from tests.unit.lifecycle_harness import (
    Failure,
    Injection,
    Interrupt,
    Outcome,
    closure,
    deadline,
    injected,
    links,
    reachable,
    rendered_text,
    run_report,
    set_cause,
    set_context,
    suppress,
)

WriteFilter = Callable[[BaseException, str, object], bool]


@dataclass
class Seen:
    """One report, with everything read while its injection was in force."""

    outcome: Outcome
    state: Injection
    report: tuple[BaseException, ...]
    observed: list[BaseException]
    text: str

    @property
    def raised(self) -> BaseException:
        return self.outcome.raised

    def omitted(self) -> set[int]:
        found = {id(exc) for exc in self.observed}
        return {id(exc) for exc in self.state.refusals if id(exc) not in found}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    errors: Sequence[BaseException],
    preferred: BaseException,
    *,
    handler: BaseException | None = None,
    **inject: Any,
) -> Seen:
    """Report under ``inject`` and read the answer before the injection ends.

    ``reported_errors`` reads the record through the same functions the
    injection replaces, so reading it afterwards would answer a different
    question from the one the run asked.
    """
    with injected(monkeypatch, **inject) as state:
        outcome = run_report(
            errors,
            preferred,
            injection=state,
            handler=handler,
            record_present=not inject.get("no_store", False),
        )
        report = lifecycle.reported_errors(outcome.raised)
        roots = list({id(exc): exc for exc in (outcome.raised, *report)}.values())
        observed = closure(roots)
        with deadline():
            text = "".join(rendered_text(root) for root in roots)
    return Seen(outcome, state, report, observed, text)


def missing(found: Sequence[BaseException], *wanted: BaseException) -> list[str]:
    """What of ``wanted`` ``found`` does not hold, by identity."""
    ids = {id(exc) for exc in found}
    return [str(exc) for exc in wanted if id(exc) not in ids]


def _refusal(kind: str) -> Callable[[], BaseException]:
    if kind == "interrupt":
        return lambda: KeyboardInterrupt("refused")
    return lambda: Failure("refused")


# -- the refusal policy (C31 to C40) ------------------------------------------

POLICY_SITES = ("first-link", "context", "seal", "record-1", "record-2")


def _policy_injection(
    site: str, preferred: BaseException, handler: BaseException
) -> dict[str, Any]:
    """The injection that refuses ``site`` once, and nothing else."""
    if site == "record-1":
        return {"record_ordinals": {1}}
    if site == "record-2":
        # A second record exists only after a pass that refused, so the first
        # record's refusal is what makes the second one happen.
        return {"record_ordinals": {1, 2}}
    filters: dict[str, WriteFilter] = {
        # The link the coming `raise` writes, which reporting writes first.
        "first-link": lambda node, name, value: (
            node is preferred and name == "__context__" and value is handler
        ),
        "context": lambda node, name, _: (
            node is not preferred and name == "__context__"
        ),
        "seal": lambda node, name, _: node is preferred and name == "__cause__",
    }
    return {"refuse_if": filters[site], "refuse_limit": 1}


@pytest.mark.parametrize("selected", ["ordinary", "interrupt"])
@pytest.mark.parametrize("kind", ["ordinary", "interrupt"])
@pytest.mark.parametrize("unreachable", [False, True], ids=["record", "no-record"])
@pytest.mark.parametrize("site", POLICY_SITES)
def test_a_refused_write_follows_the_rank_of_9_4(
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    unreachable: bool,
    kind: str,
    selected: str,
) -> None:
    preferred: BaseException = (
        Interrupt("selected stop") if selected == "interrupt" else Failure("selected")
    )
    losers = [Failure("cleanup 1"), Failure("cleanup 2")]
    handler = Failure("handled")

    seen = _run(
        monkeypatch,
        losers,
        preferred,
        handler=handler,
        no_store=unreachable,
        refusal=_refusal(kind),
        **_policy_injection(site, preferred, handler),
    )
    state = seen.state

    # The site ran, so this case is not passing vacuously.
    if site.startswith("record"):
        assert state.record_refusals == (2 if site == "record-2" else 1)
    else:
        assert state.matched_refused == 1
    assert state.refusals

    if kind == "interrupt" and selected == "ordinary":
        # An interrupted write becomes what leaves.
        assert seen.raised is state.refusals[0]
    else:
        # An ordinary refusal never replaces the selected failure, and nothing
        # outranks a selected interrupt.
        assert seen.raised is preferred

    originals = (preferred, *losers)
    if not unreachable:
        assert not missing(seen.report, *originals)
    # Every original failure and every refusal is still reported.
    assert not missing(seen.observed, *originals, *state.refusals)


@pytest.mark.parametrize("carried", [False, True], ids=["empty", "carried"])
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
@pytest.mark.parametrize("mode", ["no-store", "no-readback"])
def test_a_record_that_is_not_carried_forces_the_peers_past_a_suppression(
    monkeypatch: pytest.MonkeyPatch, mode: str, inside: bool, carried: bool
) -> None:
    preferred = Failure("selected")
    suppress(preferred, Failure("arrived context") if carried else None)
    losers = [Failure("cleanup 1"), Failure("cleanup 2")]

    seen = _run(
        monkeypatch,
        losers,
        preferred,
        handler=Failure("handled") if inside else None,
        no_store=mode == "no-store",
        no_readback=mode == "no-readback",
    )

    if mode == "no-store":
        assert seen.state.stores > 0 and seen.state.records_ok == 0
    else:
        assert seen.state.loads_hidden > 0
    assert seen.raised is preferred
    assert not missing(lifecycle._rendered(seen.raised), *losers)
    assert not missing(seen.report, preferred, *losers)


# -- each write `_seal` can make, probed on its own ---------------------------


@dataclass
class Probe:
    preferred: BaseException
    loser: BaseException
    arrived: tuple[BaseException, ...]
    refuse_if: WriteFilter


def _probe(name: str) -> Probe:
    preferred, loser = Failure("selected"), Failure("cleanup loser")
    if name == "seal":
        return Probe(
            preferred,
            loser,
            (),
            lambda node, slot, _: node is preferred and slot == "__cause__",
        )
    if name == "splice":
        # The loser's own chain leads back to the reported exception, so the
        # seal has to take that exception out of it first.
        set_context(loser, preferred)
        return Probe(
            preferred,
            loser,
            (),
            lambda node, slot, _: node is loser and slot == "__context__",
        )
    # The merge branch: the reported exception arrives with an explicit cause,
    # which is never replaced, so the loser goes at the deep end of it.
    cause = Failure("selected's own cause")
    set_cause(preferred, cause)
    return Probe(
        preferred,
        loser,
        (cause,),
        lambda node, slot, _: node is cause and slot == "__context__",
    )


@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
@pytest.mark.parametrize("limit", [1, None], ids=["once", "for-good"])
@pytest.mark.parametrize("name", ["seal", "splice", "merge"])
def test_each_seal_write_refused_keeps_every_failure_observable(
    monkeypatch: pytest.MonkeyPatch, name: str, limit: int | None, inside: bool
) -> None:
    probe = _probe(name)
    handler = Failure("handled") if inside else None

    seen = _run(
        monkeypatch,
        [probe.loser],
        probe.preferred,
        handler=handler,
        no_store=True,
        refuse_if=probe.refuse_if,
        refuse_limit=limit,
    )

    assert seen.state.matched_refused >= 1, f"the {name} write never ran"
    assert seen.raised is probe.preferred
    wanted = (probe.preferred, probe.loser, *probe.arrived)
    assert not missing(seen.observed, *wanted)
    if handler is not None:
        assert not missing(seen.observed, handler)
    if name == "merge":
        assert links(probe.preferred)[0] is probe.arrived[0]  # still the chain head


@pytest.mark.parametrize("record", [True, False], ids=["record", "no-record"])
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
def test_the_merge_branch_keeps_a_loser_whose_cause_leads_back(
    monkeypatch: pytest.MonkeyPatch, inside: bool, record: bool
) -> None:
    """Nothing refused. This is the shape the merge branch used to drop."""
    preferred, cause = Failure("selected"), Failure("selected's own cause")
    loser = Failure("cleanup loser")
    set_cause(preferred, cause)
    set_cause(loser, preferred)

    seen = _run(
        monkeypatch,
        [loser],
        preferred,
        handler=Failure("handled") if inside else None,
        no_store=not record,
    )

    assert seen.state.refusals == []
    assert seen.raised is preferred
    assert links(preferred)[0] is cause
    assert not missing(seen.observed, loser, cause)


@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
def test_both_splice_slots_refused_still_reach_and_print_the_loser(
    monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    """C33's case: the reported exception cannot be cut out of the loser's chain."""
    preferred, loser = Failure("selected"), Failure("cleanup loser")
    set_context(loser, preferred)

    seen = _run(
        monkeypatch,
        [loser],
        preferred,
        handler=Failure("handled") if inside else None,
        no_store=True,
        refuse_if=lambda node, slot, _: (
            node is loser and slot in ("__context__", "__suppress_context__")
        ),
    )

    assert seen.state.matched_refused >= 1
    assert seen.raised is preferred
    assert not missing(seen.observed, loser)
    assert "cleanup loser" in seen.text  # and printing it terminated


# -- crowning -----------------------------------------------------------------
#
# Every case refuses the sealing write for good and takes the record away, so
# `__context__` is the only slot left and the coming `raise` overwrites it.


def _sealing(preferred: BaseException) -> WriteFilter:
    return lambda node, slot, _: node is preferred and slot == "__cause__"


def _also_refusing(first: WriteFilter, node: BaseException) -> WriteFilter:
    """``first``, and every write that could cut a link out of ``node``."""
    return lambda target, slot, value: (
        first(target, slot, value)
        or (target is node and slot in ("__context__", "__suppress_context__"))
    )


def _crowned(
    monkeypatch: pytest.MonkeyPatch,
    loser: BaseException,
    preferred: BaseException,
    handler: BaseException | None,
    refuse_if: WriteFilter | None = None,
) -> Seen:
    seen = _run(
        monkeypatch,
        [loser],
        preferred,
        handler=handler,
        no_store=True,
        refuse_if=refuse_if or _sealing(preferred),
    )
    assert seen.state.matched_refused >= 1, "the sealing write never ran"
    assert seen.raised is preferred
    return seen


def test_without_a_handler_nothing_is_crowned(monkeypatch: pytest.MonkeyPatch) -> None:
    preferred, loser = Failure("selected"), Failure("cleanup loser")

    seen = _crowned(monkeypatch, loser, preferred, None)

    # The raise leaves `__context__` alone, so the loser sits in the printed
    # chain under the reported exception rather than under anything else.
    assert not missing(lifecycle._rendered(seen.raised), loser)


def test_a_handler_that_is_the_reported_failure_is_never_put_under_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred, loser = Failure("selected"), Failure("cleanup loser")

    seen = _crowned(monkeypatch, loser, preferred, preferred)

    below = closure(found for found in links(preferred) if found is not None)
    assert all(node is not preferred for node in below)
    assert not missing(seen.observed, loser)


def test_a_handler_with_an_explicit_cause_keeps_it_and_takes_the_chain_deep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred, loser = Failure("selected"), Failure("cleanup loser")
    handler, handler_cause = Failure("handled"), Failure("handler's own cause")
    set_cause(handler, handler_cause)

    seen = _crowned(monkeypatch, loser, preferred, handler)

    assert links(handler)[0] is handler_cause  # its own link is never replaced
    assert not missing(reachable(seen.raised), handler, handler_cause, loser)


@pytest.mark.parametrize("link", ["refused-context", "explicit-cause"])
def test_a_handler_the_chain_leads_back_to_still_reaches_the_failure(
    monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    preferred, loser, handler = (
        Failure("selected"),
        Failure("cleanup loser"),
        Failure("handled"),
    )
    if link == "explicit-cause":
        set_cause(loser, handler)
        refuse_if = _sealing(preferred)
    else:
        set_context(loser, handler)
        refuse_if = _also_refusing(_sealing(preferred), loser)

    seen = _crowned(monkeypatch, loser, preferred, handler, refuse_if)

    assert not missing(reachable(seen.raised), loser, handler)
    assert "cleanup loser" in seen.text  # printed, and the print terminated


def test_a_handler_with_its_own_context_takes_the_failure_at_its_deepest_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred, loser = Failure("selected"), Failure("cleanup loser")
    handler, handler_context = Failure("handled"), Failure("handler's own context")
    set_context(handler, handler_context)
    set_cause(loser, handler)  # an uncuttable link into the handler

    seen = _crowned(monkeypatch, loser, preferred, handler)

    assert not missing(reachable(seen.raised), handler, handler_context, loser)


@pytest.mark.parametrize("refused", ["everything", "only-the-seal"])
def test_the_exhausted_shape_fixes_where_the_loss_boundary_is(
    monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    preferred, loser, handler = (
        Failure("selected"),
        Failure("cleanup loser"),
        Failure("handled"),
    )

    if refused == "everything":
        seen = _run(
            monkeypatch,
            [loser],
            preferred,
            handler=handler,
            no_store=True,
            refuse_writes=True,
        )
    else:
        seen = _crowned(monkeypatch, loser, preferred, handler)

    assert seen.raised is preferred
    assert not missing(seen.observed, preferred, handler)
    found = {id(exc) for exc in seen.observed}
    lost = [id(exc) for exc in seen.outcome.preserved if id(exc) not in found]
    if refused == "everything":
        # The loss is real, and it is this object and nothing else.
        assert lost == [id(loser)]
    else:
        assert lost == []


# -- the terminal contract (9.6) ----------------------------------------------


@pytest.mark.parametrize("kind", ["ordinary", "interrupt"])
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
@pytest.mark.parametrize("channels", ["record", "chain", "both"])
def test_the_terminal_contract_names_what_each_channel_case_leaves_out(
    monkeypatch: pytest.MonkeyPatch, channels: str, inside: bool, kind: str
) -> None:
    # One cleanup failure, which is the shape 9.6 measures its bound on.
    preferred = Failure("selected")
    losers = [Failure("cleanup")]
    handler = Failure("handled") if inside else None

    seen = _run(
        monkeypatch,
        losers,
        preferred,
        handler=handler,
        refuse_record=channels in ("record", "both"),
        refuse_writes=channels in ("chain", "both"),
        refusal=_refusal(kind),
    )
    state = seen.state

    # Reporting ended, inside the bound of 9.6, and the selection rule held.
    assert state.writes + state.records <= (59 if inside else 56)
    if kind == "interrupt":
        assert seen.raised is state.refusals[0]
        assert isinstance(seen.raised, KeyboardInterrupt)
    else:
        assert seen.raised is preferred

    # A refusal is reported by a later write than the one that caused it:
    # whatever had been refused when one record was written is in the next.
    for earlier, later in itertools.pairwise(state.record_log):
        assert not missing(later.reported, *earlier.refused_by_then)

    refusals = {id(exc): exc for exc in state.refusals}
    if channels == "record":
        # Nothing is lost, and every refusal the record made reaches the chain.
        assert state.record_refused
        assert not missing(seen.observed, *state.record_refused)
        assert seen.omitted() == set()
        assert not missing(seen.observed, preferred, *losers)
    elif channels == "chain":
        # Every caller failure is still in the record, and what is left out is
        # exactly what the last record carried does not hold, all of it made
        # by the final pass.
        assert not missing(seen.report, preferred, *losers)
        last = state.last_stored()
        assert last is not None
        expected = {key for key in refusals if key not in {id(exc) for exc in last}}
        assert seen.omitted() == expected
        final = {id(exc) for exc in state.final_pass_refusals()}
        assert seen.omitted() <= final
    else:
        # Nowhere to put anything: the report holds the exception that leaves,
        # and what an active handler's own raise linked under it.
        assert state.last_stored() is None
        expected_report = (seen.raised, *([handler] if handler is not None else []))
        assert [id(exc) for exc in seen.report] == [id(exc) for exc in expected_report]
        assert seen.omitted() == {key for key in refusals if key != id(seen.raised)}
