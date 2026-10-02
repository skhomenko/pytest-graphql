"""Ownership, the cleanup sweep and failure reporting (M5c).

The ownership contract, the sweep and the report are the most safety-critical
part of this project. ``docs/reference/DESIGN_DECISIONS.md`` section 9 is the
normative statement, and the code below is transcribed from the consolidated
lifecycle in ``PLAN.md`` 2.14 (C28, as amended in place by C29 through C40).

Several things here read as style choices and are not. Each was a review
finding, and each can be broken without any ordinary test failing:

- the record is written before the chain within a pass, because whether the
  record carries the report is what permits the chain a cycle;
- ``_walk`` rather than ``_rendered`` is the delivery predicate, because a
  suppressed link is out of the traceback and still in the graph;
- every read and write goes through the ``BaseException`` descriptors and the
  unbound ``dict`` item accessors, never ordinary attribute access, because a
  caller's exception type can lie about either;
- the record write is proved by reading it back, because a write that was
  accepted and stored nothing is a refusal too;
- the placement loop breaks on the first refusal, because a channel that just
  refused is not a channel right now.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import (
    Any,
    Final,
    Generic,
    NoReturn,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
)


class Closable(Protocol):
    def close(self) -> None: ...


P = ParamSpec("P")
T = TypeVar("T", bound=Closable)


class _Owned(Generic[T]):
    """Adopted by the cleanup list before it acquires anything."""

    inner: T | None

    def __init__(
        self,
        cleanup: list[Closable],
        acquire: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        self.inner = None  # own state first, so close() is safe half built
        self._closed = False
        cleanup.append(self)  # adopted before the resource exists
        self.inner = acquire(*args, **kwargs)

    @property
    def value(self) -> T:
        if self.inner is None:
            raise RuntimeError("acquisition did not complete")
        return self.inner

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.inner is not None:
            self.inner.close()


def _close_all(items: Sequence[Closable]) -> list[BaseException]:
    """Close every item once, in reverse order. Return the failures in sweep order."""
    errors: list[BaseException] = []
    for item in reversed(items):
        try:
            item.close()
        except BaseException as exc:  # every item must still be attempted
            errors.append(exc)
    return errors


def _primary(
    errors: Sequence[BaseException], preferred: BaseException
) -> BaseException:
    """Never downgrade an interrupt; otherwise the caller's preferred exception wins."""
    for exc in (preferred, *errors):
        if not isinstance(exc, Exception):
            return exc
    return preferred


REPORTED_ERRORS = "pytest_graphql_reported_errors"

# The storage the interpreter itself uses for these names, taken from `BaseException`.
# A caller's exception type can shadow any of them with its own descriptor and can
# intercept them in `__getattribute__` and `__setattr__`, so an ordinary read can lie
# or raise and an ordinary write can be accepted and dropped. None of that changes
# what these four descriptors reach, which is the chain `raise` itself reads and
# writes.
_SLOTS: Final[dict[str, Any]] = {
    name: BaseException.__dict__[name]
    for name in ("__cause__", "__context__", "__suppress_context__", "__dict__")
}


def _read(node: BaseException, name: str) -> Any:
    """Read one real slot on `node`, past every hook its type installs.

    A read that fails answers `None`, the reading that hides nothing: an unknown
    `__suppress_context__` lets the walk continue, and an unknown link ends it.
    """
    try:
        return _SLOTS[name].__get__(node)
    except BaseException:
        return None


def _write(node: BaseException, name: str, value: object) -> BaseException | None:
    """Write one real slot on `node`. Return whatever refused, and never raise.

    One attempt, because this descriptor is the storage. There is no second path to
    fall back to, so there is no refusal to discard on the way to one.
    """
    try:
        _SLOTS[name].__set__(node, value)
        return None
    except BaseException as exc:
        return exc


def _kept(refusal: BaseException | None, refused: list[BaseException]) -> bool:
    """Keep a refusal as reportable data. True when the write happened."""
    if refusal is None:
        return True
    refused.append(refusal)
    return False


# The unbound builtin item accessors, so a `dict` subclass installed as `__dict__`
# cannot override the way the record is stored or read back either.
_ITEMS: Final = cast("type[dict[str, Any]]", dict)
_GET_ITEM: Final = _ITEMS.__getitem__
_SET_ITEM: Final = _ITEMS.__setitem__


def _store(node: BaseException) -> dict[str, Any] | None:
    """The real attribute dictionary of `node`, or None when there is none to reach."""
    found = _read(node, "__dict__")
    return cast("dict[str, Any]", found) if isinstance(found, dict) else None


