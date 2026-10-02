"""The opcode interrupt sweep over the factory and its release path (9.2, 9.7).

A ``KeyboardInterrupt`` can arrive between any two bytecode instructions, not
only between lines. These tests inject one at every opcode boundary of a frame
in turn, and check that every resource the factory created is closed exactly
once and that none is closed twice. Every path through each frame is swept,
not only the success path: the factory as it returns a client, as it unwinds
a failed schema load, and as it unwinds a failed client constructor; the
sweep that unwinding calls; the client's ``close()``; the owned wrapper as it
acquires; and every close that runs at most once, as it releases.

Nothing leaks except at the residual boundaries 9.7 names, and those are
asserted to leak exactly where 9.7 says: the ``RETURN_VALUE`` instruction of
the factory, the two boundaries inside each owned wrapper between the
acquisition returning and the store into ``inner``, the unwinding path before
the sweep has attempted an item, and the boundaries inside a once-only close
from marking itself closed through the call that releases its resource. With a
supplied cleanup list, which the caller sweeps again, the releasing call sites
and the sweep no longer leak. The wrapper store pair and the once-only close
window still do, because the second sweep cannot reach the one and skips a
wrapper the other already marked closed.

The injection is keyed on a count of opcode events, not on a bytecode offset,
because offsets shift between versions and with specialisation. Every
injection is asserted to have fired, so a boundary the hook never reached
fails the test instead of passing as clean. Python 3.12 and later use
``sys.monitoring``, which instruments the code object before the call; there,
setting ``f_trace_opcodes`` from a trace function misses the first call of
each code object. Earlier versions use ``sys.settrace``.

The root pool is a fake, so the sweep opens no socket. The factory body runs
as published: it passes ``owns_transport=True`` and hands the client the list
it built or the list it was given.
"""

from __future__ import annotations

import dis
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from types import CodeType, FrameType
from typing import Any, Literal

import httpx
import pytest
from graphql import GraphQLSchema

from pytest_graphql._core import client as factory
from pytest_graphql._core.client import ClientConfig, GraphQLClient, build_client
from pytest_graphql._core.lifecycle import (
    Closable,
    _close_all,
    _Owned,
    reported_errors,
)
from pytest_graphql._core.transport.httpx_transport import (
    HttpxTransport,
    _NonClosingPoolWrapper,
)
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema

URL = "https://example.test/graphql"

FACTORY: CodeType = build_client.__code__
WRAPPER: CodeType = _Owned.__init__.__code__
SWEEP: CodeType = _close_all.__code__
CLOSE: CodeType = GraphQLClient.close.__code__
OWNED_CLOSE: CodeType = _Owned.close.__code__

# The path a run takes: a returned client, a schema load that fails after the
# pool and the probe exist, a client constructor that fails after the owning
# transport exists, or a returned client that is then closed.
Path = Literal["success", "schema", "constructor", "close"]


class _Injected(KeyboardInterrupt):
    """The interrupt the sweep raises, so a real one is never mistaken for it."""


class _Broken(Exception):  # noqa: N818 -- a construction failure, not an error class
    """The construction failure a failure path raises."""


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


# -- resources that count their closes ----------------------------------------


class _Counted(FakeGraphQLTransport):
    def __init__(self, created: list[_Counted], schema: GraphQLSchema) -> None:
        super().__init__(schema)
        self.created = created
        self.graphql_schema = schema
        self.closes = 0
        created.append(self)

    def close(self) -> None:
        self.closes += 1
        super().close()

    def derive(self, *, own_pool: bool = False) -> _Counted:  # noqa: ARG002 -- the factory's own shape
        return _Counted(self.created, self.graphql_schema)


class _Source:
    def __init__(self, schema: GraphQLSchema | None) -> None:
        self.schema = schema

    def load(self) -> GraphQLSchema:
        if self.schema is None:
            raise _Broken("schema")
        return self.schema


