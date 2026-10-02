"""The C58 omission records, end to end through the client (5.4).

Auto-selection records every field it removes, with the parent type, the
path relative to the document and the reason, and the records reach the
request and its snapshot. These tests cover every reason, every rule that
reduces a selection rather than removing a field, path rebasing under
nesting, a cached selection spliced at two positions, and truncation.

The completeness check walks each generated document against the schema. A
field of a selected object type is accounted for when the document selects
it, when a record names it, or when records name fields below it, because an
expansion that came back empty removes its field through its children. A
record that names a field the document selects is a false report, and the
same walk finds it. It runs over every cycle policy and several depths, so a
rule that drops a field silently, or records one it kept, fails here even
when no targeted test names that rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from graphql import (
    FieldNode,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    parse,
)
from graphql import build_schema as build_graphql_schema

from pytest_graphql import AUTO, Field, Selection
from pytest_graphql._core.client import ClientConfig, GraphQLClient
from pytest_graphql._core.diagnostics import MAX_OMISSION_RECORDS, RequestInfo
from pytest_graphql._core.selection import builder as builder_module
from pytest_graphql._core.selection.policy import is_connection_type
from tests.schema.fake_transport import FakeGraphQLTransport

_CAP = 10  # SelectionPolicy.max_union_members' default
_MEMBERS = "\n".join(f"type M{i} {{ id: ID name: String }}" for i in range(_CAP + 1))

#: More fields than the record bound holds, each skipped for its required
#: argument, so one walk of ``Many`` is already truncated on its own.
_MANY_COUNT = MAX_OMISSION_RECORDS + 10
_MANY = "\n".join(f"  f{i}(need: String!): String" for i in range(_MANY_COUNT))

#: Implementations of ``Named``. Only the first and the capped last one have
#: fields the interface does not, so the rest add members without size.
_PLAIN = "\n".join(
    f"type N{i} implements Named {{ id: ID name: String }}" for i in range(1, _CAP)
)

_SDL = f"""
type PageInfo {{ hasNextPage: Boolean endCursor: String }}
type Inner {{ id: ID }}
type InnerEdge {{ cursor: String node: Inner }}
type InnerConnection {{ edges: [InnerEdge] pageInfo: PageInfo }}
type Item {{ id: ID nested(first: Int): InnerConnection }}
type ItemEdge {{ cursor: String node: Item }}
type ItemConnection {{ edges: [ItemEdge] pageInfo: PageInfo }}
type Leaf {{ id: ID }}
type Mid {{ id: ID leaf: Leaf }}
type Other {{ id: ID }}
union Related = Report | Other
{_MEMBERS}
union Wide = {" | ".join(f"M{i}" for i in range(_CAP + 1))}

type Report {{
  id: ID
  title: String
  needsArg(x: String!): String
  old: String @deprecated(reason: "gone")
  unpaged: ItemConnection
  paged(first: Int): ItemConnection
  mid: Mid
  parent: Report
  related: Related
  wide: Wide
}}

type Many {{
  id: ID
{_MANY}
}}

type Holder {{ a: Report b: Report c: Report many: Many }}

interface Named {{ id: ID name: String }}
type N0 implements Named {{ id: ID name: String extra: String back: Named }}
{_PLAIN}
type N{_CAP} implements Named {{ id: ID name: String extra: String }}

type Child {{ id: ID }}
interface Unimplemented {{ id: ID child: Child }}

type Query {{
  report: Report
  holder: Holder
  many: Many
  named: Named
  n0: N0
  unimplemented: Unimplemented
}}
"""

Record = tuple[str, tuple[str, ...], str]


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_graphql_schema(_SDL)


def _client(schema: GraphQLSchema, **config: Any) -> GraphQLClient:
    return GraphQLClient(
        transport=FakeGraphQLTransport(schema),
        schema=schema,
        config=ClientConfig(url="https://example.test/graphql", **config),
    )


def _sent(client: GraphQLClient) -> RequestInfo:
    transport = client.transport
    assert isinstance(transport, FakeGraphQLTransport)
    return transport.sent[-1]


def _records(request: RequestInfo) -> list[Record]:
    """The snapshot's records, which is what a report reads."""
    return [
        (record.parent_type, record.path, record.reason)
        for record in request.redacted().omissions
    ]


