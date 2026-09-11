#!/usr/bin/env python3
"""Core purity check.

Fails when any module outside the pytest plugin package imports pytest.

``docs/reference/DESIGN_DECISIONS.md`` section 1 states the rule: the core
library imports no pytest, and only modules under the pytest plugin package may
import it. ``docs/reference/SPEC.md`` section 6 states the same rule for
``_core/`` and requires CI to enforce it. This script is that enforcement.

Usage::

    python3 scripts/check_core_purity.py [<package root>]
    python3 scripts/check_core_purity.py --self-test

The default package root is ``src/pytest_graphql`` relative to the repository
root. Exit codes: 0 clean, 1 findings reported, 2 usage or read error.

Stdlib only, so it runs before the project environment exists and in the
no-pytest job, which installs the distribution without the ``pytest`` extra.

The check is deliberately strict. A ``TYPE_CHECKING`` import is reported like
any other, because the rule is about what the source imports, not only about
what ends up in ``sys.modules``. A module that needs a pytest type belongs in
the plugin package.

Dynamic imports are read as far as a syntax tree allows. A call is judged by the
binding its callable resolves to, never by how the callable is spelled. These
forms are caught, each with a literal module name given as the first positional
argument or as the ``name`` keyword: ``import_module`` and ``__import__`` reached
as an attribute of the module they live in, the ``__import__`` builtin itself,
and ``import_module`` reached through a name bound by ``from importlib import
import_module as ...``.

A name is resolved the way Python resolves it: in the scope that evaluates the
expression, and at the point the expression runs.

Which scope owns a name follows the scope chain. The nearest enclosing scope
that binds it wins, so a local function, a parameter, a type parameter, or a
variable spelled like an importer is not one. A class body is skipped by a scope
nested inside it, with one exception: the scope a generic definition opens
directly inside a class body reads that class body, so a class-local importer is
reachable from an annotation, from a type parameter bound, and from the value of
a type alias written there. A class body is also the one scope that looks
further out when the call stands above every binding it makes, which is how
Python reads it too.

A name lives in a cell, not in a scope. The cell usually belongs to the scope
that binds the name. A ``global`` statement points the name at the cell of the
module, and a ``nonlocal`` statement at the cell of the nearest enclosing
function that binds that same name, which is not always the nearest enclosing
function. Every write keeps the scope it stands in, so a write beside the call
still fixes an order against it even when the cell belongs somewhere else.

A binding can also be taken away. A ``del`` statement unbinds the name, and so
does the end of an ``except ... as`` clause, which Python clears by itself.
Below an unbinding a class body and a module look further out, while a function
keeps a name of its own that is simply unbound. A name no scope binds is a
builtin, which is how an unshadowed ``__import__`` is recognised.

A generic definition opens one scope more than its body. The type parameters of
a generic function, class, or ``type`` alias are bound in a scope of their own,
and that scope evaluates the annotations, the class bases and keywords, and the
value of the alias. A type parameter spelled like an importer hides the importer
there, exactly as a parameter does inside a body.

Which scope evaluates an expression follows evaluation, not nesting. A
decorator, a parameter default, and the outermost iterable of a comprehension
are resolved in the scope the definition stands in. An annotation, a class base,
and a class keyword are resolved in the type parameter scope when the definition
is generic, and in the scope the definition stands in when it is not. None of
them is resolved in the body that definition introduces. A walrus runs where it
stands but binds where the comprehension around it stands, which is the one
binding that leaves a comprehension. A generator expression is the exception:
its body runs when the generator is consumed, so a walrus inside one binds at a
time the source does not fix.

Which binding is active follows the source order. When the call and the binding
sit in the same scope, the binding nearest above the call wins, so a rebinding
below the call cannot hide the import that happens above it. Inside a definition
that order is Python's own: the decorators, then the defaults, then the
annotations, then the name of the definition itself. An annotated assignment
stores its value before it evaluates its annotation. A bare annotation with no
value binds nothing, except that inside a function it still makes the name
local, so a use of it there reaches no outer binding. An augmented assignment
reads its target before the right-hand side and stores it last.

A dictionary display runs each key with its value, pair by pair, while the
syntax tree keeps every key in one list and every value in another. The two are
merged by position there. A call and a class header keep two lists as well, and
those are read in the order the tree stores them, because Python runs every
positional part of a call before any keyword part even where the source writes
them the other way round.

One write can settle the question on its own: a write in the scope of the call
that every path to the call passes. It replaces everything written above it,
including what another scope wrote, but only while nothing between it and the
call can run. Another scope writes a shared name only while it is running, and
it runs only where something lets it: a call, an attribute, an operator, a
branch, a loop, an unpacking, an import, and a definition that may be decorated
all reach code this script does not follow, and a plain name assignment reaches
none. Such a node runs its own code around its children rather than only before
them, so a write made inside one of them does not stand below it: the call in
``switch(load := safe)`` runs after the walrus it carries. So a rebinding
written beside the call with nothing between them wins, while a helper called in
between leaves the question open again.

Where the source does not fix an order, every binding of the name counts and the
call is reported if any of them is an importer. That covers a call in one scope
and the binding in another; a write another scope could have made in between; a
write a branch can skip, and one in a branch the call is not in, which can be
reached whenever the two are not sides of the same conditional; a write below
the call that a loop running both of them again can bring back, which counts
every part of one loop statement that runs again, so the body of a ``while``
reaches its next test; and a write below a call in a scope whose name is stored
elsewhere, which a second entry to that scope finds.

A call above every binding of the name is the other open case, and where it
leads depends on the scope that stores the name. A module and a class body read
the scope around them and in the end the builtins, so an unshadowed
``__import__`` is reported there. A function does not: reading a local name
before its first write raises, and reaches nothing outside.

These forms are not caught, because the module name or the callable itself is
not in the source: a name built at runtime, a name read from data, an importer
reached through a variable that was assigned rather than imported, and an
importer reached through an attribute of anything but the module it was imported
from. The rule those forms break is still the rule; this script is not the only
thing that enforces it.

Three further limits are deliberate. Which branch of a conditional runs is not
decided, only which branches can run beside each other, so a write in a branch
counts at a call the branch does not exclude even where the condition makes it
impossible. A helper called between a write and the call is not followed either,
so what the helper can write counts beside what stood before it, even where the
helper always leaves the name safe. Both limits report a call the interpreter
would not, which is the direction that rejects allowed code rather than the one
that lets a real import through. And an annotation is read as source even where
the interpreter never evaluates it, for the same reason a ``TYPE_CHECKING``
import is reported. Under ``from __future__ import annotations`` no annotation
runs, and under the deferred annotations of Python 3.14 one runs only when
something asks for it, later than its place in the source. An annotation is
judged where it stands in both cases, which reports more than either interpreter
would run.
"""

from __future__ import annotations

import ast
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

# Both the public package and its private mirror. A module reaching into
# ``_pytest`` is importing pytest by another name.
FORBIDDEN_ROOTS = frozenset({"pytest", "_pytest"})

# The one directory allowed to import pytest, relative to the package root.
ALLOWED_SUBPACKAGE = "plugin"

# The module each importer lives in, and the attribute it is reached by. A call
# of the form ``<name bound to this module>.<attribute>(...)`` imports by name.
IMPORTER_ATTRIBUTES = {"importlib": "import_module", "builtins": "__import__"}

# The importer that needs no import at all, because it is a builtin.
BUILTIN_IMPORTER = "__import__"