def _load(node: BaseException) -> object:
    """Read the record back out of that dictionary, past any hook and any dict
    subclass.
    """
    store = _store(node)
    if store is None:
        return None
    try:
        return _GET_ITEM(store, REPORTED_ERRORS)
    except BaseException:
        return None


def _record(
    node: BaseException, reported: tuple[BaseException, ...]
) -> tuple[bool, BaseException | None]:
    """Store the report on `node` and read it back. Returns whether it is there, and
    whatever refused the write.

    A write that was accepted and stored nothing is a refusal too, so the answer is
    the read-back and not the absence of an exception.
    """
    store = _store(node)
    if store is None:
        return False, None
    try:
        _SET_ITEM(store, REPORTED_ERRORS, reported)
    except BaseException as exc:
        return False, exc
    return _load(node) is reported, None


def _stored(exc: BaseException) -> tuple[BaseException, ...]:
    """The record a report left on `exc`, or nothing when it holds none."""
    stored = _load(exc)
    if type(stored) is tuple:
        values = cast("tuple[object, ...]", stored)
        if values and all(isinstance(item, BaseException) for item in values):
            return cast("tuple[BaseException, ...]", values)
    return ()


def reported_errors(exc: BaseException) -> tuple[BaseException, ...]:
    """Every failure the report that raised `exc` covers, `exc` first.

    Read from the interpreter's own attribute storage, so a type that forges the
    ordinary read cannot change the answer. Falls back to the rendered chain, which
    is where the failures are put when that storage could not hold the record.
    """
    return _stored(exc) or tuple(_rendered(exc))


def _seen(node: BaseException, name: str, pending: _Edge | None) -> Any:
    """What `name` on `node` holds once the coming `raise` has written its own link.

    That link replaces whatever `__context__` held on the exception being raised, so
    while it is pending the value in the slot is one the caller never gets. Reading it
    instead would answer about a graph that stops existing the moment the report leaves,
    and the raise runs last, so the answer could not be corrected afterwards.
    """
    if pending is not None and name == "__context__" and node is pending[0]:
        return pending[1]
    return _read(node, name)


def _below(node: BaseException, pending: _Edge | None = None) -> BaseException | None:
    """What a traceback prints under `node`, or None when it prints nothing."""
    cause = _read(node, "__cause__")
    if isinstance(cause, BaseException):
        return cause
    if _read(node, "__suppress_context__"):
        return None  # an explicit `raise ... from None`
    context = _seen(node, "__context__", pending)
    return context if isinstance(context, BaseException) else None


def _rendered(
    head: BaseException,
    cut: BaseException | None = None,
    pending: _Edge | None = None,
) -> list[BaseException]:
    """The chain a traceback prints for `head`.

    A visited set bounds the walk, so a cyclic exception graph ends it instead of
    hanging it. `cut` is a node the walk stops at rather than passing through, which
    answers what a chain still reaches once that node's own link has been rewritten.
    """
    chain: list[BaseException] = []
    seen: set[int] = set() if cut is None else {id(cut)}
    node: BaseException | None = head
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = _below(node, pending)
    return chain


def _holds(chain: Sequence[BaseException], target: BaseException | None) -> bool:
    return target is not None and any(node is target for node in chain)


# One link the coming `raise` is going to write, named by the two exceptions it joins.
_Edge = tuple[BaseException, BaseException]


def _walk(node: BaseException, pending: _Edge | None = None) -> set[int]:
    """Every failure a walk of both public links reaches from `node`, `node` included.

    Both links, and a suppressed one too. `__suppress_context__` hides a link from a
    rendered traceback and leaves it in place for everything that walks the graph
    itself, so a rendered walk calls a closing edge free. This is the walk every cycle
    guard asks about, because the promise is about the graph the caller is handed and
    not about what a traceback prints from it.

    `pending` is the link the coming `raise` writes that reporting has not, and the walk
    takes it in place of the slot it overwrites. A refused write does not stop the
    interpreter from writing that link, so a walk that left it out would answer about a
    graph the caller never gets, and the answer cannot be corrected once the raise has
    run.
    """
    seen: set[int] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        for name in ("__cause__", "__context__"):
            found = _seen(item, name, pending)
            if isinstance(found, BaseException):
                stack.append(found)
    return seen


def _reaches(
    node: BaseException, target: BaseException, pending: _Edge | None = None
) -> bool:
    return id(target) in _walk(node, pending)