class _BrokenMiddleware:
    """A middleware sequence that cannot be read, so the constructor raises."""

    def __iter__(self) -> Iterator[Any]:
        raise _Broken("constructor")


# -- injection at the n-th opcode event ---------------------------------------


@dataclass
class _Injection:
    """Raises at opcode event ``at`` of the target code; ``at=0`` only counts."""

    at: int
    events: int = 0
    offset: int | None = None
    raised: _Injected | None = None

    def tick(self, offset: int) -> None:
        self.events += 1
        if self.events == self.at:
            self.offset = offset
            self.raised = _Injected(f"opcode event {self.at}")
            raise self.raised


@contextmanager
def _injecting(code: CodeType, injection: _Injection) -> Iterator[None]:
    """Count opcode events in ``code`` alone, and inject at the chosen one."""
    if sys.version_info >= (3, 12):
        monitoring = sys.monitoring
        tool = next(i for i in range(6) if monitoring.get_tool(i) is None)
        instruction = monitoring.events.INSTRUCTION

        def on_instruction(_code: CodeType, offset: int) -> None:
            injection.tick(offset)

        monitoring.use_tool_id(tool, "opcode interrupt sweep")
        try:
            monitoring.register_callback(tool, instruction, on_instruction)
            monitoring.set_local_events(tool, code, instruction)
            yield
        finally:
            monitoring.set_local_events(tool, code, 0)
            monitoring.register_callback(tool, instruction, None)
            monitoring.free_tool_id(tool)
    else:

        def on_opcode(frame: FrameType, event: str, _arg: object) -> Any:
            if event == "opcode":
                injection.tick(frame.f_lasti)
            return on_opcode

        def on_call(frame: FrameType, _event: str, _arg: object) -> Any:
            if frame.f_code is not code:
                return None
            frame.f_trace_opcodes = True
            return on_opcode

        previous = sys.gettrace()
        sys.settrace(on_call)
        try:
            yield
        finally:
            sys.settrace(previous)


# -- one run per boundary -------------------------------------------------------


@dataclass
class _Run:
    path: Path
    injection: _Injection
    created: list[_Counted]
    client: GraphQLClient | None
    left: BaseException | None
    supplied: list[Closable] | None = None
    leaked: list[_Counted] = field(init=False)

    def __post_init__(self) -> None:
        self.leaked = [item for item in self.created if item.closes == 0]

    @property
    def open_at_failure(self) -> list[_Counted]:
        """What was open when unwinding began, in creation order.

        The probe closes on the success path before the owning transport is
        derived, so once an owning transport exists the probe is not open.
        """
        if len(self.created) == 3:
            root, _probe, owner = self.created
            return [root, owner]
        return list(self.created)