# Both importers spell their first parameter ``name``, so a keyword call binds
# the module through it.
DYNAMIC_NAME_KEYWORD = "name"

# What one name is bound to in one scope. ``MODULE_PREFIX`` is followed by the
# dotted module name, so ``import importlib`` binds ``module:importlib``.
IMPORTER = "importer"
OTHER = "other"
MODULE_PREFIX = "module:"

# The event a ``del`` statement and the end of an ``except ... as`` clause
# record. It is not something a call can resolve to. It ends the binding above
# it.
UNBOUND = "unbound"

# What a name resolves to when no scope in the chain binds it. Only the builtin
# importer matters here, and only while nothing has shadowed it.
BUILTIN = "builtin"

# A comprehension is a scope of its own in Python 3, so its target does not leak
# into the scope around it.
COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# The PEP 695 type alias statement. It arrived in Python 3.12, after the oldest
# supported version, so it is looked up rather than named.
TYPE_ALIAS_NODES = tuple(
    node for node in (getattr(ast, "TypeAlias", None),) if node is not None
)

# Every node type that opens a scope.
SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    *COMPREHENSION_NODES,
    *TYPE_ALIAS_NODES,
)

# The four kinds of scope. Which one a scope is decides how a name is looked up
# in it and where a declaration made in it lands.
MODULE_SCOPE = "module"
FUNCTION_SCOPE = "function"
COMPREHENSION_SCOPE = "comprehension"
CLASS_SCOPE = "class"
ANNOTATION_SCOPE = "annotation"

# The scopes a ``nonlocal`` statement can name. A comprehension is one of them,
# because a function nested in a comprehension closes over its target.
NONLOCAL_SCOPES = (FUNCTION_SCOPE, COMPREHENSION_SCOPE)

# The two statements that send a binding to another scope.
GLOBAL_KEYWORD = "global"
NONLOCAL_KEYWORD = "nonlocal"

# Nodes whose value is evaluated before the name they bind, against the field
# order the syntax tree stores them in. An annotated assignment and an augmented
# assignment are not here, because neither runs its parts in field order at all,
# so ``_Walk`` gives each one a visitor.
VALUE_FIRST: dict[type[ast.AST], tuple[str, ...]] = {
    ast.Assign: ("value",),
    ast.For: ("iter",),
    ast.AsyncFor: ("iter",),
}


class Branch(NamedTuple):
    """One field of a node that does not run beside the code around it."""

    field: str
    # True when the items of the field are branches of their own rather than
    # one block that runs together.
    alternatives: bool = False
    # True when the field runs again after the code below it. Every repeating
    # field of one statement shares one identity, so a write in the body of a
    # ``while`` reaches a call in its test.
    repeats: bool = False
    # True when no more than one branch of this node ever runs, so a write in
    # one of them cannot reach a call in another.
    exclusive: bool = False
    # True when the first item of the field runs whatever the code around it
    # does, as the left side of ``and`` and of ``or`` does, and as the test of
    # a ``while`` does. Such an item stands beside the code around it.
    first_runs: bool = False
    # The name this field uses in its frame key, when two fields of one node
    # run together and so make one branch. Empty means the field's own name.
    frame: str = ""


# Every field that may be skipped, chosen instead of a sibling, or run more
# than once. A write inside one of them is not certain to stand at a call
# outside it. A field that is not here runs where it is written.
BRANCHES: dict[type[ast.AST], tuple[Branch, ...]] = {
    ast.If: (Branch("body", exclusive=True), Branch("orelse", exclusive=True)),
    ast.IfExp: (Branch("body", exclusive=True), Branch("orelse", exclusive=True)),
    # The test of a ``while`` runs at least once and runs last before the loop
    # is left, so it stands beside the code around it rather than in a branch
    # of its own. The body may be skipped, so it does not.
    ast.While: (
        Branch("test", repeats=True, first_runs=True),
        Branch("body", repeats=True),
        Branch("orelse"),
    ),
    # The target of a ``for`` is bound again on every pass, right before the
    # body, so the two make one branch.
    ast.For: (
        Branch("target", repeats=True, frame="loop"),
        Branch("body", repeats=True, frame="loop"),
        Branch("orelse"),
    ),
    ast.AsyncFor: (
        Branch("target", repeats=True, frame="loop"),
        Branch("body", repeats=True, frame="loop"),
        Branch("orelse"),
    ),
    # A ``try`` excludes nothing: the body can run part way, then a handler,
    # then the ``finally`` clause.
    ast.Try: (
        Branch("body"),
        Branch("handlers", alternatives=True),
        Branch("orelse"),
        Branch("finalbody"),
    ),
    # ``and`` and ``or`` always run their left side, and each side after it
    # only while the sides before it allow.
    ast.BoolOp: (Branch("values", alternatives=True, first_runs=True),),
    ast.Match: (Branch("cases", alternatives=True, exclusive=True),),
}

# ``try ... except*`` arrived in Python 3.11, after the oldest version this
# script runs on, and its fields are the fields of ``try``.
_TRY_STAR = getattr(ast, "TryStar", None)
if _TRY_STAR is not None:
    BRANCHES[_TRY_STAR] = BRANCHES[ast.Try]

# The nodes that run no code of their own. Everything else is read as a point
# where another scope can run: a call, an attribute, an operator, a comparison,
# a branch, a loop over an iterator, an unpacking, an import, a comprehension,
# and a definition that may be decorated all reach code this script does not
# follow. A plain name assignment reaches none.
QUIET_NODES: tuple[type[ast.AST], ...] = (
    ast.Name,
    ast.Constant,
    ast.expr_context,
    ast.Expr,
    ast.Assign,
    ast.AnnAssign,
    ast.NamedExpr,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Return,
)


class Finding(NamedTuple):
    """One prohibited import."""

    path: Path
    line: int
    detail: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: {self.detail}"


# One region that runs again: the node it belongs to, and a fixed word, so
# every repeating field of one loop statement names the same region.
Repeat = tuple[int, str]


class Frame(NamedTuple):
    """One branch a node stands in, against the code around that branch."""

    # The node the branch belongs to, the field that holds it, and which item
    # of that field it is. Two fields that run together share one key.
    key: tuple[int, str, int]
    # True when no other branch of the same node runs beside this one.
    exclusive: bool


class Event(NamedTuple):
    """One write to one cell, and where that write stands in its own scope."""

    scope: Scope
    order: int
    token: str
    # The branches this write stands in, outermost first.
    frames: tuple[Frame, ...]
    # The loop regions that run this write again.
    repeats: tuple[Repeat, ...]


class Cell:
    """One storage location for one name, which several scopes may write to.

    A name declared ``global`` or ``nonlocal`` names the cell of another scope
    instead of one of its own. Each event keeps the scope it happens in, so a
    call can still use the order the source fixes inside that scope.
    """

    def __init__(self, owner: Scope) -> None:
        # The scope the name is stored in. Reading it before its first write
        # reaches the scope around a module and a class body, and raises in a
        # function, whatever scope the call itself runs in.
        self.owner = owner
        self.events: list[Event] = []