# -- every reason --------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param(
            {}, ("Report", ("report", "needsArg"), "required-argument"), id="argument"
        ),
        pytest.param({}, ("Report", ("report", "old"), "deprecated"), id="deprecated"),
        pytest.param(
            {"exclude": ("Report.title",)},
            ("Report", ("report", "title"), "should-include"),
            id="should-include",
        ),
        pytest.param(
            {},
            ("Report", ("report", "unpaged"), "connection-page-size"),
            id="page-size",
        ),
        pytest.param(
            {},
            (
                "Item",
                ("report", "paged", "edges", "node", "nested"),
                "connection-depth",
            ),
            id="connection-depth",
        ),
        pytest.param(
            {"max_depth": 2}, ("Mid", ("report", "mid", "leaf"), "depth"), id="depth"
        ),
        pytest.param(
            {"cycle_policy": "stop"},
            ("Report", ("report", "parent"), "cycle"),
            id="cycle",
        ),
        pytest.param(
            {},
            (f"M{_CAP}", ("report", "wide", "name"), "union-member-cap"),
            id="union-member-cap",
        ),
    ],
)
def test_every_reason_reaches_the_snapshot_and_the_report(
    schema: GraphQLSchema, config: dict[str, Any], expected: Record
) -> None:
    client = _client(schema, **config)

    client.query("report")

    assert expected in _records(_sent(client))
    parent, path, reason = expected
    assert f"{parent}.{'.'.join(path)} ({reason})" in client.recorder.dump()


# -- rules that reduce a selection ----------------------------------------------


def _report_fields(schema: GraphQLSchema, *, keep: frozenset[str]) -> set[Record]:
    """Each ``Report`` field a reducing rule removes, under the reason it gets.

    ``old`` is deprecated, which would have removed it anyway, so that is
    the reason it carries.
    """
    report = schema.get_type("Report")
    assert isinstance(report, GraphQLObjectType)
    return {
        ("Report", (name,), "deprecated" if name == "old" else "cycle")
        for name in report.fields
        if name not in keep
    }


def _under(records: list[Record], prefix: tuple[str, ...]) -> set[Record]:
    """The records directly below ``prefix``, with the prefix taken off."""
    return {
        (parent, path[len(prefix) :], reason)
        for parent, path, reason in records
        if path[: len(prefix)] == prefix and len(path) == len(prefix) + 1
    }


def test_stop_records_every_field_of_a_member_fragment_it_removes(
    schema: GraphQLSchema,
) -> None:
    # A fragment has no field of its own, so its fields are what it loses.
    client = _client(schema, cycle_policy="stop")

    client.query("report")
    records = _records(_sent(client))

    assert "... on Report" not in _sent(client).document
    assert {
        record
        for record in _under(records, ("report", "related"))
        if record[0] == "Report"
    } == _report_fields(schema, keep=frozenset())


@pytest.mark.parametrize("prefix", [("report", "parent"), ("report", "related")])
def test_id_only_records_every_field_it_removes_but_id(
    schema: GraphQLSchema, prefix: tuple[str, ...]
) -> None:
    # `parent` is a field id_only keeps, and `related` reaches Report through
    # a member fragment it keeps. Both keep `id` and lose the rest.
    client = _client(schema, cycle_policy="id_only")

    client.query("report")
    records = _records(_sent(client))

    assert {
        record for record in _under(records, prefix) if record[0] == "Report"
    } == _report_fields(schema, keep=frozenset({"id"}))