def _edge(raised: BaseException, handler: BaseException | None) -> _Edge | None:
    """The link the coming `raise` will write and reporting has not written itself.

    A `raise` inside an active `except` block writes the handled exception into
    `__context__` on the exception it raises. Reporting writes that link first, so that
    every question below is asked about the graph the caller actually gets. That write
    can be refused, and the exception that leaves can change afterwards when an
    interruption is promoted over it. The interpreter writes the link either way, so
    what reporting could not put in the graph is carried beside it instead.
    """
    if handler is None or handler is raised or _read(raised, "__context__") is handler:
        return None
    return (raised, handler)


def _shown(node: BaseException, refused: list[BaseException]) -> bool:
    """Clear a suppression that would hide the link just written into `node`.

    The flag was standing over an empty slot, so clearing it changes nothing that was
    rendering before, and leaving it would hide the failure this write just placed. A
    refused clear leaves the link in place and reachable, which is why the answer is the
    write's and not the flag's.
    """
    if not _read(node, "__suppress_context__"):
        return True
    _kept(_write(node, "__suppress_context__", False), refused)
    return True


def _shut(node: BaseException) -> bool:
    """Whether an explicit suppression on `node` still has a link to stand in the way
    of.

    `raise ... from None` sets the flag over whatever the interpreter had already put in
    the slot. Once that slot is empty the flag hides nothing, and a walk ends at `node`
    with or without it, so it is not a chain the report would be replacing. `_bypass`
    leaves exactly that state behind when it cuts a link it could not empty on the first
    attempt, and a later pass that emptied the slot should not still be locked out of
    it.
    """
    return bool(_read(node, "__suppress_context__")) and isinstance(
        _read(node, "__context__"), BaseException
    )


def _link(
    node: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    *,
    forced: bool = False,
    pending: _Edge | None = None,
) -> bool:
    """Make `extra` the rendered predecessor of `node`, touching only `node`'s links.

    `forced` is what permits a cycle, and it is set only when the record is not carrying
    the report. While the record holds them an acyclic graph is handed back acyclic, so
    the guard is the whole graph. Once the chain is the only channel left, the guard is
    what a rendered walk would repeat, and a link that only closes a suppressed edge is
    worth more than the repeat costs.
    """
    below = _rendered(extra, pending=pending)
    if _holds(below, node) or (not forced and _reaches(extra, node, pending)):
        return False  # linking here would close a cycle
    if _read(node, "__cause__") is not None or _shut(node):
        return False  # an explicit chain is never replaced
    current = _below(node, pending)
    if current is None or current is node or _holds(below, current):
        # free, or a self link, or what it held survives through `extra`
        return _kept(_write(node, "__context__", extra), refused) and _shown(
            node, refused
        )
    return False


def _merge(
    node: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    avoid: BaseException | None = None,
    *,
    forced: bool = False,
    pending: _Edge | None = None,
) -> bool:
    """Make `extra` reachable below `node`, dropping nothing already reachable.

    `avoid` is a node this must not write on. The exception being reported owns both of
    its own links: the coming `raise` overwrites one of them and `_seal` writes the
    other. A chain extended through it is a chain `_seal` then has to cut itself back
    out of, and a link that cannot be cut strands everything above it.
    """
    chain = _rendered(node, pending=pending)
    if _holds(chain, extra):
        return True  # already reachable
    if node is avoid:
        return False
    return _link(node, extra, refused, forced=forced, pending=pending) or (
        chain[-1] is not avoid
        and _link(chain[-1], extra, refused, forced=forced, pending=pending)
    )


def _under(
    node: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    avoid: BaseException | None = None,
    *,
    forced: bool = False,
    pending: _Edge | None = None,
) -> BaseException:
    """The chain head that keeps `node` and everything `extra` already reaches."""
    if extra is node:
        return node
    if _merge(node, extra, refused, avoid, forced=forced, pending=pending):
        return node  # `extra` hangs under `node`, which stays the head
    if _anchor(extra, node, refused, avoid, forced=forced, pending=pending):
        return extra  # `node`'s own links are full, so it goes underneath
    if _anchor(node, extra, refused, avoid, forced=forced, pending=pending):
        return node  # neither ordinary link was free, so `node` keeps
        # the head and `extra` goes at its deepest link
    # Neither of them will hold the other. Answering `extra` anyway would name a head
    # that does not reach `node`, and the caller would then build the rest of the report
    # on a chain that has already dropped one, so the answer is whichever still reaches
    # both. When neither does, one side is dropped whatever this answers, so it is the
    # side that takes fewer failures with it.
    if _reaches(node, extra, pending):
        return node
    if _reaches(extra, node, pending):
        return extra
    return node if len(_walk(node, pending)) >= len(_walk(extra, pending)) else extra