class Scope:
    """One Python binding scope, and the cell each name it uses lives in."""

    def __init__(self, parent: Scope | None, kind: str, lazy: bool = False) -> None:
        self.parent = parent
        self.kind = kind
        # True for a generator expression, whose body runs when the generator
        # is consumed rather than where the expression stands.
        self.lazy = lazy
        # The cell each name resolves to here. A declared name points at the
        # cell of another scope; every other name gets a cell of its own.
        self.cells: dict[str, Cell] = {}
        # Names this scope declares ``global`` or ``nonlocal``, each mapped to
        # the keyword that declared it.
        self.declared: dict[str, str] = {}
        # Every name this scope writes, whether it declared it or not.
        self.written: set[str] = set()
        # The position of every node in this scope that can run code of its
        # own, and so can let another scope write a name this one shares. The
        # walk appends in order, so the list stays sorted.
        self.noise: list[int] = []

    def owns(self, name: str) -> bool:
        """Return True when this scope binds ``name`` in a cell of its own.

        This is what a ``nonlocal`` statement below it searches for.
        """
        return name in self.written and name not in self.declared

    def cell(self, name: str) -> Cell:
        """Return the cell ``name`` uses here, creating this scope's own cell."""
        cell = self.cells.get(name)
        if cell is None:
            cell = Cell(self)
            self.cells[name] = cell
        return cell

    def module(self) -> Scope:
        """Return the module scope at the root of this chain."""
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope


class Write(NamedTuple):
    """One recorded write, before the declarations say which cell it lands in.

    ``scope`` is where the write happens in time. ``home`` is the scope whose
    name it writes. The two differ only for a walrus inside a generator
    expression, which names the scope around it but runs whenever the generator
    is consumed, not where it stands.
    """

    scope: Scope
    home: Scope
    name: str
    order: int
    token: str
    frames: tuple[Frame, ...]
    repeats: tuple[Repeat, ...]


class CallSite(NamedTuple):
    """One call, the scope it runs in, and its place in that scope."""

    node: ast.Call
    scope: Scope
    order: int
    # The branches the call stands in, outermost first.
    frames: tuple[Frame, ...]
    # The loop regions that run this call again.
    repeats: tuple[Repeat, ...]


def _nonlocal_target(scope: Scope, name: str) -> Scope | None:
    """Return the scope a ``nonlocal`` statement in ``scope`` names.

    Python takes the nearest enclosing function scope that binds the name
    itself, and passes over an enclosing function that does not bind it.
    """
    current = scope.parent
    while current is not None:
        if current.kind in NONLOCAL_SCOPES and current.owns(name):
            return current
        current = current.parent
    return None


def _link_declarations(scopes: list[Scope]) -> None:
    """Point every declared name at the cell of the scope that really owns it.

    Where a ``nonlocal`` name lands depends on which enclosing scopes bind that
    name anywhere in their body, so it is decided once the whole module has been
    walked rather than when the scope is opened.
    """
    for scope in scopes:
        for name, keyword in scope.declared.items():
            if keyword == GLOBAL_KEYWORD:
                target: Scope | None = scope.module()
            else:
                target = _nonlocal_target(scope, name)
            # A ``nonlocal`` with no enclosing binding does not compile, so a
            # miss here can only be a scope this walk does not model. Keep the
            # name in place rather than dropping what it writes.
            scope.cells[name] = (target or scope).cell(name)


def _file_writes(writes: list[Write]) -> None:
    """Put every recorded write in the cell its own scope resolves the name to."""
    for write in writes:
        cell = write.home.cell(write.name)
        cell.events.append(
            Event(write.scope, write.order, write.token, write.frames, write.repeats)
        )


def _excludes(one: tuple[Frame, ...], other: tuple[Frame, ...]) -> bool:
    """Return True when two places in one scope never both run.

    They part company at the first branch they do not share. Only a node that
    runs one branch at most keeps them apart there.
    """
    for first, second in zip(one, other, strict=False):
        if first == second:
            continue
        return first.exclusive and second.exclusive and first.key[0] == second.key[0]
    return False


def _quiet(scope: Scope, low: int, high: int) -> bool:
    """Return True when nothing between two points in ``scope`` can run code.

    Another scope writes a name it shares only while it is running, and it runs
    only where something in this scope lets it. Between a plain assignment and
    the call below it, nothing does.
    """
    return bisect_right(scope.noise, low) >= bisect_left(scope.noise, high)


def _certain(cell: Cell, site: CallSite) -> Event | None:
    """Return the last write in this scope that every path to the call passes.

    A write counts here only when the branches it stands in hold the call as
    well. A write inside a branch the call is outside of may not have run.
    """
    passed = [
        event
        for event in cell.events
        if event.scope is site.scope
        and event.order < site.order
        and site.frames[: len(event.frames)] == event.frames
    ]
    return max(passed, key=lambda event: event.order, default=None)


def _reaches(cell: Cell, event: Event, site: CallSite, certain: Event | None) -> bool:
    """Return True when ``event`` can be the write this call reads.

    A write that every path passes replaces what stands above it. It replaces
    what another scope wrote as well, but only while nothing between it and the
    call can run. What is left is what the source does not order against the
    call: a write another scope made while this one was not looking, a write a
    branch can skip, and a write below the call that a loop or a second entry
    to the scope can bring back.
    """
    if event.scope is not site.scope:
        # Where such a write stands in its own scope says nothing about when
        # that scope runs, so only the chance to run counts here.
        return certain is None or not _quiet(site.scope, certain.order, site.order)
    if _excludes(event.frames, site.frames):
        return False
    if certain is not None and event.order < certain.order:
        return False
    if event.order < site.order:
        return True
    if certain is None and cell.owner is not site.scope:
        # The scope can run again, and a name stored elsewhere keeps what the
        # last run wrote to it.
        return True
    repeats = set(event.repeats) & set(site.repeats)
    if certain is not None:
        # A loop that holds the certain write runs it again before the call.
        repeats -= set(certain.repeats)
    return bool(repeats)


def _live(cell: Cell, site: CallSite) -> tuple[set[str], bool]:
    """Return what ``cell`` can hold at this call, and whether to keep looking.

    Keeping looking means the name may have no binding yet. A name stored in a
    module or in a class body then reaches the scope around it, and in the end
    the builtins. A name stored in a function does not, because reading it
    unbound raises instead.
    """
    certain = _certain(cell, site)
    live = [event for event in cell.events if _reaches(cell, event, site, certain)]
    tokens = {event.token for event in live if event.token != UNBOUND}
    if certain is None or certain.token == UNBOUND:
        unbound = True
    else:
        unbound = any(
            event.token == UNBOUND and event.order != certain.order for event in live
        )
    return tokens, unbound and cell.owner.kind in (MODULE_SCOPE, CLASS_SCOPE)


def _visible(scope: Scope, innermost: bool, annotations_only: bool) -> bool:
    """Return True when a lookup that reached ``scope`` may read it.

    A class body is skipped by a scope nested inside it. The exception is the
    annotation scope a generic definition opens directly inside a class body,
    which reads that class body. A scope nested inside such an annotation scope
    is an ordinary nested scope again and skips the class.
    """
    if scope.kind != CLASS_SCOPE:
        return True
    return innermost or annotations_only


def _also_reads_the_class(scope: Scope) -> bool:
    """Return True for an annotation scope that a class body stands around.

    Python 3.12 and 3.13 let the type parameter win there, and Python 3.14 lets
    the class body win. Both are counted, so the call is reported when either
    one is an importer.
    """
    return (
        scope.kind == ANNOTATION_SCOPE
        and scope.parent is not None
        and scope.parent.kind == CLASS_SCOPE
    )