def test_id_only_records_a_refused_id_and_the_field_it_then_removes(
    schema: GraphQLSchema,
) -> None:
    client = _client(schema, cycle_policy="id_only", exclude=("Report.id",))

    client.query("report")
    records = _records(_sent(client))

    assert ("Report", ("report", "parent", "id"), "should-include") in records
    assert ("Report", ("report", "parent"), "cycle") in records


def test_shallow_records_what_it_removes_as_cycle_not_depth(
    schema: GraphQLSchema,
) -> None:
    client = _client(schema, cycle_policy="shallow")

    client.query("report")
    below = _under(_records(_sent(client)), ("report", "parent"))

    # `unpaged` has no page-size argument, a rule checked before the budget.
    composite = {"paged", "mid", "parent", "related", "wide"}
    assert {path[0] for _, path, reason in below if reason == "cycle"} == composite
    assert ("Report", ("unpaged",), "connection-page-size") in below
    assert all(reason != "depth" for _, _, reason in below)


def test_a_capped_member_records_every_field_but_id(schema: GraphQLSchema) -> None:
    client = _client(schema)

    client.query("report")
    records = _records(_sent(client))

    capped = [record for record in records if record[0] == f"M{_CAP}"]
    assert capped == [(f"M{_CAP}", ("report", "wide", "name"), "union-member-cap")]
    assert not any(record[0] == f"M{_CAP - 1}" for record in records)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("ItemConnection.pageInfo", ("ItemConnection", ("pageInfo",))),
        ("ItemConnection.edges", ("ItemConnection", ("edges",))),
        ("ItemEdge.cursor", ("ItemEdge", ("edges", "cursor"))),
        ("PageInfo.endCursor", ("PageInfo", ("pageInfo", "endCursor"))),
    ],
)
def test_a_template_field_the_policy_refuses_is_recorded(
    schema: GraphQLSchema, pattern: str, expected: tuple[str, tuple[str, ...]]
) -> None:
    client = _client(schema, exclude=(pattern,))

    client.query("report")

    parent, tail = expected
    record = (parent, ("report", "paged", *tail), "should-include")
    assert record in _records(_sent(client))


# -- interfaces -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("root", "config", "prefix", "member", "expected"),
    [
        pytest.param(
            "named",
            {},
            ("named",),
            f"N{_CAP}",
            {("extra", "union-member-cap")},
            id="capped",
        ),
        pytest.param(
            "n0",
            {"cycle_policy": "stop"},
            ("n0", "back"),
            "N0",
            {("extra", "cycle"), ("back", "cycle")},
            id="stop",
        ),
        pytest.param(
            "n0",
            {"cycle_policy": "id_only"},
            ("n0", "back"),
            "N0",
            {("extra", "cycle"), ("back", "cycle")},
            id="id-only",
        ),
        pytest.param(
            "named",
            {"exclude": ("N0.name",)},
            ("named",),
            "N0",
            set(),
            id="should-include",
        ),
    ],
)
def test_a_member_does_not_record_a_field_its_interface_selects(
    schema: GraphQLSchema,
    root: str,
    config: dict[str, Any],
    prefix: tuple[str, ...],
    member: str,
    expected: set[tuple[str, str]],
) -> None:
    # `id` and `name` are selected on the interface itself, so the response
    # carries them for every member, whatever the member's fragment lost.
    client = _client(schema, **config)

    client.query(root)
    records = _under(_records(_sent(client)), prefix)

    assert {
        (path[0], reason) for parent, path, reason in records if parent == member
    } == expected
    assert ("Named", ("name",), "should-include") not in records


def test_a_spliced_member_scope_records_what_it_left_out(
    schema: GraphQLSchema,
) -> None:
    # The records describe one automatic scope. The explicit `name` around
    # the spliced fragment is outside it, so the fragment's record stays.
    client = _client(schema, exclude=("N0.name",))

    client.query("named", fields=Selection("name", Selection.of("N0")))
    request = _sent(client)

    assert ("N0", ("named", "name"), "should-include") in _records(request)


