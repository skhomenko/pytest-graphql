"""Reporting through the two releasing call sites a caller reaches (9.3 to 9.5).

``GraphQLClient.close()`` and the ``build_client()`` unwinding path both sweep
a cleanup list and hand the failures to one report. These tests drive real
failures through both, rather than calling the report directly, because what
the interpreter does around each site is part of the contract: the factory
reports inside the ``except`` block that caught the construction failure, and
a caller can call ``close()`` inside an ``except`` block of its own. A
``raise`` there replaces ``__context__`` on the exception it raises.

Every call runs under a deadline, so a walk that fails to terminate fails the
test rather than hanging the suite.

Sections, in the order ``PLAN.md`` M5c lists them:

- failure during cleanup, at both sites;
- reporting: cyclic failures, explicit causes and ``raise ... from None``;
- sealing, per C30;
- hostile exception types, per C31.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import client as factory
from pytest_graphql._core import lifecycle
from pytest_graphql._core.client import GraphQLClient, build_client
from pytest_graphql._core.lifecycle import REPORTED_ERRORS, reported_errors
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema
from tests.unit.lifecycle_harness import (
    Failure,
    Interrupt,
    Timeout,
    closure,
    deadline,
    injected,
    links,
    reachable,
    rendered_text,
)

URL = "https://example.test/graphql"
SITES = ("close", "factory")


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


# -- raising a failure from a cleanup item or a construction step -------------


def label(exc: BaseException) -> str:
    """The message of ``exc``, read without asking its type."""
    return BaseException.__str__(exc)


def labels(chain: Sequence[BaseException]) -> list[str]:
    return [label(exc) for exc in chain]


def thrower(
    exc: BaseException,
    *,
    cause: BaseException | None = None,
    from_none: bool = False,
    within: BaseException | None = None,
    shape: Callable[[BaseException], None] | None = None,
) -> Callable[[], NoReturn]:
    """A callable that raises ``exc`` the way a real failure would.

    ``within`` raises it from inside the handler of another exception, so the
    interpreter links that one under it. ``shape`` runs after the raise has
    written its own links and before anything catches ``exc``, which is the
    only way to hand a site a failure whose links the raise would otherwise
    overwrite, a self-referencing ``__context__`` among them.
    """

    def plain() -> NoReturn:
        if from_none:
            raise exc from None
        if cause is not None:
            raise exc from cause
        raise exc

    def throw() -> NoReturn:
        try:
            if within is None:
                plain()
            try:
                raise within
            except BaseException:  # the failure is raised from this handler
                plain()
        finally:
            if shape is not None:
                shape(exc)

    return throw


class Item:
    """A cleanup item that counts its closes and runs ``fail`` on each one."""

    def __init__(self, fail: Callable[[], NoReturn] | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def close(self) -> None:
        self.calls += 1
        if self.fail is not None:
            self.fail()


def _left(call: Callable[[], object], outer: BaseException | None) -> BaseException:
    """Run ``call`` under the deadline, inside ``outer``'s handler when given."""
    with deadline():
        try:
            if outer is None:
                call()
            else:
                try:
                    raise outer
                except BaseException:  # the call is made while this is handled
                    call()
        except Timeout:
            raise
        except BaseException as left:  # the report is the subject here
            return left
    raise AssertionError("the releasing call site returned without raising")


@dataclass
class Run:
    """One call through a site, and what is needed to ask about it afterwards."""

    raised: BaseException
    items: list[Item]
    client: GraphQLClient | None

    @property
    def roots(self) -> list[BaseException]:
        return [self.raised, *reported_errors(self.raised)]

    def observed(self) -> list[BaseException]:
        return closure(self.roots)


