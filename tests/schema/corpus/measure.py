"""Calibration-gate measurement harness.

Measures auto-selection against the checked-in corpus of project-authored
synthetic schema fixtures in ``sdl/``. Two kinds of measurement, kept separate
because they answer different questions:

- Schema-static measurements (``union_and_interface_widths``,
  ``detect_connections``, ``probe_type_depth``) look at the schema graph
  itself, independent of any policy, and answer "what does a schema shaped
  like this look like".
- Engine measurements (``measure_query_fields``) run the actual
  ``SelectionBuilder`` with the library's default ``SelectionPolicy`` and
  answer "what does auto-selection actually produce on this schema, under
  the current defaults".

Run as a script for the human-readable report the calibration gate records
in the review handoff log:

    uv run python tests/schema/corpus/measure.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from graphql import (
    FieldNode,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    SelectionSetNode,
    Undefined,
    build_schema,
    get_named_type,
    is_composite_type,
)

from pytest_graphql._core.errors import SelectionError, SelectionTooLargeError
from pytest_graphql._core.selection.builder import BuiltSelection, SelectionBuilder
from pytest_graphql._core.selection.policy import (
    SelectionPolicy,
    is_connection_type,
    page_size_argument,
)

CORPUS_DIR = Path(__file__).parent
SDL_DIR = CORPUS_DIR / "sdl"


def load_corpus() -> dict[str, GraphQLSchema]:
    """Every ``*.graphql`` SDL fixture in ``sdl/``, keyed by file stem.

    Each fixture is plain GraphQL SDL, built directly with graphql-core's
    ``build_schema``. There is no introspection round-trip: the domain model
    and prose are project-authored, and the pagination vocabulary follows the
    published GraphQL Cursor Connections Specification rather than any
    third-party schema (see the fixture files for the exact source and
    license).
    """
    schemas = {}
    for path in sorted(SDL_DIR.glob("*.graphql")):
        schemas[path.stem] = build_schema(path.read_text())
    return schemas


def union_and_interface_widths(schema: GraphQLSchema) -> dict[str, int]:
    """Member count of every union and interface in the schema.

    Answers the ``max_union_members`` question: how wide are the corpus's
    unions and interfaces, and how often would the default cap of 10 actually
    cut something.
    """
    widths: dict[str, int] = {}
    for t in schema.type_map.values():
        if t.name.startswith("__"):
            continue
        if isinstance(t, GraphQLUnionType):
            widths[t.name] = len(t.types)
        elif isinstance(t, GraphQLInterfaceType):
            widths[t.name] = len(schema.get_possible_types(t))
    return widths


@dataclass
class ConnectionSurvey:
    detected: list[str] = field(default_factory=list)
    named_but_wrong_shape: list[str] = field(default_factory=list)
    shaped_but_unnamed: list[str] = field(default_factory=list)


def detect_connections(schema: GraphQLSchema) -> ConnectionSurvey:
    """Where the name-based relay heuristic (``is_connection_type``) agrees
    or disagrees with the raw shape (an object with both ``edges`` and
    ``pageInfo``, regardless of name).

    Answers spec open question 6.
    """
    survey = ConnectionSurvey()
    for t in schema.type_map.values():
        if not isinstance(t, GraphQLObjectType) or t.name.startswith("__"):
            continue
        shaped = "edges" in t.fields and "pageInfo" in t.fields
        named = t.name.endswith("Connection")
        if is_connection_type(t):
            survey.detected.append(t.name)
        elif named and not shaped:
            survey.named_but_wrong_shape.append(t.name)
        elif shaped and not named:
            survey.shaped_but_unnamed.append(t.name)
    return survey


def deprecated_field_census(schema: GraphQLSchema) -> tuple[int, int]:
    """``(deprecated field count, total field count)`` across every object type.

    Answers the C1 ``include_deprecated=False`` question directly: does a
    schema shaped like this actually carry deprecated fields at all. If none
    ever did, the default would have nothing to exclude and the corpus would
    say so.
    """
    deprecated = 0
    total = 0
    for t in schema.type_map.values():
        if t.name.startswith("__") or not isinstance(t, GraphQLObjectType):
            continue
        for f in t.fields.values():
            total += 1
            if f.deprecation_reason is not None:
                deprecated += 1
    return deprecated, total


@dataclass(frozen=True)
class NestedConnection:
    """A detected connection whose ``node`` type has a field returning
    another detected connection: the shape ``max_connection_depth=1`` exists
    to cap."""

    outer: str
    field_name: str
    inner: str


def nested_connections(
    schema: GraphQLSchema, survey: ConnectionSurvey
) -> list[NestedConnection]:
    """Every real occurrence of a connection nested inside another one.

    Answers whether ``max_connection_depth=1`` is exercised at all by the
    corpus, or whether nested connections are a case this corpus never
    actually presents to the engine.
    """
    found: list[NestedConnection] = []
    for outer_name in survey.detected:
        outer = schema.type_map[outer_name]
        edges = getattr(outer, "fields", {}).get("edges")
        if edges is None:
            continue
        edge_type = get_named_type(edges.type)
        node = getattr(edge_type, "fields", {}).get("node")
        if node is None:
            continue
        node_type = get_named_type(node.type)
        for field_name, field_def in getattr(node_type, "fields", {}).items():
            inner_named = get_named_type(field_def.type)
            if (
                isinstance(inner_named, GraphQLObjectType)
                and inner_named.name in survey.detected
            ):
                found.append(NestedConnection(outer_name, field_name, inner_named.name))
    return found


def _child_selection_set(
    selection_set: SelectionSetNode, field_name: str
) -> SelectionSetNode | None:
    """The selection set of one direct child field, or ``None`` when the
    field is absent or has no sub-selection."""
    for sel in selection_set.selections:
        if isinstance(sel, FieldNode) and sel.name.value == field_name:
            return sel.selection_set
    return None


def _has_field_at_edges_node(
    outer_selection_set: SelectionSetNode, field_name: str
) -> bool:
    """Whether ``field_name`` is selected at the exact ``edges.node`` path.

    Checking the printed document for the field name as a substring cannot
    tell a real hit from a coincidental one, and cannot say where in the
    document it occurred. This walks the two known steps of the connection
    template (rule 4 in builder.py) instead.
    """
    edges = _child_selection_set(outer_selection_set, "edges")
    if edges is None:
        return False
    node = _child_selection_set(edges, "node")
    if node is None:
        return False
    return any(
        isinstance(sel, FieldNode) and sel.name.value == field_name
        for sel in node.selections
    )


def connection_depth_cap_confirmed(
    schema: GraphQLSchema, builder: SelectionBuilder, examples: list[NestedConnection]
) -> list[NestedConnection]:
    """The subset of ``examples`` where raising ``max_connection_depth`` by
    one is what makes the inner connection field appear: absent under the
    default cap and present once the cap is raised, at the exact
    ``outer.edges.node.<inner>`` path.

    A field can be absent from the default build for a reason unrelated to
    the cap, for example a connection whose page-size argument is not named
    ``first`` (C1): such a field is skipped at every depth, not only at the
    default one. Checking "absent under the default" alone cannot tell that
    case apart from a cap-caused omission; only a build under a raised cap
    can, because a field excluded for another reason stays absent at both.
    """
    confirmed: list[NestedConnection] = []
    default_policy = SelectionPolicy()
    raised_policy = SelectionPolicy(
        max_connection_depth=default_policy.max_connection_depth + 1
    )
    built_by_outer: dict[str, tuple[SelectionSetNode, SelectionSetNode]] = {}
    for example in examples:
        if example.outer not in built_by_outer:
            outer_type = schema.type_map[example.outer]
            if not is_composite_type(outer_type):
                continue
            at_default = builder.build(outer_type, default_policy)  # type: ignore[arg-type]
            at_raised = builder.build(outer_type, raised_policy)  # type: ignore[arg-type]
            built_by_outer[example.outer] = (
                at_default.selection_set,
                at_raised.selection_set,
            )
        default_set, raised_set = built_by_outer[example.outer]
        absent_at_default = not _has_field_at_edges_node(
            default_set, example.field_name
        )
        present_at_raised = _has_field_at_edges_node(raised_set, example.field_name)
        if absent_at_default and present_at_raised:
            confirmed.append(example)
    return confirmed


def page_size_argument_survey(schema: GraphQLSchema) -> tuple[int, int]:
    """``(recognized 'first' arguments, of those with a GraphQL-declared
    default)`` across every object type.

    Answers the ``connection_page_size`` question directly. Introspection
    can expose a *declared* default for the recognized page-size argument
    (``GraphQLArgument.default_value``), so this checks for one rather than
    assuming a corpus schema never states one. Only an undeclared,
    implementation-side runtime default stays outside what any introspection
    document could ever reveal.
    """
    recognized = 0
    with_default = 0
    for t in schema.type_map.values():
        if t.name.startswith("__") or not isinstance(t, GraphQLObjectType):
            continue
        for f in t.fields.values():
            argument = page_size_argument(f)
            if argument is None:
                continue
            recognized += 1
            if argument.default_value is not Undefined:
                with_default += 1
    return recognized, with_default


def probe_type_depth(
    schema: GraphQLSchema,
    root: GraphQLObjectType | GraphQLInterfaceType | GraphQLUnionType,
    cap: int = 20,
) -> int:
    """The longest acyclic composite path below ``root``, or ``cap`` for a
    branch that has not terminated by the time it reaches that many levels.

    Cycle termination uses the same ancestor rule as the engine
    (``SelectionBuilder``): a composite type already on the current branch is
    a cycle and the walk stops there, whether that type is reached directly
    or through a union or interface member (diamond reuse through two
    different branches is not a cycle). ``cap`` is only a safety ceiling for
    a branch that has not hit a repeat by then; it is not evidence that the
    schema is actually infinite, so a self-referential type can legitimately
    probe to a small finite depth rather than "at least cap". Answers whether
    the default ``max_depth=3`` is shallow or deep relative to what the
    corpus schemas actually nest.

    A union or interface ``root`` costs no depth of its own, the same way
    choosing a member inside an inline fragment costs no depth mid-walk
    (B12): the result is the deepest of its possible types, each probed from
    depth 0. Mid-walk, a field returning a union or interface costs the same
    one level as a field returning an object; entering the abstract type
    itself, and then one of its possible types, adds no further cost, so a
    repeat of the abstract type through any of its members is caught by
    putting the abstract type's own name in ``ancestors`` before its members
    are checked, exactly where ``SelectionBuilder`` puts it.
    """

    def possible_types(
        t: GraphQLInterfaceType | GraphQLUnionType,
    ) -> tuple[GraphQLObjectType, ...]:
        if isinstance(t, GraphQLUnionType):
            members: tuple[GraphQLObjectType, ...] = t.types
            return members
        return tuple(schema.get_possible_types(t))

    def walk(
        t: GraphQLObjectType | GraphQLInterfaceType | GraphQLUnionType,
        ancestors: frozenset[str],
        depth: int,
    ) -> int:
        if depth >= cap:
            return depth
        best = depth
        if isinstance(t, (GraphQLUnionType, GraphQLInterfaceType)):
            for member in possible_types(t):
                if member.name in ancestors:
                    continue
                best = max(best, walk(member, ancestors | {member.name}, depth))
                if best >= cap:
                    return best
            return best
        for f in t.fields.values():
            named = get_named_type(f.type)
            if not is_composite_type(named) or named.name in ancestors:
                continue
            best = max(best, walk(named, ancestors | {named.name}, depth + 1))
            if best >= cap:
                return best
        return best

    return walk(root, frozenset({root.name}), 0)


def deepest_query_field_depth(schema: GraphQLSchema) -> int | None:
    """The maximum ``probe_type_depth`` reachable from any top-level Query
    field returning a composite type (object, interface, or union), or
    ``None`` when no Query field does.

    Answers whether the default ``max_depth=3`` is shallow or deep relative
    to what the corpus schemas nest, at the query-field level rather than one
    type probed in isolation. A union-returning field (for example
    ``search: [SearchResult!]!``) is included: excluding it would silently
    drop the deepest member reachable through that field from the measurement.
    """
    query_type = schema.query_type
    if query_type is None:
        return None
    depths: list[int] = []
    composite_kinds = (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
    for field_def in query_type.fields.values():
        named = get_named_type(field_def.type)
        if isinstance(named, composite_kinds):
            depths.append(probe_type_depth(schema, named))
    return max(depths) if depths else None


@dataclass
class FieldMeasurement:
    root_type: str
    query_field: str
    status: str  # "ok", "too_large", "no_selectable_fields"
    field_count: int | None = None
    ast_depth: int | None = None
    fragment_count: int | None = None


def _ast_depth(selection_set: SelectionSetNode) -> int:
    best = 1
    for sel in selection_set.selections:
        inner = getattr(sel, "selection_set", None)
        if inner is not None:
            best = max(best, 1 + _ast_depth(inner))
    return best


def _fragment_count(selection_set: SelectionSetNode) -> int:
    total = 0
    for sel in selection_set.selections:
        if isinstance(sel, InlineFragmentNode):
            total += 1
        inner = getattr(sel, "selection_set", None)
        if inner is not None:
            total += _fragment_count(inner)
    return total


def measure_query_fields(schema: GraphQLSchema) -> list[FieldMeasurement]:
    """Build the default-policy auto-selection for every top-level Query
    field that returns a composite type, the realistic "AUTO on a query
    result" scenario. Answers the ``max_fields`` question directly: does the
    default 2000 actually get exercised on the corpus schemas.
    """
    query_type = schema.query_type
    if query_type is None:
        return []
    builder = SelectionBuilder(schema)
    policy = SelectionPolicy()
    results: list[FieldMeasurement] = []
    for field_name, field_def in query_type.fields.items():
        named = get_named_type(field_def.type)
        if not is_composite_type(named):
            continue
        try:
            built: BuiltSelection = builder.build(named, policy)
        except SelectionTooLargeError as error:
            results.append(
                FieldMeasurement(
                    root_type=named.name,
                    query_field=field_name,
                    status="too_large",
                    field_count=error.field_count,
                )
            )
            continue
        except SelectionError:
            results.append(
                FieldMeasurement(
                    root_type=named.name,
                    query_field=field_name,
                    status="no_selectable_fields",
                )
            )
            continue
        results.append(
            FieldMeasurement(
                root_type=named.name,
                query_field=field_name,
                status="ok",
                field_count=built.field_count,
                ast_depth=_ast_depth(built.selection_set),
                fragment_count=_fragment_count(built.selection_set),
            )
        )
    return results


def largest_default_policy_build(
    corpus: dict[str, GraphQLSchema],
) -> tuple[str, FieldMeasurement] | None:
    """The single largest default-policy build across every corpus schema,
    by ``field_count``, among builds that complete (``status == "ok"``).

    Answers which corpus schema and query field the ``max_fields``/
    ``max_depth`` headroom claim in the design authority's Calibration
    section actually cites: the largest build in the whole corpus, not the
    largest within one schema examined in isolation. Returns ``None`` when no
    schema in ``corpus`` has an ``"ok"`` build.
    """
    best: tuple[str, FieldMeasurement] | None = None
    best_count = -1
    for name, schema in corpus.items():
        for measurement in measure_query_fields(schema):
            if measurement.status != "ok" or measurement.field_count is None:
                continue
            if measurement.field_count > best_count:
                best_count = measurement.field_count
                best = (name, measurement)
    return best


def build_report() -> str:
    lines: list[str] = []
    schemas = load_corpus()
    for name, schema in schemas.items():
        lines.append(f"## {name}")

        widths = union_and_interface_widths(schema)
        over_cap = {k: v for k, v in widths.items() if v > 10}
        widest = max(widths.values()) if widths else 0
        lines.append(f"- union/interface types: {len(widths)}, widest: {widest}")
        if over_cap:
            lines.append(f"  - over max_union_members=10: {over_cap}")

        survey = detect_connections(schema)
        lines.append(
            f"- connections detected: {len(survey.detected)}"
            f", named-but-wrong-shape: {survey.named_but_wrong_shape}"
            f", shaped-but-unnamed: {survey.shaped_but_unnamed}"
        )

        deprecated, total_fields = deprecated_field_census(schema)
        lines.append(f"- deprecated fields: {deprecated} of {total_fields}")

        nested = nested_connections(schema, survey)
        if nested:
            examples = [f"{n.outer}.{n.field_name} -> {n.inner}" for n in nested[:5]]
            lines.append(
                f"- nested connections in the schema graph: {len(nested)} {examples}"
            )
            confirmed = connection_depth_cap_confirmed(
                schema, SelectionBuilder(schema), nested
            )
            lines.append(
                f"  - max_connection_depth=1 causally confirmed (absent at 1, "
                f"present at 2) for {len(confirmed)} of {len(nested)}; "
                f"{len(nested) - len(confirmed)} stay absent at both depths"
            )
        else:
            lines.append("- nested connections in the schema graph: 0")

        recognized, with_default = page_size_argument_survey(schema)
        lines.append(
            f"- recognized page-size ('first') arguments: {recognized}, with a "
            f"GraphQL-declared default: {with_default}"
        )

        deepest = deepest_query_field_depth(schema)
        if deepest is not None:
            lines.append(
                f"- deepest schema nesting from a Query field (capped at 20): {deepest}"
            )

        measurements = measure_query_fields(schema)
        ok = [m for m in measurements if m.status == "ok"]
        too_large = [m for m in measurements if m.status == "too_large"]
        empty = [m for m in measurements if m.status == "no_selectable_fields"]
        if ok:
            max_fields = max(m.field_count for m in ok)  # type: ignore[type-var]
            max_depth = max(m.ast_depth for m in ok)  # type: ignore[type-var]
            max_fragments = max(m.fragment_count for m in ok)  # type: ignore[type-var]
            lines.append(
                f"- default-policy builds: {len(ok)} ok, max field_count={max_fields}, "
                f"max ast_depth={max_depth}, max fragment_count={max_fragments}"
            )
        if too_large:
            fields = [m.query_field for m in too_large]
            lines.append(f"  - too_large under max_fields=2000: {fields}")
        if empty:
            lines.append(f"  - no_selectable_fields: {[m.query_field for m in empty]}")

        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_report())