def _stack(
    primary: BaseException,
    sources: Sequence[BaseException | None],
    refused: list[BaseException],
    *,
    forced: bool = False,
    pending: _Edge | None = None,
) -> BaseException | None:
    """Start the chain that goes under the report, deepest first.

    This runs before the record has been written, so it cannot know yet whether the
    record is carrying the report and it never writes a cycle. The first pass folds the
    same failures in again once that answer is known.
    """
    below: BaseException | None = None
    for exc in sources:
        if exc is None or exc is primary:
            continue
        below = (
            exc
            if below is None
            else _under(exc, below, refused, primary, forced=forced, pending=pending)
        )
    return below


def _bypass(
    head: BaseException,
    target: BaseException,
    refused: list[BaseException],
    *,
    keeps: bool = True,
    spare: BaseException | None = None,
    pending: _Edge | None = None,
) -> bool:
    """Take `target` out of the middle of `head`'s chain.

    `target` belongs at the head of a chain and nowhere else: it is the exception about
    to be raised, or the one the coming `raise` will attach directly under it. With
    `keeps`, the link that reached it takes over its own successor, so nothing that sat
    under `target` is dropped. `_crown` clears `keeps`, because there `target` becomes
    the head of the chain itself, so what sat under it is still reached through it and
    splicing the same nodes in twice would only block the reattachment. Returns False
    when the link is an explicit cause, which is never cut, or when every write that
    could cut it was refused.

    `spare` is a node whose own link is not reporting's to cut, because the coming
    `raise` writes that link itself. Cutting it there removes an edge the raise puts
    straight back, and every reachability question asked in between then has the wrong
    answer.
    """
    successor = _below(target, pending) if keeps else None
    for node in _rendered(head, pending=pending):
        if _read(node, "__cause__") is target:
            return False
        if (
            _seen(node, "__context__", pending) is target
            and _read(node, "__cause__") is None
        ):
            if node is spare or (pending is not None and node is pending[0]):
                continue  # the coming `raise` writes this link itself
            if successor is target or (
                successor is not None and _reaches(successor, node, pending)
            ):
                successor = None  # splicing that in would close a cycle
            if _kept(_write(node, "__context__", successor), refused):
                return True
            if successor is not None:
                return False  # the link still leads back to `target`, and
                # nothing else can cut it without dropping what
                # `target` sat above
            # `target` was the whole of what this link held, so suppressing the link
            # ends the chain in the same place. It is a different slot, so a type that
            # refused the first write has not necessarily refused this one.
            return _kept(_write(node, "__suppress_context__", True), refused)
    return True  # `target` is not on the chain


def _keep(
    head: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    avoid: BaseException | None,
    *,
    forced: bool,
    slot: str = "__context__",
    reached: Sequence[BaseException] = (),
    pending: _Edge | None = None,
    owned: BaseException | None = None,
) -> bool:
    """Put `extra` directly under `head`, keeping whatever `head` already held.

    This is the one link in the group that closes a rendered cycle, and it is written
    only where the alternative is losing a failure. `head` is a node the chain has to
    hang from and cannot move off: the exception being reported, the one the coming
    `raise` attaches, or the deepest node of a chain that is occupied above it. In each
    of those the rest of the chain already leads back to `head`, so the ordinary link is
    the one `_link` refuses as a cycle, and whatever is left above `head` is what the
    raise drops.

    `forced` is what permits the cycle, and it is set only when the record is not
    carrying the report, so the rendered chain is the last place these failures can
    live. While the record holds them the chain stays best effort and no cycle is
    written, because an exception graph that arrived acyclic is handed back to the
    caller acyclic.

    A closed cycle costs one repeated node to a walk that bounds itself, which
    `_rendered`, the `traceback` module and the interpreter's own display all do.
    Nothing already reachable is dropped: what `head` held moves to the end of the chain
    `extra` reaches without running back through `head`, and that end is the one node in
    it whose own link the walk did not follow, so what that link holds is nothing, or
    `head`, or a node the same chain already reaches. `reached` is what the caller keeps
    reachable by another path, so a value that is only behind `head` is told apart from
    one that is not.

    Answers whether `extra` ended up reachable, because a caller that could not place it
    has to try the next place rather than carry on as though this one worked.
    """
    if extra is head or _holds(_rendered(head, pending=pending), extra):
        return True  # already reachable
    if not forced:
        return False  # the record carries them, so no cycle is written
    prior = _read(head, "__cause__")
    if isinstance(prior, BaseException) and (slot == "__context__" or prior is owned):
        return False  # an explicit chain is never replaced
    if slot == "__context__" and _shut(head):
        return False  # nor an explicit `raise ... from None`
    keeps = _rendered(extra, head, pending)  # what the chain this write makes reaches
    # What the write drops. A `__context__` write drops what `head` renders. A
    # `__cause__` write drops only a cause an earlier pass of this report sealed,
    # because the caller's own is refused just above and the slot is otherwise empty: a
    # cause written beside a populated context only stops the context printing, and the
    # link stays in the slot, where the two-link walk and `reported_errors` both still
    # reach it.
    if slot == "__cause__":
        held = prior if isinstance(prior, BaseException) else None
    else:
        held = _below(head, pending)
    if held is not None and not _holds(keeps, held) and not _holds(reached, held):
        end = keeps[-1]
        if (
            end is avoid
            or _read(end, "__cause__") is not None
            or _read(end, "__suppress_context__")
            or not _kept(_write(end, "__context__", held), refused)
        ):
            return False  # nowhere left to hold it, so leave it alone
    if not _kept(_write(head, slot, extra), refused):
        return False
    return slot == "__cause__" or _shown(head, refused)