def test_an_interface_no_type_implements_records_nothing(
    schema: GraphQLSchema,
) -> None:
    # No member fragment exists to select `child`, and no value of the
    # interface can exist for the server to return, so nothing is left out.
    client = _client(schema)

    client.query("unimplemented")
    request = _sent(client)

    assert "child" not in request.document
    assert "id" in request.document
    assert _records(request) == []
    assert request.omissions_total == 0


# -- completeness ---------------------------------------------------------------


#: The connection fields rule 4's template selects. Any other connection field
#: is one no rule selects, so its absence is not an omission.
_TEMPLATE = frozenset({"pageInfo", "edges"})


def _fields_of(selection_set: SelectionSetNode, type_name: str) -> Iterator[FieldNode]:
    """The fields selected for ``type_name``, looking through its fragments."""
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            yield selection
        elif (
            isinstance(selection, InlineFragmentNode)
            and selection.type_condition is not None
            and selection.type_condition.name.value == type_name
        ):
            yield from _fields_of(selection.selection_set, type_name)


def _mismatches(
    schema: GraphQLSchema,
    selection_set: SelectionSetNode,
    type_: Any,
    path: tuple[str, ...],
    records: set[tuple[str, tuple[str, ...]]],
) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    """Every field left out but not recorded, or recorded but selected.

    A member's fields are the ones its fragment selects plus the ones the
    interface selects around it, since the response carries both. An
    interface with no member has no value to check.
    """
    if isinstance(type_, (GraphQLUnionType, GraphQLInterfaceType)):
        for member in schema.get_possible_types(type_):
            yield from _mismatches(schema, selection_set, member, path, records)
        return
    assert isinstance(type_, GraphQLObjectType)
    selected = {node.name.value: node for node in _fields_of(selection_set, type_.name)}
    template = is_connection_type(type_)
    for name, field_ in type_.fields.items():
        here = (*path, name)
        node = selected.get(name)
        if node is None:
            if template and name not in _TEMPLATE:
                continue  # rule 4's template selects a fixed shape
            below = any(
                len(path_) > len(here) and path_[: len(here)] == here
                for _, path_ in records
            )
            if (type_.name, here) not in records and not below:
                yield ("missing", type_.name, here)
            continue
        if (type_.name, here) in records:
            yield ("surplus", type_.name, here)
        if node.selection_set is not None:
            yield from _mismatches(
                schema,
                node.selection_set,
                get_named_type(field_.type),
                (*path, name),
                records,
            )