class _Factory:
    def __init__(self, schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch) -> None:
        self.schema = schema
        self.created: list[_Counted] = []

        def new_root(config: ClientConfig) -> _Counted:  # noqa: ARG001 -- the factory's own shape
            return _Counted(self.created, schema)

        monkeypatch.setattr(factory, "_new_root_pool", new_root)

    def _build(self, path: Path, supplied: list[Closable] | None) -> GraphQLClient:
        if path == "constructor":
            return build_client(
                url=URL,
                cleanup=supplied,
                schema_source=_Source(self.schema),
                middleware=_BrokenMiddleware(),  # type: ignore[arg-type]
            )
        source = _Source(None if path == "schema" else self.schema)
        return build_client(url=URL, cleanup=supplied, schema_source=source)

    def run(self, code: CodeType, at: int, *, path: Path, supply: bool) -> _Run:
        """Take ``path`` once with an injection at ``at``, then sweep any list."""
        self.created = []
        injection = _Injection(at)
        supplied: list[Closable] | None = [] if supply else None
        client: GraphQLClient | None = None
        left: BaseException | None = None
        if path == "close":
            client = self._build(path, supplied)
        with _injecting(code, injection):
            try:
                if client is None:
                    client = self._build(path, supplied)
                else:
                    client.close()
            except (_Injected, _Broken) as exc:
                left = exc
        if supplied is not None:
            # The caller's own teardown, as the session transport fixture runs it.
            assert _close_all(supplied) == []
        return _Run(path, injection, self.created, client, left, supplied)

    def _baseline(self, code: CodeType, *, path: Path, supply: bool) -> _Run:
        run = self.run(code, 0, path=path, supply=supply)
        if path in ("schema", "constructor"):
            assert isinstance(run.left, _Broken)
            assert run.client is None
        else:
            assert run.left is None
            assert run.client is not None
            assert run.client.owns_transport is True
            if supply:
                assert run.client._cleanup is run.supplied
            run.client.close()
        assert all(item.closes == 1 for item in run.created)
        return run

    def sweep(self, code: CodeType, *, path: Path, supply: bool) -> Iterator[_Run]:
        """One run per opcode boundary ``path`` crosses in ``code``.

        A first run lets any other tool, such as coverage, instrument every
        code object the path reaches. On 3.12 and later, instrumenting a new
        code object while the swept frame runs drops some of that frame's
        opcode events, so a count taken then is short and leaves boundaries
        unswept. A second count after the sweep proves it was not.
        """
        self._baseline(code, path=path, supply=supply)
        boundaries = self._baseline(code, path=path, supply=supply).injection.events
        assert boundaries > 0, "the hook never reached the target code"
        for at in range(1, boundaries + 1):
            run = self.run(code, at, path=path, supply=supply)
            assert run.injection.offset is not None, f"boundary {at} never fired"
            assert run.left is run.injection.raised, f"boundary {at}"
            if path != "close":
                assert run.client is None, f"boundary {at}"
            assert all(item.closes <= 1 for item in run.created), f"boundary {at}"
            yield run
        again = self._baseline(code, path=path, supply=supply)
        assert again.injection.events == boundaries, "the count was not stable"


@pytest.fixture
def builds(schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch) -> _Factory:
    return _Factory(schema, monkeypatch)


def _opname(code: CodeType) -> Callable[[int], str]:
    names = {each.offset: each.opname for each in dis.get_instructions(code)}
    return names.__getitem__


def _store_window(code: CodeType) -> set[int]:
    """The offsets after the acquisition call returns, through the ``inner`` store."""
    instructions = list(dis.get_instructions(code))
    store = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR" and instruction.argval == "inner"
    )
    call = max(
        index
        for index, instruction in enumerate(instructions[:store])
        if instruction.opname.startswith("CALL")
    )
    return {instruction.offset for instruction in instructions[call + 1 : store + 1]}


def _release_window(code: CodeType) -> set[int]:
    """The offsets after a close marks itself closed, through the call that releases.

    The call's own event fires before the call runs, so it is in the window.
    """
    instructions = list(dis.get_instructions(code))
    mark = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_closed"
    )
    release = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname.startswith("CALL")
    )
    assert release > mark, "the close releases before it marks itself closed"
    return {instruction.offset for instruction in instructions[mark + 1 : release + 1]}


def _call_into_sweep(code: CodeType) -> int:
    """The offset of the call that hands a cleanup list to ``_close_all``."""
    instructions = list(dis.get_instructions(code))
    load = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL" and instruction.argval == "_close_all"
    )
    return next(
        instruction.offset
        for instruction in instructions[load + 1 :]
        if instruction.opname.startswith("CALL")
    )


def _assert_entry_window(code: CodeType, leaks: list[_Run]) -> None:
    """Leaks form one stretch of boundaries that ends at the call into the sweep.

    The stretch starts after the path created everything it creates, so an
    interrupt before the failure is never part of it. Each leak strands
    everything that was open, because the sweep has not started. That is the
    unwinding residual 9.7 names, at a releasing call site.
    """
    assert leaks, "the unwinding path never leaked, so 9.7 names a window too many"
    created = 2 if leaks[0].path == "schema" else 3
    assert all(len(run.created) == created for run in leaks)
    ats = [run.injection.at for run in leaks]
    assert ats == list(range(ats[0], ats[-1] + 1)), "the leaks are not one stretch"
    assert leaks[-1].injection.offset == _call_into_sweep(code)
    for run in leaks:
        assert run.leaked == run.open_at_failure, f"boundary {run.injection.at}"