def _anchor(
    head: BaseException,
    below: BaseException,
    refused: list[BaseException],
    avoid: BaseException | None,
    *,
    forced: bool,
    pending: _Edge | None = None,
) -> bool:
    """Put `below` into the chain `head` renders, at the deepest link that is free.

    The ordinary merge first, which writes only where nothing is dropped. When that
    cannot place it, every link above the deepest node is occupied by the chain itself,
    and the deepest link is refused only because the chain leads back through `head`,
    which is a repeat rather than a loss. `_keep` writes that one, so it is written only
    while the record is not carrying the report. Nothing is relocated on the way: the
    walk stopped at the deepest node, so what its own link holds is already reachable
    from `head`.

    The deepest link is taken twice over, because the chain can run back through
    `avoid`, the exception being reported, whose own two links `_seal` and the coming
    `raise` are going to write. A link past that point is one of theirs to overwrite, so
    the node the chain reaches without running through it is tried first, and the
    rendered end second and only when it is a different node.
    """
    chain = _rendered(head, pending=pending)
    stops = _rendered(head, avoid, pending) or chain
    if _merge(head, below, refused, avoid, forced=forced, pending=pending) or _keep(
        stops[-1], below, refused, avoid, forced=forced, reached=chain, pending=pending
    ):
        return True
    return (
        stops[-1] is not chain[-1]
        and chain[-1] is not avoid
        and _keep(
            chain[-1],
            below,
            refused,
            avoid,
            forced=forced,
            reached=chain,
            pending=pending,
        )
    )


def _crown(
    primary: BaseException,
    handler: BaseException | None,
    below: BaseException,
    refused: list[BaseException],
    *,
    forced: bool,
    pending: _Edge | None = None,
) -> BaseException:
    """Move the exception the coming `raise` will attach to the head of `below`.

    A `raise` inside an active `except` block writes that exception into `__context__`
    on the exception it raises, replacing whatever reporting put there. So once
    `__cause__` has been refused and `__context__` is the only slot left, the chain has
    to hang under that exception rather than above it. The raise then performs the link
    itself, and everything underneath survives it. Answers the head to keep building on.
    """
    if handler is None or handler is primary or handler is below:
        return below
    # It belongs at the head and nowhere else. What sat under it stays under it, so the
    # link that reached it is cut rather than spliced.
    _bypass(below, handler, refused, keeps=False, spare=primary, pending=pending)
    if _under(
        handler, below, refused, primary, forced=forced, pending=pending
    ) is not handler and not _keep(
        handler, below, refused, primary, forced=forced, pending=pending
    ):
        # A link into it could not be cut, so the chain still leads back to it and the
        # ordinary placement would leave the rest of that chain above it. The link
        # directly underneath is not free either, so the chain goes at the deepest one
        # it renders.
        _anchor(handler, below, refused, primary, forced=forced, pending=pending)
    return handler  # the head either way: the raise puts it there