def _tokens(site: CallSite, name: str) -> set[str]:
    """Return every binding of ``name`` that can be active at this call."""
    tokens: set[str] = set()
    scope: Scope | None = site.scope
    innermost = True
    annotations_only = True
    while scope is not None:
        cell = scope.cells.get(name)
        if cell is not None and _visible(scope, innermost, annotations_only):
            found, keep_looking = _live(cell, site)
            tokens |= found
            if not keep_looking and not _also_reads_the_class(scope):
                return tokens
            if keep_looking and cell.owner is not scope:
                # A declared name is stored in another scope. When it may be
                # unbound, the rest of the lookup runs from the scope that
                # stores it, not from the one that only declared it.
                scope = cell.owner
                innermost = False
                annotations_only = False
        annotations_only = annotations_only and scope.kind == ANNOTATION_SCOPE
        innermost = False
        scope = scope.parent
    # Nothing in the chain binds the name, so a builtin answers for it.
    tokens.add(BUILTIN)
    return tokens


def _is_forbidden(module: str | None) -> bool:
    """Return True when ``module`` is pytest or lives under it."""
    if not module:
        return False
    return module.split(".", 1)[0] in FORBIDDEN_ROOTS


def _literal_str(node: ast.expr | None) -> str | None:
    """Return the value of a literal string expression, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the descendants of ``node`` that belong to its own scope.

    A nested scope is yielded so its name can be seen here, but it is not
    entered, so what it declares stays inside it.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, SCOPE_NODES):
            yield from _own_nodes(child)


def _parameters(args: ast.arguments) -> list[ast.arg]:
    """Return every parameter of one signature, in annotation order.

    Python evaluates the annotations of a signature in this order, so the list
    follows it.
    """
    collected: list[ast.arg | None] = [*args.posonlyargs, *args.args, args.vararg]
    collected += [*args.kwonlyargs, args.kwarg]
    return [parameter for parameter in collected if parameter is not None]


def _position(node: ast.AST) -> tuple[int, int]:
    """Return where ``node`` starts, for ordering parts the source interleaves."""
    line: int = getattr(node, "lineno", 0)
    column: int = getattr(node, "col_offset", 0)
    return line, column


def _in_source_order(*groups: Sequence[ast.AST | None]) -> list[ast.AST]:
    """Return the nodes of several parallel fields, in the order they are written.

    A dictionary display keeps its keys in one field and its values in another,
    and runs a key with the value beside it. Merging the two fields by position
    restores that order. A key is absent for ``**other``, and the value carries
    the position there.
    """
    nodes = [node for group in groups for node in group if node is not None]
    return sorted(nodes, key=_position)


def _deletes(node: ast.AST) -> bool:
    """Return True when ``node`` takes a name away instead of binding it."""
    return isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del)


def _binding_name(node: ast.AST) -> str | None:
    """Return the name ``node`` binds to something that is not an importer.

    Every way a name is bound at the node that binds it: an assignment or
    deletion target, and a match capture. A parameter and a caught exception
    bind at a point of their own, so ``_Walk`` places those two itself.
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return node.id
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