def run_site(
    site: str,
    monkeypatch: pytest.MonkeyPatch,
    schema: GraphQLSchema,
    primary: Callable[[], NoReturn],
    losers: Sequence[Callable[[], NoReturn] | None] = (),
    *,
    outer: BaseException | None = None,
) -> Run:
    """Drive ``primary`` and ``losers`` through one releasing site.

    At ``close`` the primary is the earliest-acquired item, which the client
    prefers among ordinary failures, and the losers are swept before it. At
    ``factory`` the primary is the construction failure and the losers are the
    supplied cleanup list. A loser of ``None`` closes cleanly.
    """
    if site == "close":
        items = [Item(primary), *(Item(fail) for fail in losers)]
        client = GraphQLClient(
            transport=FakeGraphQLTransport(schema),
            schema=schema,
            owns_transport=True,
            cleanup=list(items),
        )
        return Run(_left(client.close, outer), items, client)

    items = [Item(fail) for fail in losers]

    def root(config: Any) -> NoReturn:  # noqa: ARG001 -- the factory's own shape
        primary()

    monkeypatch.setattr(factory, "_new_root_pool", root)
    built = _left(
        lambda: build_client(url=URL, cleanup=list(items), schema=schema), outer
    )
    return Run(built, items, None)


def assert_prints(run: Run) -> str:
    """Printing every root ends inside the deadline. Returns the text."""
    with deadline():
        return "".join(rendered_text(root) for root in run.roots)


def held(found: Sequence[BaseException], *wanted: BaseException) -> list[str]:
    """The labels of ``wanted`` that ``found`` does not hold by identity."""
    ids = {id(exc) for exc in found}
    return [label(exc) for exc in wanted if id(exc) not in ids]


# -- failure during cleanup, at both sites (M5c) ------------------------------


@pytest.mark.parametrize(
    "failing",
    [(2,), (1,), (2, 0), (1, 0), (2, 1, 0)],
    ids=["first-swept", "middle", "first-swept-and-root", "middle-and-root", "all"],
)
def test_close_attempts_every_item_whatever_an_earlier_one_raised(
    schema: GraphQLSchema, failing: tuple[int, ...]
) -> None:
    """Item 0 is the root pool. The sweep runs 2, 1, 0."""
    made = {index: Failure(f"item {index} close") for index in failing}
    items = [
        Item(thrower(made[index]) if index in made else None) for index in range(3)
    ]
    client = GraphQLClient(
        transport=FakeGraphQLTransport(schema),
        schema=schema,
        owns_transport=True,
        cleanup=list(items),
    )

    raised = _left(client.close, None)
    client.close()

    assert [item.calls for item in items] == [1, 1, 1]
    assert raised is made[min(failing)]  # the earliest-acquired resource
    assert not held(reported_errors(raised), *made.values())


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize(
    "order", ["ordinary-then-interrupt", "interrupt-then-ordinary"]
)
def test_an_interrupt_leaves_in_either_sweep_order(
    monkeypatch: pytest.MonkeyPatch, schema: GraphQLSchema, site: str, order: str
) -> None:
    stop, ordinary = Interrupt("stop"), Failure("ordinary close")
    first, second = (
        (ordinary, stop) if order.startswith("ordinary") else (stop, ordinary)
    )
    primary = Failure("construction") if site == "factory" else Failure("root close")

    # Losers are swept last to first, so ``first`` is swept before ``second``,
    # and a clean item at the bottom shows that every later item is attempted.
    run = run_site(
        site,
        monkeypatch,
        schema,
        thrower(primary),
        [None, thrower(second), thrower(first)],
    )

    assert run.raised is stop
    assert all(item.calls == 1 for item in run.items)
    assert not held(reported_errors(run.raised), ordinary, primary)
    assert not held(run.observed(), ordinary, primary)


@pytest.mark.parametrize("site", SITES)
def test_a_report_inside_an_active_handler_keeps_everything_reachable(
    monkeypatch: pytest.MonkeyPatch, schema: GraphQLSchema, site: str
) -> None:
    """A ``raise`` there overwrites ``__context__``, so the seal is what keeps them."""
    outer = Failure("the caller's own handled exception")
    primary = Failure("construction") if site == "factory" else Failure("root close")
    losers = [Failure("middle close"), Failure("top close")]

    run = run_site(
        site,
        monkeypatch,
        schema,
        thrower(primary),
        [thrower(exc) for exc in losers],
        outer=outer,
    )

    assert run.raised is primary
    # Through the graph alone, not only through the record.
    assert not held(reachable(run.raised), *losers, outer)
    assert not held(reported_errors(run.raised), primary, *losers)
    if run.client is not None:
        run.client.close()
        assert all(item.calls == 1 for item in run.items)