def _seal(
    primary: BaseException,
    below: BaseException | None,
    handler: BaseException | None,
    refused: list[BaseException],
    *,
    forced: bool,
    pending: _Edge | None = None,
    owned: BaseException | None = None,
) -> BaseException | None:
    """Put `below` under `primary` where the coming `raise` cannot drop it.

    A `raise` inside an active `except` block overwrites `__context__` on the exception
    it raises, whatever that link already held, so `__cause__` is the only link that
    survives the raise. `forced` takes a free `__cause__` under an explicit suppression
    too, and is set only when the record is not there and the chain is the last place
    the reported failures can live.

    `owned` is the cause the caller's own exception arrived with, read before the first
    write. That one is never replaced. A cause an earlier pass of this report sealed is
    a different thing: the pass after it has a head that reaches further, and refusing
    to move the seal would leave everything the later pass collected hanging above a
    link nothing points at.
    """
    if below is None or below is primary:
        return below
    closed = not _bypass(below, primary, refused, pending=pending)
    prior = _read(primary, "__cause__")
    if isinstance(prior, BaseException) and (
        prior is owned or prior is below or not forced
    ):
        _anchor(
            primary,
            below,
            refused,
            None,  # extend the explicit
            forced=forced,
            pending=pending,
        )  # chain, never replace it
        return below
    if _read(primary, "__suppress_context__") and not forced:
        return below  # an explicit `raise ... from None` is kept
    if closed or prior is not None or _reaches(below, primary, pending):
        # The link that makes `primary` the head closes a rendered cycle, because
        # `primary` could not be taken out of the chain, or because a cause an earlier
        # pass of this report sealed is still in the slot. `_keep` writes both on the
        # durable slot, and only where nothing already reachable is dropped.
        sealed = _keep(
            primary,
            below,
            refused,
            None,
            forced=forced,
            slot="__cause__",
            pending=pending,
            owned=owned,
        )
    else:
        sealed = _kept(_write(primary, "__cause__", below), refused)
    if not sealed:
        # `__context__` is the slot that is left, and the coming `raise` overwrites it
        # with the exception it attaches, so the chain hangs under that one. When that
        # link is not free either, the deepest link `primary` renders is the last place
        # there is.
        below = _crown(primary, handler, below, refused, forced=forced, pending=pending)
        _anchor(primary, below, refused, None, forced=forced, pending=pending)
    return below


def _order(head: BaseException, pending: _Edge | None = None) -> list[BaseException]:
    """Every failure `_walk` reaches from `head`, nearest first, `head` included."""
    out: list[BaseException] = []
    queue = [head]
    while queue:
        node = queue.pop(0)
        if any(node is one for one in out):
            continue
        out.append(node)
        for name in ("__cause__", "__context__"):
            found = _seen(node, name, pending)
            if isinstance(found, BaseException):
                queue.append(found)
    return out


def _place(
    head: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    *,
    pending: _Edge | None = None,
) -> bool:
    """Write `extra` into the first empty link under `head` that it does not reach back.

    Nearest first, so the value lands as close to the head as there is room for. Nothing
    is dropped, because the slot is empty. No cycle is written, because a node `extra`
    itself reaches is passed over, and reachability is transitive, so that is the whole
    of the test. One write is attempted and no more: a surface that refused it is
    refusing.
    """
    for node in _order(head, pending):
        if pending is not None and node is pending[0]:
            continue  # the coming `raise` overwrites this one
        if _seen(node, "__context__", pending) is not None:
            continue
        if node is extra or _reaches(extra, node, pending):
            continue  # writing here would close a cycle
        return _kept(_write(node, "__context__", extra), refused)
    return False


def _stow(
    head: BaseException,
    extra: BaseException,
    refused: list[BaseException],
    *,
    pending: _Edge | None = None,
) -> bool:
    """Put `extra` in a link a traceback does not print, and drop nothing to do it.

    The last place there is, and the one every rendering placement passes over. The
    chain is built to be printed, so `_link` will not write a `__context__` under a
    populated `__cause__`: that link is never printed, and the chain walk does not
    follow it. It is still a link the caller reaches, which is the same thing `_keep`
    relies on when it writes a cause beside a populated context. So a value that no
    rendering placement would take goes into one of those rather than nowhere. This runs
    only while the record is not carrying the report, for the caller's own failures
    first and then for reporting's own refusals, which is the rank 9.4 states.

    When `extra` reaches every empty link there is, some link in its own chain is what
    put it there. That link is cut where what it holds is reachable from `head` anyway,
    which drops nothing and makes `extra` a leaf instead of a second head, and then
    there is somewhere to put it. The link is looked for through the whole of `extra`'s
    own chain rather than on `extra` alone, because a failure raised while handling
    another failure leads back through the middle one, and a cut made only on `extra`
    leaves that path in place and the failure with nowhere to go. One link is cut at a
    time and the placement is tried again after each, so no cut is made that the
    placement did not need, and the first refusal ends it.
    """
    if extra is head or _reaches(head, extra, pending):
        return True  # already reachable
    if _place(head, extra, refused, pending=pending):
        return True
    for node in _order(extra, pending):
        if node is not extra and not _reaches(extra, node, pending):
            continue  # an earlier cut already took this one off the chain
        for name in ("__cause__", "__context__"):
            found = _read(node, name)
            if not isinstance(found, BaseException):
                continue
            if not _reaches(head, found, pending):
                continue  # cutting here would drop what the link holds
            if not _kept(_write(node, name, None), refused):
                return False
            if _place(head, extra, refused, pending=pending):
                return True
    return False