def _check_records(
    schema: GraphQLSchema, root_field: str, root_type: str, **config: Any
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Build ``root_field`` and walk its document against its records."""
    client = _client(schema, **config)

    client.query(root_field)
    request = _sent(client)

    # The check reads the records, so it needs all of them.
    assert request.omissions_total <= MAX_OMISSION_RECORDS
    records = {(parent, path) for parent, path, _ in _records(request)}
    operation = parse(request.document).definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    (root,) = operation.selection_set.selections
    assert isinstance(root, FieldNode)
    assert root.selection_set is not None
    return list(
        _mismatches(
            schema,
            root.selection_set,
            schema.get_type(root_type),
            (root_field,),
            records,
        )
    )


_DEPTHS = pytest.mark.parametrize("max_depth", [1, 2, 3, 4])
_CYCLE_POLICIES = pytest.mark.parametrize(
    "cycle_policy", ["stop", "id_only", "shallow"]
)


@_DEPTHS
@_CYCLE_POLICIES
@pytest.mark.parametrize(
    "exclude",
    [
        (),
        ("Report.id",),
        ("ItemEdge.cursor", "PageInfo.hasNextPage"),
        ("ItemConnection.pageInfo",),
        ("ItemConnection.edges",),
    ],
    ids=["none", "id", "template-leaves", "page-info", "edges"],
)
def test_every_field_a_document_leaves_out_is_recorded(
    schema: GraphQLSchema,
    max_depth: int,
    cycle_policy: str,
    exclude: tuple[str, ...],
) -> None:
    mismatches = _check_records(
        schema,
        "report",
        "Report",
        max_depth=max_depth,
        cycle_policy=cycle_policy,
        exclude=exclude,
    )

    assert mismatches == []


@_DEPTHS
@_CYCLE_POLICIES
@pytest.mark.parametrize(
    "exclude",
    [(), ("N0.name",), ("Named.name",)],
    ids=["none", "member-name", "interface-name"],
)
@pytest.mark.parametrize(
    ("root_field", "root_type"), [("named", "Named"), ("n0", "N0")]
)
def test_every_member_field_left_out_is_recorded_once(
    schema: GraphQLSchema,
    max_depth: int,
    cycle_policy: str,
    exclude: tuple[str, ...],
    root_field: str,
    root_type: str,
) -> None:
    # `named` reaches the capped member, and `n0` reaches N0 again through a
    # member fragment of `back`, which is where the cycle policy applies.
    mismatches = _check_records(
        schema,
        root_field,
        root_type,
        max_depth=max_depth,
        cycle_policy=cycle_policy,
        exclude=exclude,
    )

    assert mismatches == []


# -- rebasing and the cache ------------------------------------------------------


def _count_walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the root type of every walk the builder really runs."""
    walks: list[str] = []
    run = builder_module._Walk.run

    def counted(walk: Any, type_: Any) -> Any:
        walks.append(type_.name)
        return run(walk, type_)

    monkeypatch.setattr(builder_module._Walk, "run", counted)
    return walks


def test_a_cached_selection_is_rebased_at_every_position_it_is_spliced(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    walks = _count_walks(monkeypatch)
    client = _client(schema)

    client.query("report")
    alone = _records(_sent(client))
    client.query(
        "holder",
        fields=[
            Field("a", fields=AUTO),
            Field("b", fields=[Field("parent", fields=AUTO)]),
        ],
    )
    nested = _records(_sent(client))

    # One real walk for Report: both later splices came from the cache.
    assert walks == ["Report"]
    relative = [(parent, path[1:], reason) for parent, path, reason in alone]
    assert nested == [
        *(
            (parent, ("holder", "a", *path), reason)
            for parent, path, reason in relative
        ),
        *(
            (parent, ("holder", "b", "parent", *path), reason)
            for parent, path, reason in relative
        ),
    ]


def test_a_cached_truncated_selection_keeps_its_total(
    schema: GraphQLSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three splices of one cached Report selection omit more than the bound
    # holds. The retained records are the first in traversal order, and the
    # total still counts every splice.
    walks = _count_walks(monkeypatch)
    client = _client(schema, cycle_policy="id_only", max_depth=2)

    client.query("report")
    each = _sent(client).omissions_total
    client.query("holder", fields=[Field(name, fields=AUTO) for name in "abc"])
    request = _sent(client)
    snapshot = request.redacted()

    assert walks == ["Report"]
    assert 3 * each > MAX_OMISSION_RECORDS
    assert request.omissions_total == 3 * each
    assert len(snapshot.omissions) == MAX_OMISSION_RECORDS
    assert snapshot.omissions_dropped == 3 * each - MAX_OMISSION_RECORDS
    order = [record.path[1] for record in snapshot.omissions]
    assert order == sorted(order)
    assert order.count("a") == each
    assert f"of {3 * each} total" in client.recorder.dump()


@pytest.mark.parametrize(
    ("root", "fields"),
    [
        pytest.param("many", AUTO, id="root"),
        pytest.param("holder", [Field("many", fields=AUTO)], id="spliced"),
    ],
)
def test_a_selection_truncated_on_its_own_keeps_its_total(
    schema: GraphQLSchema, root: str, fields: Any
) -> None:
    # The walk already cut the list, so a stage that counted the records it
    # was handed would report nothing dropped.
    client = _client(schema)

    client.query(root, fields=fields)
    request = _sent(client)

    assert request.omissions_total == _MANY_COUNT
    assert request.redacted().omissions_dropped == _MANY_COUNT - MAX_OMISSION_RECORDS