# -- reporting: cycles, explicit causes, from None (C29 to C40) ---------------


def _self_context(exc: BaseException, partner: BaseException) -> None:  # noqa: ARG001
    exc.__context__ = exc


def _two_context(exc: BaseException, partner: BaseException) -> None:
    exc.__context__ = partner
    partner.__context__ = exc


def _two_cause(exc: BaseException, partner: BaseException) -> None:
    exc.__cause__ = partner
    partner.__cause__ = exc


CYCLES = {
    "self-context": _self_context,
    "two-context": _two_context,
    "two-cause": _two_cause,
}


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("cycle", sorted(CYCLES))
@pytest.mark.parametrize("role", ["loser", "raised"])
def test_a_cyclic_failure_terminates_and_keeps_the_others(
    monkeypatch: pytest.MonkeyPatch,
    schema: GraphQLSchema,
    site: str,
    cycle: str,
    role: str,
) -> None:
    cyclic, partner = Failure("cyclic"), Failure("cyclic partner")
    other = Failure("other failure")

    def make(exc: BaseException) -> None:
        CYCLES[cycle](exc, partner)

    looped = thrower(cyclic, shape=make)
    if role == "raised":
        run = run_site(site, monkeypatch, schema, looped, [thrower(other)])
    else:
        run = run_site(site, monkeypatch, schema, thrower(other), [looped])

    assert run.raised is (cyclic if role == "raised" else other)
    assert_prints(run)
    assert not held(reported_errors(run.raised), cyclic, other)
    joined = () if cycle == "self-context" else (partner,)
    assert not held(run.observed(), cyclic, other, *joined)


@pytest.mark.parametrize("site", SITES)
def test_a_primary_with_an_explicit_cause_keeps_it_and_gains_the_failure_below_it(
    monkeypatch: pytest.MonkeyPatch, schema: GraphQLSchema, site: str
) -> None:
    primary, cause = Failure("primary"), Failure("primary's own cause")
    loser = Failure("cleanup loser")

    run = run_site(
        site, monkeypatch, schema, thrower(primary, cause=cause), [thrower(loser)]
    )

    assert run.raised is primary
    chain = lifecycle._rendered(run.raised)
    assert chain[1] is cause  # still the head of what is under the primary
    assert not held(chain[2:], loser)  # and the cleanup failure sits below it
    text = assert_prints(run)
    assert "primary's own cause" in text
    assert "cleanup loser" in text


@pytest.mark.parametrize("site", SITES)
def test_a_loser_with_an_explicit_cause_is_extended_rather_than_replaced(
    monkeypatch: pytest.MonkeyPatch, schema: GraphQLSchema, site: str
) -> None:
    primary, loser = Failure("primary"), Failure("cleanup loser")
    cause, peer = Failure("loser's own cause"), Failure("cleanup peer")

    run = run_site(
        site,
        monkeypatch,
        schema,
        thrower(primary),
        [thrower(peer), thrower(loser, cause=cause)],
    )

    assert run.raised is primary
    assert links(loser)[0] is cause
    text = rendered_text(run.raised)  # asserted on the printed text itself
    for name in ("primary", "cleanup loser", "loser's own cause", "cleanup peer"):
        assert name in text, name


@pytest.mark.parametrize("site", SITES)
def test_a_primary_raised_from_none_keeps_its_suppression(
    monkeypatch: pytest.MonkeyPatch, schema: GraphQLSchema, site: str
) -> None:
    primary, loser = Failure("primary"), Failure("cleanup loser")

    run = run_site(
        site, monkeypatch, schema, thrower(primary, from_none=True), [thrower(loser)]
    )

    assert run.raised is primary
    assert lifecycle._read(primary, "__suppress_context__") is True
    assert links(primary)[0] is None
    assert lifecycle._rendered(run.raised) == [primary]
    assert "cleanup loser" not in rendered_text(run.raised)
    assert not held(reported_errors(run.raised), primary, loser)