class _Walk:
    """One ordered pass over a module, in the scope each expression runs in."""

    def __init__(self) -> None:
        self._order = 0
        # The branches the walk stands in right now, outermost first.
        self._frames: tuple[Frame, ...] = ()
        # The loop regions around the walk right now.
        self._repeats: tuple[Repeat, ...] = ()
        self.sites: list[CallSite] = []
        self.scopes: list[Scope] = []
        self.writes: list[Write] = []

    def module(self, tree: ast.Module) -> None:
        """Walk a parsed module and record every call in it."""
        scope = self._open(tree, None, MODULE_SCOPE)
        self._visit_all(tree.body, scope)
        # Which cell a declared name uses is known only once every scope has
        # been seen, so the writes are filed after the walk rather than during
        # it. That keeps one writer for every cell.
        _link_declarations(self.scopes)
        _file_writes(self.writes)

    def _next(self) -> int:
        self._order += 1
        return self._order

    def _bind(
        self, scope: Scope, name: str, token: str, home: Scope | None = None
    ) -> None:
        """Record that ``scope`` binds ``name`` to ``token`` here.

        ``home`` names the scope the binding belongs to when that is not the
        scope the write runs in.
        """
        home = home or scope
        home.written.add(name)
        self.writes.append(
            Write(scope, home, name, self._next(), token, self._frames, self._repeats)
        )

    def _unbind(self, scope: Scope, name: str) -> None:
        """Record that ``name`` stops being bound in ``scope`` here."""
        self._bind(scope, name, UNBOUND)

    def _new(self, parent: Scope | None, kind: str, lazy: bool = False) -> Scope:
        """Create one scope and keep it for the declaration pass."""
        scope = Scope(parent, kind, lazy)
        self.scopes.append(scope)
        return scope

    def _open(
        self, node: ast.AST, parent: Scope | None, kind: str, lazy: bool = False
    ) -> Scope:
        """Create the scope of ``node`` and record the names it declares."""
        scope = self._new(parent, kind, lazy)
        for child in _own_nodes(node):
            if isinstance(child, ast.Global):
                keyword = GLOBAL_KEYWORD
            elif isinstance(child, ast.Nonlocal):
                keyword = NONLOCAL_KEYWORD
            else:
                continue
            for name in child.names:
                scope.declared[name] = keyword
        return scope

    def _visit_all(self, value: object, scope: Scope) -> None:
        """Visit one field of a node, which may be a node, a list, or nothing."""
        if isinstance(value, ast.AST):
            self.visit(value, scope)
        elif isinstance(value, list):
            for item in value:
                self._visit_all(item, scope)

    def visit(self, node: ast.AST, scope: Scope) -> None:
        """Visit ``node`` in the scope that evaluates it."""
        order = self._next()
        quiet = isinstance(node, QUIET_NODES)
        if not quiet:
            # This node can run code of its own, and that code can enter
            # another scope and write a name this one shares.
            scope.noise.append(order)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function(node, scope)
        elif isinstance(node, ast.Lambda):
            self._lambda(node, scope)
        elif isinstance(node, ast.ClassDef):
            self._class(node, scope)
        elif isinstance(node, COMPREHENSION_NODES):
            self._comprehension(node, scope)
        elif isinstance(node, ast.Import):
            self._import(node, scope)
        elif isinstance(node, ast.ImportFrom):
            self._import_from(node, scope)
        elif isinstance(node, ast.ExceptHandler):
            self._handler(node, scope)
        elif isinstance(node, ast.AnnAssign):
            self._ann_assign(node, scope)
        elif isinstance(node, ast.AugAssign):
            self._aug_assign(node, scope)
        elif isinstance(node, ast.NamedExpr):
            self._named_expr(node, scope)
        elif isinstance(node, TYPE_ALIAS_NODES):
            self._type_alias(node, scope)
        elif isinstance(node, ast.Dict):
            self._dict(node, scope)
        elif isinstance(node, ast.Call):
            # The callable is looked up before the arguments are evaluated, so
            # the call takes the position of the node itself.
            self.sites.append(CallSite(node, scope, order, self._frames, self._repeats))
            self._plain(node, scope)
        else:
            self._plain(node, scope)
        if not quiet:
            # Such a node also runs its own code after its children: a call
            # runs after its arguments, an operator after both sides. A write
            # a child makes therefore does not stand below it.
            scope.noise.append(self._next())

    def _plain(self, node: ast.AST, scope: Scope) -> None:
        """Visit the children of a node that opens no scope, then bind it."""
        first = VALUE_FIRST.get(type(node), ())
        branches = {branch.field: branch for branch in BRANCHES.get(type(node), ())}
        for field in first:
            self._visit_all(getattr(node, field, None), scope)
        for field, value in ast.iter_fields(node):
            if field in first:
                continue
            branch = branches.get(field)
            if branch is None:
                self._visit_all(value, scope)
            else:
                self._branch(node, branch, value, scope)
        bound = _binding_name(node)
        if bound:
            if _deletes(node):
                self._unbind(scope, bound)
            else:
                self._bind(scope, bound, OTHER)

    def _branch(
        self, node: ast.AST, branch: Branch, value: object, scope: Scope
    ) -> None:
        """Visit one field that the code around it does not run beside.

        Each item of a field of alternatives is a branch of its own. Every
        other field runs as one block. A definition nested in a branch keeps
        the branches around it, which can only add candidates to a call in its
        body, never take one away.
        """
        items: list[object]
        if branch.alternatives and isinstance(value, list):
            items = list(value)
        else:
            items = [value]
        saved = self._frames
        repeats = self._repeats
        if branch.repeats:
            self._repeats = (*repeats, (id(node), "repeat"))
        field = branch.frame or branch.field
        for index, item in enumerate(items):
            if branch.first_runs and index == 0:
                self._frames = saved
            else:
                frame = Frame((id(node), field, index), branch.exclusive)
                self._frames = (*saved, frame)
            self._visit_all(item, scope)
        self._frames = saved
        self._repeats = repeats

    def _dict(self, node: ast.Dict, scope: Scope) -> None:
        """Visit a dictionary display, one key and value pair at a time."""
        for child in _in_source_order(node.keys, node.values):
            self.visit(child, scope)

    def _handler(self, node: ast.ExceptHandler, scope: Scope) -> None:
        """Visit one ``except`` clause, which binds before its body runs.

        Python deletes the target once the handler ends, so the name the
        handler borrowed is gone below it, whatever it held before.
        """
        self._visit_all(node.type, scope)
        if node.name:
            self._bind(scope, node.name, OTHER)
        self._visit_all(node.body, scope)
        if node.name:
            self._unbind(scope, node.name)

    def _ann_assign(self, node: ast.AnnAssign, scope: Scope) -> None:
        """Visit ``target: annotation = value`` in the order Python runs it.

        The value runs first and is stored, so the annotation after it sees the
        new binding. With no value nothing is bound, except inside a function,
        where a bare annotation still makes the name local, so a use of it
        there reaches no outer binding.
        """
        self._visit_all(node.value, scope)
        binds = node.value is not None or scope.kind == FUNCTION_SCOPE
        if binds or not isinstance(node.target, ast.Name):
            self._visit_all(node.target, scope)
        self._visit_all(node.annotation, scope)

    def _aug_assign(self, node: ast.AugAssign, scope: Scope) -> None:
        """Visit ``target op= value``, which reads the target before the value.

        Python evaluates the target as an expression first, then the right-hand
        side, and stores last. Only a name target binds, and it binds at the end.
        """
        if isinstance(node.target, ast.Name):
            self._visit_all(node.value, scope)
            self._bind(scope, node.target.id, OTHER)
            return
        self._visit_all(node.target, scope)
        self._visit_all(node.value, scope)

    def _named_expr(self, node: ast.NamedExpr, scope: Scope) -> None:
        """Visit ``name := value``, which binds outside any comprehension.

        A walrus is the one binding that leaves the comprehension it stands in.
        It lands in the nearest scope around the comprehension, so the name is
        still bound once the comprehension is over. A generator expression is
        the exception in time rather than in place: its body runs when the
        generator is consumed, so a walrus there is not ordered against the code
        around it, and every binding of the name counts at a call below it.
        """
        self._visit_all(node.value, scope)
        target = scope
        lazy = False
        while target.kind == COMPREHENSION_SCOPE and target.parent is not None:
            lazy = lazy or target.lazy
            target = target.parent
        name = _binding_name(node.target)
        if name:
            self._bind(scope if lazy else target, name, OTHER, home=target)

    def _defaults(self, args: ast.arguments, scope: Scope) -> None:
        """Visit the defaults, which run where the definition stands."""
        self._visit_all(args.defaults, scope)
        self._visit_all(args.kw_defaults, scope)

    def _parameters_into(self, args: ast.arguments, scope: Scope) -> None:
        """Bind the parameter names, which belong to the body scope."""
        for parameter in _parameters(args):
            self._bind(scope, parameter.arg, OTHER)

    def _generic(self, node: ast.AST, scope: Scope) -> Scope:
        """Open the scope a PEP 695 type parameter list creates, if any.

        Returns the scope that evaluates the annotations, the class bases, or
        the value of a type alias: the new type parameter scope for a generic
        definition, and the enclosing scope for every other one.
        """
        params = getattr(node, "type_params", None) or []
        if not params:
            return scope
        inner = self._new(scope, ANNOTATION_SCOPE)
        for param in params:
            self._bind(inner, param.name, OTHER)
        for param in params:
            # A bound and a type parameter default are evaluated lazily, so
            # every type parameter is already bound by the time either runs.
            self._visit_all(getattr(param, "bound", None), inner)
            self._visit_all(getattr(param, "default_value", None), inner)
        return inner

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, scope: Scope
    ) -> None:
        # Python's own order: the decorators, then the defaults, which do not
        # see the type parameters, then the annotations, which do.
        self._visit_all(node.decorator_list, scope)
        header = self._generic(node, scope)
        self._defaults(node.args, scope)
        for parameter in _parameters(node.args):
            self._visit_all(parameter.annotation, header)
        self._visit_all(node.returns, header)
        self._bind(scope, node.name, OTHER)
        inner = self._open(node, header, FUNCTION_SCOPE)
        self._parameters_into(node.args, inner)
        self._visit_all(node.body, inner)

    def _lambda(self, node: ast.Lambda, scope: Scope) -> None:
        self._defaults(node.args, scope)
        inner = self._open(node, scope, FUNCTION_SCOPE)
        self._parameters_into(node.args, inner)
        self.visit(node.body, inner)

    def _class(self, node: ast.ClassDef, scope: Scope) -> None:
        self._visit_all(node.decorator_list, scope)
        header = self._generic(node, scope)
        # Every base runs before any keyword, as in a call, so the two fields
        # are read in the order the tree stores them.
        self._visit_all(node.bases, header)
        self._visit_all(node.keywords, header)
        inner = self._open(node, header, CLASS_SCOPE)
        self._visit_all(node.body, inner)
        # The class name is bound only once its body has run.
        self._bind(scope, node.name, OTHER)

    def _type_alias(self, node: ast.AST, scope: Scope) -> None:
        """Visit ``type Alias[T] = value``, whose value runs in the type scope."""
        header = self._generic(node, scope)
        self._visit_all(getattr(node, "value", None), header)
        # The alias name is bound in the scope the statement stands in.
        self._visit_all(getattr(node, "name", None), scope)

    def _comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        scope: Scope,
    ) -> None:
        outermost, rest = node.generators[0], node.generators[1:]
        # Only the outermost iterable runs where the comprehension stands.
        self.visit(outermost.iter, scope)
        inner = self._open(
            node, scope, COMPREHENSION_SCOPE, isinstance(node, ast.GeneratorExp)
        )
        self.visit(outermost.target, inner)
        self._visit_all(outermost.ifs, inner)
        for generator in rest:
            self.visit(generator.iter, inner)
            self.visit(generator.target, inner)
            self._visit_all(generator.ifs, inner)
        for field in ("elt", "key", "value"):
            self._visit_all(getattr(node, field, None), inner)

    def _import(self, node: ast.Import, scope: Scope) -> None:
        """Record what ``import x`` and ``import x.y as z`` bind."""
        for alias in node.names:
            if alias.asname:
                self._bind(scope, alias.asname, MODULE_PREFIX + alias.name)
            else:
                # ``import x.y`` binds only ``x``, and binds it to ``x``.
                root = alias.name.split(".", 1)[0]
                self._bind(scope, root, MODULE_PREFIX + root)

    def _import_from(self, node: ast.ImportFrom, scope: Scope) -> None:
        """Record what ``from x import y`` binds, marking the importers."""
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            imports_importer = (
                node.level == 0 and IMPORTER_ATTRIBUTES.get(module) == alias.name
            )
            self._bind(scope, bound, IMPORTER if imports_importer else OTHER)