def _also(
    rest: list[BaseException], exc: BaseException | None, raised: BaseException
) -> bool:
    """Add `exc` to the losers once, unless it is the exception being raised."""
    if exc is None or exc is raised or any(exc is other for other in rest):
        return False
    rest.append(exc)
    return True


def _reported(
    raised: BaseException, others: Sequence[BaseException]
) -> tuple[BaseException, ...]:
    """The report tuple: the raised failure first, then every distinct other one."""
    out = [raised]
    for exc in others:
        if not any(exc is other for other in out):
            out.append(exc)
    return tuple(out)


def _fold(
    raised: BaseException,
    origin: Sequence[BaseException],
    rest: list[BaseException],
    refused: list[BaseException],
    below: BaseException | None,
    sent: int,
    *,
    forced: bool = False,
    pending: _Edge | None = None,
) -> BaseException | None:
    """Offer the collected failures to the fallback chain, and answer its head.

    The caller's own failures are offered again on every pass, because a link one
    refused write could not make is not a failure the report may drop. Reporting's own
    refusals are offered from `sent` on, which the caller advances over the ones that
    are now somewhere the caller reaches. An offer is not a delivery: a placement can be
    declined, or made into a link a later write replaces, and a refusal counted as sent
    on the strength of the offer alone is one nothing ever puts in the report.
    """
    fresh = tuple(refused[sent:])
    for exc in fresh:  # a refused write is itself a teardown failure
        _also(rest, exc, raised)
    mark = len(refused)
    for exc in (*origin, *fresh):
        if exc is raised:
            continue
        below = (
            exc
            if below is None
            else _under(below, exc, refused, raised, forced=forced, pending=pending)
        )
        if len(refused) > mark:
            break  # the chain just refused, so it is not a channel
            # right now; the next pass offers the rest again
    return below


def _interrupt(
    raised: BaseException, refused: Sequence[BaseException]
) -> BaseException | None:
    """The refusal that must leave in place of `raised`, or None.

    Only an interruption outranks the selected failure, and only while that failure is
    still an ordinary one. This is `_primary`'s rule applied to reporting's own writes.
    An interruption once selected is no longer an `Exception`, so nothing can outrank it
    again and this choice is made at most once however many writes are refused.
    """
    if not isinstance(raised, Exception):
        return None
    for exc in refused:
        if not isinstance(exc, Exception):
            return exc
    return None


def _pass(
    raised: BaseException,
    origin: Sequence[BaseException],
    rest: list[BaseException],
    refused: list[BaseException],
    below: BaseException | None,
    handler: BaseException | None,
    sent: int,
    owned: BaseException | None,
) -> tuple[BaseException, BaseException | None, int]:
    """One report: promote whatever outranks the failure, then write both channels.

    Promotion comes first, so the record and the seal this pass writes are written for
    the exception that will actually leave, and never for one this pass is about to
    displace.
    """
    taken = _interrupt(raised, refused)
    if taken is not None:
        _also(rest, raised, taken)
        raised = taken  # the whole report now hangs under the interruption
    for exc in refused[sent:]:  # a refused write is itself a teardown failure
        _also(rest, exc, raised)  # `sent` stays put here: this pass has not put
        # any of them anywhere the caller reaches yet
    # The record is written before the chain, so every chain write in this pass knows
    # whether the record is carrying the report. That answer is what permits a cycle,
    # and a chain written before it is known would have to guess: guessing that the
    # record works loses a failure, and guessing that it does not hands back a graph a
    # caller who never had one now has to walk defensively.
    report = _reported(raised, rest)
    stored, refusal = _record(raised, report)
    if refusal is not None:
        refused.append(refusal)  # onto the other channel, in this same pass
    steady = len(refused)  # what the chain has refused before this pass writes to it
    below = _fold(
        raised,
        origin,
        rest,
        refused,
        below,
        sent,
        forced=not stored,
        pending=_edge(raised, handler),
    )
    below = _seal(
        raised,
        below,
        handler,
        refused,
        forced=not stored,
        pending=_edge(raised, handler),
        owned=owned,
    )
    # A refusal counts as sent once it is somewhere the caller reaches: the record this
    # pass proved by reading it back, or the graph that leaves once the raise has
    # written its own link. Advancing on the offer instead drops the one whose placement
    # was declined while later writes were still being accepted, because nothing offers
    # it a second time.
    if not stored:
        # The record is not carrying the report, so a failure or a refusal that nothing
        # placed has only the graph left. `_stow` uses the links the rendering
        # placements will not.
        mark = len(refused)
        # The caller's own failures are offered first, because 9.4 ranks them above
        # reporting's own refusals and a link given to a refusal is one a failure cannot
        # then have. They are offered only while the chain has accepted every write this
        # pass: a chain that just refused is not a channel right now, the offer would
        # spend a write attempt to learn what this pass already knows, and the next pass
        # offers them again.
        for exc in origin if mark == steady else ():
            if exc is raised:
                continue
            _stow(raised, exc, refused, pending=_edge(raised, handler))
            if len(refused) > mark:
                break  # refusing, so it is not a channel right now
        for exc in tuple(refused[sent:]):
            _stow(raised, exc, refused, pending=_edge(raised, handler))
            if len(refused) > mark:
                break  # refusing, so it is not a channel right now
    carried = _walk(raised, _edge(raised, handler))
    if stored:
        carried.update(id(exc) for exc in report)  # what the record was proved to hold,
        # which is not what `rest` holds now
    while sent < len(refused) and id(refused[sent]) in carried:
        sent += 1
    return raised, below, sent