# -- sealing (C30) ------------------------------------------------------------

SEAL_SITES = ("close", "factory", "factory-interrupt")


@pytest.mark.parametrize(
    ("site", "inner", "outer", "peer"),
    list(itertools.product(SEAL_SITES, (False, True), (False, True), (False, True))),
)
def test_sealing_names_every_failure_in_the_printed_chain(
    monkeypatch: pytest.MonkeyPatch,
    schema: GraphQLSchema,
    site: str,
    inner: bool,
    outer: bool,
    peer: bool,
) -> None:
    """The full cross-product, at both sites and with the factory's interrupt."""
    within = Failure("inner cause") if inner else None
    handled = Failure("unrelated outer") if outer else None
    loser: BaseException = (
        Interrupt("cleanup interrupt")
        if site == "factory-interrupt"
        else Failure("cleanup loser")
    )
    other = Failure("cleanup peer") if peer else None
    raising = thrower(loser, within=within)

    if site == "close":
        # The client raises the failure of its earliest-acquired item. With a
        # peer that is the peer, and the loser is swept before it. Alone, the
        # loser is the one raised.
        if other is None:
            run = run_site("close", monkeypatch, schema, raising, outer=handled)
            chosen: BaseException = loser
        else:
            run = run_site(
                "close", monkeypatch, schema, thrower(other), [raising], outer=handled
            )
            chosen = other
        direct = [loser, *([other] if other is not None else [])]
    else:
        construction = Failure("construction")
        losers = [raising] if other is None else [thrower(other), raising]
        run = run_site(
            "factory",
            monkeypatch,
            schema,
            thrower(construction),
            losers,
            outer=handled,
        )
        chosen = loser if site == "factory-interrupt" else construction
        direct = [construction, loser, *([other] if other is not None else [])]

    assert run.raised is chosen
    text = rendered_text(run.raised)
    names = [label(exc) for exc in direct]
    names += [label(exc) for exc in (within, handled) if exc is not None]
    for name in names:
        assert name in text, name


# -- hostile exception types (C31) --------------------------------------------
#
# Every hostile type below is an ordinary ``Exception``, so each one is a
# failure a caller's transport could raise. The traceback module reads the
# chain through ordinary attribute access, so 9.7 leaves a hostile type's own
# display to that type. What is compared here is what reporting answers: the
# chain it renders through the real slots, and ``reported_errors``.

DECOY = Failure("decoy")


def _read_hook(name: str, make: Callable[[], BaseException]) -> type[Failure]:
    class Hostile(Failure):
        def __getattribute__(self, attr: str) -> Any:
            if attr == name:
                raise make()
            return super().__getattribute__(attr)

    return Hostile


class _Lies(Failure):
    def __getattribute__(self, attr: str) -> Any:
        if attr in ("__cause__", "__context__"):
            return DECOY
        if attr == "__suppress_context__":
            return "yes"
        return super().__getattribute__(attr)


class _RejectsName(Failure):
    def __setattr__(self, attr: str, value: object) -> None:
        if attr == REPORTED_ERRORS:
            raise AttributeError(attr)
        super().__setattr__(attr, value)


def _exit_setter(_self: object, _value: object) -> None:
    raise SystemExit("hostile setter")


def _descriptor(name: str, value: property) -> type[Failure]:
    return type(name, (Failure,), {REPORTED_ERRORS: value})


class _Hides(Failure):
    def __getattribute__(self, attr: str) -> Any:
        if attr == REPORTED_ERRORS:
            raise AttributeError(attr)
        return super().__getattribute__(attr)