def _is_importer_call(site: CallSite) -> bool:
    """Return True when the callable of this call resolves to a dynamic importer."""
    func = site.node.func
    if isinstance(func, ast.Name):
        tokens = _tokens(site, func.id)
        if IMPORTER in tokens:
            return True
        # An unbound ``__import__`` is the builtin. A bound one is whatever the
        # binding says, which is why the resolution comes first.
        return BUILTIN in tokens and func.id == BUILTIN_IMPORTER
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return any(
            token.startswith(MODULE_PREFIX)
            and IMPORTER_ATTRIBUTES.get(token[len(MODULE_PREFIX) :]) == func.attr
            for token in _tokens(site, func.value.id)
        )
    return False


def _dynamic_targets(node: ast.Call) -> list[str]:
    """Return every module name this importer call could name.

    The name reaches the importer either as the first positional argument or as
    the ``name`` keyword. A call that supplies both raises ``TypeError`` and
    imports nothing, so both bindings are returned and either one is reported.
    Nothing is lost by that, and a broken call beside this boundary is worth
    seeing.
    """
    candidates = [node.args[0] if node.args else None]
    candidates += [
        keyword.value
        for keyword in node.keywords
        if keyword.arg == DYNAMIC_NAME_KEYWORD
    ]
    return [value for value in map(_literal_str, candidates) if value is not None]