# -- the sweeps -----------------------------------------------------------------


def test_the_factory_leaks_only_at_its_return_instruction(builds: _Factory) -> None:
    opname = _opname(FACTORY)
    runs = builds.sweep(FACTORY, path="success", supply=False)
    leaks = [run for run in runs if run.leaked]

    assert [opname(run.injection.offset or 0) for run in leaks] == ["RETURN_VALUE"]
    (leak,) = leaks
    root, probe, owner = leak.created
    assert leak.leaked == [root, owner]
    assert probe.closes == 1


@pytest.mark.parametrize("path", ["schema", "constructor"])
def test_the_factory_unwinding_leaks_only_before_its_sweep_starts(
    builds: _Factory, path: Path
) -> None:
    runs = list(builds.sweep(FACTORY, path=path, supply=False))

    _assert_entry_window(FACTORY, [run for run in runs if run.leaked])


def test_closing_the_client_leaks_only_before_its_sweep_starts(
    builds: _Factory,
) -> None:
    runs = list(builds.sweep(CLOSE, path="close", supply=False))
    leaks = [run for run in runs if run.leaked]

    _assert_entry_window(CLOSE, leaks)
    for run in leaks:
        assert run.client is not None
        run.client.close()
        assert all(item.closes <= 1 for item in run.created)
    # Once the client is marked closed a second close does not retry, which is
    # why 9.7 names this window for a caller that supplied no list.
    last = leaks[-1]
    assert [item for item in last.created if item.closes == 0] == last.open_at_failure


@pytest.mark.parametrize("path", ["schema", "constructor"])
def test_an_interrupted_sweep_loses_only_the_item_in_progress_and_later_ones(
    builds: _Factory, path: Path
) -> None:
    abandoned = in_progress = 0
    for run in builds.sweep(SWEEP, path=path, supply=False):
        if not run.leaked:
            continue
        order = run.open_at_failure[::-1]  # the sweep runs in reverse
        first = min(order.index(item) for item in run.leaked)
        # Nothing the sweep already attempted is lost.
        assert all(item.closes == 1 for item in order[:first])
        if run.leaked == order[first:][::-1]:
            # Outside an attempt: the sweep stops, and the rest stays open.
            abandoned += 1
        else:
            # Inside one attempt, before its close() ran: that attempt failed
            # with the interrupt, it is reported, and every later item closed.
            assert run.leaked == [order[first]]
            assert run.left is not None
            assert any(isinstance(exc, _Broken) for exc in reported_errors(run.left))
            in_progress += 1
    assert abandoned > 0
    assert in_progress > 0


@pytest.mark.parametrize("supply", [False, True], ids=["own list", "supplied list"])
def test_the_wrapper_leaks_only_between_acquiring_and_storing(
    builds: _Factory, supply: bool
) -> None:
    window = _store_window(WRAPPER)
    assert len(window) == 2, "9.7 names two boundaries per owned wrapper"
    runs = list(builds.sweep(WRAPPER, path="success", supply=supply))
    leaks = [run for run in runs if run.leaked]

    assert len(runs) % 3 == 0, "three acquisitions cross the same boundaries"
    assert len(leaks) == 3 * len(window)
    for run in leaks:
        assert run.injection.offset in window
        # Only the resource being acquired is lost; everything before it closed.
        assert run.leaked == [run.created[-1]]
    assert sorted({len(run.created) for run in leaks}) == [1, 2, 3]