# How many passes may have their own refusals answered by a later pass. A refusal has to
# be reported by a later write than the one that caused it, so one pass writes the
# report and the next reports what that pass refused. The loop stops on the first pass
# that refuses nothing, and after the budget one last pass runs that nothing answers, so
# reporting always ends.
_PASSES: Final = 3


def _report(errors: Sequence[BaseException], preferred: BaseException) -> NoReturn:
    """Raise one failure with every other failure reachable from it."""
    primary = _primary(errors, preferred)
    rest: list[BaseException] = []
    refused: list[BaseException] = []
    for exc in (preferred, *errors):  # a construction failure sits deepest
        _also(rest, exc, primary)
        # An earlier report raised `exc`, as a constructor that closes what it built
        # does inside a factory sweep. What that report covered stays covered, because
        # this report's record replaces the one on `exc` when `exc` is raised again.
        for covered in _stored(exc):
            _also(rest, covered, primary)
    # What the coming `raise` would attach, and both links `primary` already carried.
    # They are read once, here, before the first write changes either. Both, because the
    # raise destroys one and `_seal` writes the other, and because what a traceback
    # prints is one link of the two and is nothing at all under an explicit suppression,
    # while the graph the caller is handed still carries what the other link holds.
    handler = sys.exc_info()[1]
    cause = _read(primary, "__cause__")
    owned = cause if isinstance(cause, BaseException) else None
    held: list[BaseException] = []
    for found in (owned, _read(primary, "__context__")):
        if isinstance(found, BaseException) and not any(found is one for one in held):
            held.append(found)
    sources = (handler, *rest, *held)
    origin: list[BaseException] = [primary]
    for source in sources:
        _also(origin, source, primary)
    # What `primary` already carried is a failure the report covers, so it goes in the
    # record too. The raise can take it out of the chain and the two channels then
    # disagree.
    for exc in held:
        _also(rest, exc, primary)
    # The edge the coming `raise` will write, written here instead. Every question asked
    # below is then asked about the graph the caller actually gets: a link that looks
    # free, or a chain that looks acyclic, only because the raise has not run yet is a
    # wrong answer, and one that cannot be corrected afterwards because the raise is
    # last. A refused write does not stop the interpreter making that link, so `_edge`
    # carries it beside the graph instead and every walk below takes it in place of the
    # slot.
    if handler is not None and handler is not primary:
        _kept(_write(primary, "__context__", handler), refused)
    raised, below, sent = (
        primary,
        _stack(primary, sources, refused, pending=_edge(primary, handler)),
        0,
    )
    for _ in range(_PASSES):
        mark = len(refused)
        raised, below, sent = _pass(
            raised, origin, rest, refused, below, handler, sent, owned
        )
        if len(refused) == mark and sent == len(refused):
            raise raised  # nothing refused and nothing owed, so it stands
    raised, below, sent = _pass(  # answered by none
        raised, origin, rest, refused, below, handler, sent, owned
    )
    taken = _interrupt(raised, refused)
    raise taken if taken is not None else raised