def scan_source(source: str, path: Path) -> list[Finding]:
    """Return every prohibited pytest import in ``source``."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 0
        return [Finding(path, line, f"could not parse: {exc.msg}")]

    walk = _Walk()
    walk.module(tree)

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    findings.append(Finding(path, node.lineno, f"imports {alias.name}"))
        # A relative import has no forbidden root by construction.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_forbidden(node.module)
        ):
            findings.append(Finding(path, node.lineno, f"imports from {node.module}"))

    for site in walk.sites:
        if not _is_importer_call(site):
            continue
        for target in _dynamic_targets(site.node):
            if _is_forbidden(target):
                findings.append(
                    Finding(path, site.node.lineno, f"imports {target} dynamically")
                )

    findings.sort()
    return findings


def scan_tree(package_root: Path) -> list[Finding]:
    """Return every prohibited pytest import under ``package_root``.

    Files under the allowed plugin subpackage are skipped. Everything else in
    the tree is scanned, including test helpers shipped inside the package.
    """
    allowed = package_root / ALLOWED_SUBPACKAGE
    findings: list[Finding] = []
    for path in sorted(package_root.rglob("*.py")):
        if path == allowed or allowed in path.parents:
            continue
        findings.extend(scan_source(path.read_text(encoding="utf-8"), path))
    return findings


_SELF_TEST_CASES: tuple[tuple[str, str, bool], ...] = (
    ("plain import", "import pytest\n", True),
    ("aliased import", "import pytest as pt\n", True),
    ("submodule import", "import pytest.mark\n", True),
    ("from import", "from pytest import fixture\n", True),
    ("private mirror", "from _pytest.config import Config\n", True),
    (
        "type-checking import",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pytest\n",
        True,
    ),
    (
        "dynamic import_module",
        'import importlib\nimportlib.import_module("pytest")\n',
        True,
    ),
    ("dunder import", '__import__("pytest")\n', True),
    (
        "dynamic import_module by keyword",
        'import importlib\nimportlib.import_module(name="pytest")\n',
        True,
    ),
    ("dunder import by keyword", '__import__(name="pytest")\n', True),
    (
        "dynamic import through a local alias",
        'from importlib import import_module\nimport_module("pytest")\n',
        True,
    ),
    (
        "dynamic import through a renamed alias",
        'from importlib import import_module as load\nload("_pytest.config")\n',
        True,
    ),
    (
        "keyword name on a renamed alias",
        'from importlib import import_module as load\nload(name="pytest")\n',
        True,
    ),
    (
        "duplicate binding is still reported",
        'import importlib\nimportlib.import_module("pytest", name="pytest")\n',
        True,
    ),
    (
        "an importlib module alias",
        'import importlib as il\nil.import_module("pytest")\n',
        True,
    ),
    (
        "a submodule import still binds importlib",
        'import importlib.util\nimportlib.import_module("pytest")\n',
        True,
    ),
    (
        "the builtins attribute importer",
        'import builtins\nbuiltins.__import__("pytest")\n',
        True,
    ),
    (
        "an alias used in the function that binds it",
        "def run():\n    from importlib import import_module as load\n"
        '    return load("pytest")\n',
        True,
    ),
    (
        "an alias reaching a nested function",
        "def outer():\n    from importlib import import_module as load\n\n"
        '    def inner():\n        return load("pytest")\n\n    return inner\n',
        True,
    ),
    (
        "an import before the reassignment",
        'from importlib import import_module as load\nload("pytest")\nload = None\n',
        True,
    ),
    (
        "a global declaration does not hide the import",
        "from importlib import import_module as load\n\n\n"
        "def reset():\n    global load\n\n    load = None\n\n\n"
        'load("pytest")\n',
        True,
    ),
    (
        "an unrelated global declaration does not hide the import",
        "import importlib\n\n\n"
        "def reset():\n    global importlib\n\n    importlib = None\n\n\n"
        'importlib.import_module("pytest")\n',
        True,
    ),
    (
        "an unrelated nonlocal declaration does not hide the import",
        "from importlib import import_module as load\n\n\n"
        "def outer():\n    load = None\n\n"
        "    def inner():\n        nonlocal load\n\n        load = print\n\n"
        "    return inner\n\n\n"
        'load("pytest")\n',
        True,
    ),
    (
        "a decorator runs outside the body it decorates",
        "from importlib import import_module as load\n\n\n"
        '@load("pytest")\ndef run():\n    load = None\n\n    return load\n',
        True,
    ),
    (
        "a parameter default runs outside the body",
        "from importlib import import_module as load\n\n\n"
        'def run(value=load("pytest")):\n    load = None\n\n    return load, value\n',
        True,
    ),
    (
        "a lambda default runs outside the lambda",
        "from importlib import import_module as load\n"
        'run = lambda load=load("pytest"): load\n',
        True,
    ),
    (
        "a class base runs outside the class body",
        "from importlib import import_module as load\n\n\n"
        'class Thing(load("pytest")):\n    load = None\n',
        True,
    ),
    (
        "the outermost iterable runs outside the comprehension",
        "from importlib import import_module as load\n"
        'VALUES = [load for load in load("pytest")]\n',
        True,
    ),
    (
        "another keyword does not bind the module",
        'import importlib\nimportlib.import_module("httpx", package="pytest")\n',
        False,
    ),
    (
        "an unrelated call with a name keyword",
        'record(name="pytest")\n',
        False,
    ),
    (
        "an unrelated module aliased from importlib",
        'from importlib import metadata\nmetadata("pytest")\n',
        False,
    ),
    (
        "an alias reassigned in its own scope",
        "from importlib import import_module as load\n"
        'load = lambda name: name\nload("pytest")\n',
        False,
    ),
    (
        "an alias bound in a sibling function",
        "def a():\n    from importlib import import_module as load\n\n"
        "    return load\n\n"
        "def b():\n    def load(name):\n        return name\n\n"
        '    return load("pytest")\n',
        False,
    ),
    (
        "a local function named like the importer",
        'def import_module(name):\n    return name\n\n\nimport_module("pytest")\n',
        False,
    ),
    (
        "a parameter shadowing an outer alias",
        "from importlib import import_module as load\n\n\n"
        'def run(load):\n    return load("pytest")\n',
        False,
    ),
    (
        "a shadowed builtin importer",
        'def run(__import__):\n    return __import__("pytest")\n',
        False,
    ),
    (
        "an attribute call on an unrelated object",
        'def run(loader):\n    return loader.import_module("pytest")\n',
        False,
    ),
    (
        "a class attribute is not the importlib module",
        "class Loader:\n    import_module = None\n\n"
        '    def run(self):\n        return self.import_module("pytest")\n',
        False,
    ),
    (
        "an annotation does not see the body of its own function",
        'def run(value: load("pytest")) -> None:\n'
        "    from importlib import import_module as load\n\n    return None\n",
        False,
    ),
    (
        "a walrus leaves the comprehension it stands in",
        "from importlib import import_module as load\n"
        "VALUES = [(load := print) for value in range(1)]\n"
        'load("pytest")\n',
        False,
    ),
    (
        "an inner iterable stays in the comprehension",
        'def run(rows):\n    return [load("pytest") for load in rows]\n',
        False,
    ),
    (
        "a class body looks out before it binds",
        "import importlib\n\n\n"
        'class Thing:\n    value = importlib.import_module("pytest")\n\n'
        "    importlib = None\n",
        True,
    ),
    (
        "an except clause binds before its body",
        "from importlib import import_module as load\n\n"
        "try:\n    pass\nexcept ValueError as load:\n"
        '    load("pytest")\n',
        False,
    ),
    (
        "a nonlocal passes over a function that does not bind the name",
        "def outer():\n    from importlib import import_module as load\n\n"
        "    def middle():\n        def inner():\n            nonlocal load\n\n"
        '            load = load("pytest")\n\n        return inner\n\n'
        "    return middle\n",
        True,
    ),
    (
        "a nonlocal stops at the function that does bind the name",
        "from importlib import import_module as load\n\n\n"
        "def outer():\n    load = print\n\n"
        "    def inner():\n        nonlocal load\n\n"
        '        load = load("pytest")\n\n    return inner\n',
        False,
    ),
    (
        "a default runs before the annotation beside it",
        "from importlib import import_module as load\n\n\n"
        'def run(first=(load := print), *, second: load("pytest") = None):\n'
        "    return first, second\n",
        False,
    ),
    (
        "an annotated assignment stores before it annotates",
        'from importlib import import_module as load\nload: load("pytest") = print\n',
        False,
    ),
    (
        "a bare annotation binds nothing at module level",
        'from importlib import import_module as load\nload: None\nload("pytest")\n',
        True,
    ),
    (
        "a bare annotation makes the name local in a function",
        "from importlib import import_module as load\n\n\n"
        'def run():\n    load: None\n\n    return load("pytest")\n',
        False,
    ),
    (
        "a global write beside the call orders it",
        "from importlib import import_module as load\n\n\n"
        "def run():\n    global load\n\n    load = print\n\n"
        '    return load("pytest")\n',
        False,
    ),
    (
        "a global write beside the call can be the importer",
        "load = print\n\n\n"
        "def run():\n    global load\n\n"
        "    from importlib import import_module as load\n\n"
        '    return load("pytest")\n',
        True,
    ),
    (
        "a nonlocal write beside the call orders it",
        "def outer():\n    from importlib import import_module as load\n\n"
        "    def inner():\n        nonlocal load\n\n        load = print\n\n"
        '        return load("pytest")\n\n    return inner\n',
        False,
    ),
    (
        "an augmented assignment reads its target before its value",
        "from importlib import import_module as load\n\n\n"
        "class Box:\n    def __getitem__(self, key):\n        return 0\n\n"
        "    def __setitem__(self, key, value):\n        pass\n\n\n"
        'box = Box()\nbox[load("pytest")] += (load := print)\n',
        True,
    ),
    (
        "an augmented assignment stores its target last",
        "from importlib import import_module as load\n\n\n"
        "class Box:\n    def __getitem__(self, key):\n        return 0\n\n"
        "    def __setitem__(self, key, value):\n        pass\n\n\n"
        'box = Box()\nbox[(load := print)] += load("pytest")\n',
        False,
    ),
    (
        "a dictionary runs each key with the value beside it",
        "from importlib import import_module as load\n"
        'VALUES = {0: load("pytest"), (load := print): 1}\n',
        True,
    ),
    (
        "a dictionary key below a value sees the new binding",
        "from importlib import import_module as load\n"
        'VALUES = {0: (load := print), load("pytest"): 1}\n',
        False,
    ),
    (
        "a deletion in a class body reaches the scope around it",
        "from importlib import import_module as load\n\n\n"
        "class Thing:\n    load = print\n    del load\n\n"
        '    value = load("pytest")\n',
        True,
    ),
    (
        "an except target is cleared once its handler ends",
        "from importlib import import_module as load\n\n\n"
        "class Thing:\n    try:\n        raise RuntimeError\n"
        "    except RuntimeError as load:\n        pass\n\n"
        '    value = load("pytest")\n',
        True,
    ),
    (
        "a deletion in a function leaves a local name unbound",
        "from importlib import import_module as load\n\n\n"
        "def run():\n    load = print\n    del load\n\n"
        '    return load("pytest")\n',
        False,
    ),
    (
        "a deleted name reaches the builtin importer again",
        '__import__ = print\ndel __import__\n__import__("pytest")\n',
        True,
    ),
    (
        "a walrus in a generator expression is not ordered against the call",
        "from importlib import import_module as load\n"
        "ROWS = ((load := print) for value in range(1))\n"
        'load("pytest")\n',
        True,
    ),
    (
        "a helper called between a global write and the call",
        "def helper():\n    global load\n\n"
        "    from importlib import import_module as load\n\n\n"
        "load = print\n\n\n"
        "def run():\n    global load\n\n    load = print\n    helper()\n\n"
        '    return load("pytest")\n',
        True,
    ),
    (
        "a helper called between a nonlocal write and the call",
        "def outer():\n    load = print\n\n"
        "    def helper():\n        nonlocal load\n\n"
        "        from importlib import import_module as load\n\n"
        "    def run():\n        nonlocal load\n\n"
        "        load = print\n        helper()\n\n"
        '        return load("pytest")\n\n    return run\n',
        True,
    ),
    (
        "a second entry to a function finds the global written",
        "load = print\n\n\n"
        "def run():\n    global load\n\n"
        '    value = load("pytest")\n'
        "    from importlib import import_module as load\n\n    return value\n",
        True,
    ),
    (
        "a call above every binding of a local name is unbound",
        "def run():\n"
        '    value = load("pytest")\n'
        "    from importlib import import_module as load\n\n    return value\n",
        False,
    ),
    (
        "the builtin importer above a later shadow",
        '__import__("pytest")\n__import__ = print\n',
        True,
    ),
    (
        "a declared global reaches the builtin past the scope around it",
        "def outer():\n    __import__ = print\n\n"
        "    def go():\n        global __import__\n\n"
        '        return __import__("pytest")\n\n    return go\n',
        True,
    ),
    (
        "a loop reaching a rebinding below the call",
        "load = print\nfor index in range(2):\n"
        '    load("pytest")\n'
        "    from importlib import import_module as load\n",
        True,
    ),
    (
        "a rebinding a branch may skip does not hide the import",
        "from importlib import import_module as load\n\n"
        "if not print:\n    load = print\n\n"
        'load("pytest")\n',
        True,
    ),
    (
        "a rebinding in the other branch does not reach the call",
        "from importlib import import_module as load\n\n"
        "if not print:\n    load = print\nelse:\n"
        '    load("pytest")\n',
        True,
    ),
    (
        "the left side of an and runs before the right",
        "from importlib import import_module as load\n"
        'VALUES = (load := print) and load("pytest")\n',
        False,
    ),
    (
        "a call runs after the argument that writes the name",
        "def switch(value):\n    global load\n\n"
        "    from importlib import import_module as load\n\n\n"
        "load = print\n\n\n"
        "def run():\n    global load\n\n"
        "    switch(load := print)\n"
        '    return load("pytest")\n',
        True,
    ),
    (
        "a while body reaches the next test",
        "def run(counter):\n    load = print\n"
        '    while load("pytest"):\n'
        "        counter -= 1\n"
        "        if counter < 0:\n            load = print\n"
        "        else:\n"
        "            from importlib import import_module as load\n",
        True,
    ),
    (
        "a while test stands beside the code below it",
        "from importlib import import_module as load\n\n"
        "while (load := print):\n    pass\n\n"
        'load("pytest")\n',
        False,
    ),
    (
        "a while test rebinding hides a later import in the body",
        "def run():\n    load = print\n"
        '    while (load := print) and load("pytest"):\n'
        "        from importlib import import_module as load\n",
        False,
    ),
    (
        "a for target is bound again before every pass",
        "from importlib import import_module as load\n\n"
        "for load in range(1):\n"
        '    load("pytest")\n',
        False,
    ),
    (
        "a for target may never be bound at all",
        "from importlib import import_module as load\n\n"
        "for load in range(1):\n    pass\n\n"
        'load("pytest")\n',
        True,
    ),
    ("unrelated import", "import httpx\n", False),
    ("similar name", "import pytest_graphql_helper\n", False),
    ("relative import", "from .client import GraphQLClient\n", False),
    ("string mention", 'DOC = "install pytest to use the plugin"\n', False),
)

# The Python version that first parses PEP 695 generic syntax. The cases below
# it are not skipped for being unimportant: an older interpreter cannot parse
# their source at all, so running them there would only report a syntax error.
GENERIC_SYNTAX = (3, 12)

_GENERIC_CASES: tuple[tuple[str, str, bool], ...] = (
    (
        "a type parameter hides the importer module in an annotation",
        "import importlib\n\n\n"
        'def run[importlib](value: importlib.import_module("pytest")) -> None:\n'
        "    return None\n",
        False,
    ),
    (
        "a generic annotation still reaches the importer module",
        "import importlib\n\n\n"
        'def run[T](value: importlib.import_module("pytest")) -> None:\n'
        "    return None\n",
        True,
    ),
    (
        "a type parameter does not reach a default",
        "import importlib\n\n\n"
        'def run[importlib](value=importlib.import_module("pytest")) -> None:\n'
        "    return None\n",
        True,
    ),
    (
        "a type parameter hides the importer module in a bound",
        "import importlib\n\n\n"
        'def run[importlib, T: importlib.import_module("pytest")]() -> None:\n'
        "    return None\n",
        False,
    ),
    (
        "a type parameter bound still reaches the importer module",
        "import importlib\n\n\n"
        'def run[T: importlib.import_module("pytest")]() -> None:\n'
        "    return None\n",
        True,
    ),
    (
        "a type parameter hides the importer module in a class base",
        "import importlib\n\n\n"
        'class Thing[importlib](importlib.import_module("pytest")):\n'
        "    pass\n",
        False,
    ),
    (
        "a type parameter hides the importer module in an alias value",
        "import importlib\n\n"
        'type Alias[importlib] = importlib.import_module("pytest")\n',
        False,
    ),
    (
        "a class body reaches an annotation nested in it",
        "class Thing:\n    import importlib\n\n"
        '    def method[T](self) -> importlib.import_module("pytest"):\n'
        "        return None\n",
        True,
    ),
    (
        "a class body reaches a type parameter bound nested in it",
        "class Thing:\n    import importlib\n\n"
        '    def method[T: importlib.import_module("pytest")](self):\n'
        "        return None\n",
        True,
    ),
    (
        "a class body reaches the value of a type alias nested in it",
        "class Thing:\n    import importlib\n\n"
        '    type Alias[T] = importlib.import_module("pytest")\n',
        True,
    ),
    (
        "a type parameter beside a class-local importer is still reported",
        # Python 3.12 and 3.13 let the type parameter win here, and Python 3.14
        # lets the class body win. The call is reported for the version that
        # imports.
        "class Thing:\n    import importlib\n\n"
        '    def method[importlib](self) -> importlib.import_module("pytest"):\n'
        "        return None\n",
        True,
    ),
    (
        "a type alias value still reaches the importer module",
        'import importlib\n\ntype Alias[T] = importlib.import_module("pytest")\n',
        True,
    ),
)


def self_test() -> int:
    """Check the detector against cases it must and must not report."""
    cases = list(_SELF_TEST_CASES)
    skipped = 0
    if sys.version_info >= GENERIC_SYNTAX:
        cases += _GENERIC_CASES
    else:
        skipped = len(_GENERIC_CASES)

    failures = 0
    for name, source, should_report in cases:
        # A case that does not compile would be judged on a parse error rather
        # than on what it does, which reads as a pass for the wrong reason.
        try:
            compile(source, "<case>", "exec")
        except SyntaxError as exc:
            print(f"FAIL {name}: the case itself does not compile: {exc.msg}")
            failures += 1
            continue
        reported = bool(scan_source(source, Path("<case>")))
        if reported != should_report:
            want = "a finding" if should_report else "no finding"
            print(f"FAIL {name}: expected {want}")
            failures += 1
    total = len(cases)
    if failures:
        print(f"\n{failures} of {total} self-test case(s) failed.")
        return 1
    note = ""
    if skipped:
        version = ".".join(str(part) for part in GENERIC_SYNTAX)
        note = f" {skipped} generic case(s) need Python {version} or newer."
    print(f"self-test: {total} of {total} cases passed.{note}")
    return 0


def default_package_root() -> Path:
    """Return the package root relative to this script's repository."""
    return Path(__file__).resolve().parent.parent / "src" / "pytest_graphql"


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--self-test":
        if len(argv) > 1:
            print("error: --self-test takes no paths", file=sys.stderr)
            return 2
        return self_test()

    if len(argv) > 1:
        print("error: at most one package root", file=sys.stderr)
        return 2

    root = Path(argv[0]).resolve() if argv else default_package_root()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    findings = scan_tree(root)
    if not findings:
        print(
            f"core purity: no pytest import outside {root.name}/{ALLOWED_SUBPACKAGE}/"
        )
        return 0

    print(f"core purity: {len(findings)} prohibited pytest import(s)", file=sys.stderr)
    for finding in findings:
        print(finding.render(root.parent), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