class _LyingDict(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        pass

    def __contains__(self, key: object) -> bool:
        return False

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ARG002 -- it lies
        return default


class _LyingStore(Failure):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        lifecycle._SLOTS["__dict__"].__set__(self, _LyingDict())


HOSTILE: dict[str, type[Failure]] = {
    **{
        f"read-raises-{slot.strip('_')}": _read_hook(slot, lambda: Failure("read"))
        for slot in ("__cause__", "__context__", "__suppress_context__")
    },
    **{
        f"read-exits-{slot.strip('_')}": _read_hook(slot, lambda: SystemExit("read"))
        for slot in ("__cause__", "__context__", "__suppress_context__")
    },
    "read-lies": _Lies,
    "write-rejects-the-name": _RejectsName,
    "write-read-only-descriptor": _descriptor("ReadOnly", property(lambda _: ())),
    "write-setter-exits": _descriptor(
        "SetterExits", property(lambda _: (), _exit_setter)
    ),
    "write-setter-drops": _descriptor(
        "SetterDrops", property(lambda _: (), lambda _s, _v: None)
    ),
    "write-read-only-context": type(
        "ReadOnlyContext", (Failure,), {"__context__": property(lambda _: None)}
    ),
    "retrieve-hides-the-record": _Hides,
    "retrieve-forges-a-shorter-tuple": _descriptor(
        "Forges", property(lambda self: (self,))
    ),
    "retrieve-lying-dict": _LyingStore,
}


@dataclass
class Answer:
    """What reporting answered for one run, read while the path was in force."""

    raised: BaseException
    expected: BaseException
    direct: tuple[BaseException, ...]
    chain: list[str]
    report: tuple[BaseException, ...]
    stored: bool
    observed: list[BaseException]


@contextmanager
def _path(monkeypatch: pytest.MonkeyPatch, path: str) -> Iterator[None]:
    if path == "record":
        yield
        return
    with injected(monkeypatch, no_store=True) as state:
        yield
    assert state.stores > 0, "the fallback path never reached the store"


def _hostile_run(
    monkeypatch: pytest.MonkeyPatch,
    schema: GraphQLSchema,
    kind: type[Failure],
    *,
    site: str,
    role: str,
    from_none: bool,
    path: str,
) -> Answer:
    chosen = (kind if role == "selected" else Failure)("selected failure")
    loser = (kind if role == "loser" else Failure)("cleanup loser")
    peer = Failure("cleanup peer")
    with _path(monkeypatch, path):
        run = run_site(
            site,
            monkeypatch,
            schema,
            thrower(chosen, from_none=from_none and role == "selected"),
            [thrower(peer), thrower(loser, from_none=from_none and role == "loser")],
        )
        return Answer(
            raised=run.raised,
            expected=chosen,
            direct=(chosen, loser, peer),
            chain=labels(lifecycle._rendered(run.raised)),
            report=reported_errors(run.raised),
            stored=bool(lifecycle._stored(run.raised)),
            observed=run.observed(),
        )


@pytest.mark.parametrize("path", ["record", "fallback"])
@pytest.mark.parametrize("from_none", [False, True], ids=["raise", "from-none"])
@pytest.mark.parametrize("role", ["selected", "loser"])
@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("kind", sorted(HOSTILE))
def test_a_hostile_type_changes_nothing_reporting_answers(
    monkeypatch: pytest.MonkeyPatch,
    schema: GraphQLSchema,
    kind: str,
    site: str,
    role: str,
    from_none: bool,
    path: str,
) -> None:
    options: dict[str, Any] = {
        "site": site,
        "role": role,
        "from_none": from_none,
        "path": path,
    }
    hostile = _hostile_run(monkeypatch, schema, HOSTILE[kind], **options)
    plain = _hostile_run(monkeypatch, schema, Failure, **options)

    # The selected failure still leaves.
    assert hostile.raised is hostile.expected
    # The path under test is the one that answered.
    assert hostile.stored is (path == "record")
    assert plain.stored is hostile.stored
    # Every direct failure is still named, on the record and on the fallback.
    assert not held(hostile.report, *hostile.direct)
    # And the rendered chain is the plain type's, failure for failure.
    assert hostile.chain == plain.chain
    assert labels(hostile.report) == labels(plain.report)
    # A lying read never brings its decoy into the graph.
    assert all(exc is not DECOY for exc in hostile.observed)