@pytest.mark.parametrize("supply", [False, True], ids=["own list", "supplied list"])
def test_a_wrapper_close_leaks_only_between_marking_and_releasing(
    builds: _Factory, supply: bool
) -> None:
    window = _release_window(OWNED_CLOSE)
    if not supply:
        # Nothing retries, so the whole attempt up to the release is the
        # in-progress item's close failure (see the sweep test above).
        window |= {
            each.offset
            for each in dis.get_instructions(OWNED_CLOSE)
            if each.offset <= max(window)
        }
    runs = list(builds.sweep(OWNED_CLOSE, path="close", supply=supply))
    leaks = [run for run in runs if run.leaked]

    assert leaks
    for run in leaks:
        assert run.injection.offset in window, f"boundary {run.injection.at}"
        # The sweep went on to the other wrapper, and with a supplied list the
        # second sweep skipped the one already marked closed.
        (leaked,) = run.leaked
        assert leaked in run.open_at_failure
    if supply:
        # The owner and the root pool cross the window as the client closes.
        # The probe closed while the client was built, so its wrapper returns
        # early and never reaches the window.
        assert len(leaks) == 2 * len(window)


class _CountedPool(httpx.BaseTransport):
    def __init__(self) -> None:
        self.closes = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # noqa: ARG002 -- httpx's own shape
        raise AssertionError("the sweep sends no request")

    def close(self) -> None:
        self.closes += 1


def _owned(pool: _CountedPool) -> Closable:
    return _Owned([], lambda: pool)


def _pool_wrapper(pool: _CountedPool) -> Closable:
    return _NonClosingPoolWrapper(pool, close_pool=True)


def _transport(pool: _CountedPool) -> Closable:
    return HttpxTransport(_shared_pool=pool)


@pytest.mark.parametrize(
    ("code", "make"),
    [
        (OWNED_CLOSE, _owned),
        (_NonClosingPoolWrapper.close.__code__, _pool_wrapper),
        (HttpxTransport.close.__code__, _transport),
    ],
    ids=["owned wrapper", "pool wrapper", "transport"],
)
def test_a_once_only_close_is_not_retried_after_it_marks_itself_closed(
    code: CodeType, make: Callable[[_CountedPool], Closable]
) -> None:
    """Every boundary outside the window is recovered by a second close().

    A second close() is what a second sweep of a supplied list runs. So the
    boundaries that leak are exactly the window, each once, and nothing is
    ever closed twice.
    """

    def run(at: int) -> tuple[_Injection, _CountedPool]:
        pool = _CountedPool()
        closable = make(pool)
        injection = _Injection(at)
        raised = pytest.raises(_Injected) if at else nullcontext()
        with _injecting(code, injection), raised:
            closable.close()
        closable.close()
        assert pool.closes <= 1, f"boundary {at}"
        return injection, pool

    run(0)  # instrument every code object the close reaches, as above
    boundaries = run(0)[0].events
    leaked: list[int] = []
    for at in range(1, boundaries + 1):
        injection, pool = run(at)
        assert injection.offset is not None, f"boundary {at} never fired"
        if pool.closes == 0:
            leaked.append(injection.offset)
    assert run(0)[0].events == boundaries, "the count was not stable"
    assert sorted(leaked) == sorted(_release_window(code))


@pytest.mark.parametrize(
    ("code", "path"),
    [
        (FACTORY, "success"),
        (FACTORY, "schema"),
        (FACTORY, "constructor"),
        (SWEEP, "schema"),
        (SWEEP, "constructor"),
        (CLOSE, "close"),
    ],
    ids=[
        "factory returns",
        "factory unwinds a schema failure",
        "factory unwinds a constructor failure",
        "sweep after a schema failure",
        "sweep after a constructor failure",
        "client close",
    ],
)
def test_a_supplied_list_closes_what_the_call_sites_and_the_sweep_strand(
    builds: _Factory, code: CodeType, path: Path
) -> None:
    runs = list(builds.sweep(code, path=path, supply=True))

    assert [run.injection.at for run in runs if run.leaked] == []
    assert any(run.created for run in runs)
