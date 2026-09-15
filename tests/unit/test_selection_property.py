"""The property test for the selection builder (SPEC 11.3, PLAN M3).

The requirement, stated once: for any type in the hostile schema and any
policy, auto-selection terminates and the document it produces validates
against that schema. This file is written against that sentence, not against
the builder, which is why it is written before the builder exists. A property
test written afterwards tends to encode what the engine happens to do.

Two outcomes count as success. A document that validates is the ordinary one.
A refusal is the other: ``SelectionTooLargeError`` when the policy's
``max_fields`` is smaller than the selection needs, and ``SelectionError``
when the type has nothing left to select once the policy has been applied.
Both are terminating, documented answers. An invalid document, a crash, or a
walk that does not finish inside the deadline is a failure.

A type in the schema is not always reachable from ``Query``, so validation
runs against a probe schema whose root exposes exactly one field of the type
under test. The probe reuses the real type objects, so what is validated is
the real schema's shape.

Assembling an operation is M4's work. The assembly here is the smallest thing
that can carry a selection set and its generated variables into ``validate``,
and it is deliberately not a preview of that module.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from graphql import (
    DocumentNode,
    FieldNode,
    GraphQLCompositeType,
    GraphQLField,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    VariableDefinitionNode,
    VariableNode,
    is_composite_type,
    parse_type,
    print_ast,
    validate,
)
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from pytest_graphql._core.errors import SelectionError, SelectionTooLargeError
from pytest_graphql._core.selection.builder import BuiltSelection, SelectionBuilder
from pytest_graphql._core.selection.policy import SelectionPolicy
from tests.schema.resolvers import build_schema

SCHEMA = build_schema()


def _composite_type_names(schema: GraphQLSchema) -> tuple[str, ...]:
    """Every object, interface and union type the schema declares."""
    names = [
        name
        for name, type_ in schema.type_map.items()
        if not name.startswith("__") and is_composite_type(type_)
    ]
    return tuple(sorted(names))


def _composite(name: str) -> GraphQLCompositeType:
    type_ = SCHEMA.type_map[name]
    assert is_composite_type(type_)
    return cast(GraphQLCompositeType, type_)


COMPOSITE_TYPE_NAMES = _composite_type_names(SCHEMA)

# Patterns drawn from the hostile schema in all three documented shapes:
# Type.field, *.field and Type.*.
EXCLUDE_PATTERNS = (
    "User.manager",
    "User.postsConnection",
    "Post.author",
    "PostConnection.edges",
    "*.id",
    "*.name",
    "Settings.*",
    "WideType.*",
)


@st.composite
def policies(draw: st.DrawFn) -> SelectionPolicy:
    """Any policy a user could configure, inside plausible bounds.

    The bounds are here so examples mostly reach a built document rather than
    the size guard. They are not the calibrated defaults, which the
    calibration gate decides.
    """
    return SelectionPolicy(
        max_depth=draw(st.integers(min_value=1, max_value=5)),
        cycle_policy=draw(st.sampled_from(("stop", "shallow", "id_only"))),
        per_type_depth_cap=draw(
            st.dictionaries(
                st.sampled_from(COMPOSITE_TYPE_NAMES),
                st.integers(min_value=0, max_value=3),
                max_size=3,
            )
        ),
        include_deprecated=draw(st.booleans()),
        # The largest selection this schema can produce is well under 800
        # fields, so the first branch is "certainly enough" and the second
        # exercises the size guard. Drawn as two branches because a single
        # range is biased towards small values, and an example that refuses
        # says less about the document than one that builds it.
        max_fields=draw(
            st.one_of(
                st.integers(min_value=800, max_value=3000),
                st.integers(min_value=1, max_value=800),
            )
        ),
        exclude=draw(
            st.lists(st.sampled_from(EXCLUDE_PATTERNS), max_size=4, unique=True)
        ),
        relay_aware=draw(st.booleans()),
        connection_page_size=draw(st.integers(min_value=1, max_value=100)),
        max_union_members=draw(st.integers(min_value=0, max_value=5)),
        max_connection_depth=draw(st.integers(min_value=0, max_value=3)),
    )


def _probe_schema(type_: GraphQLCompositeType) -> GraphQLSchema:
    """A schema whose only root field returns ``type_``.

    Introspection types are dropped from the carried set because a schema
    rejects a type whose name starts with two underscores, and graphql-core
    adds its own back.
    """
    carried: list[GraphQLNamedType] = [
        named for name, named in SCHEMA.type_map.items() if not name.startswith("__")
    ]
    root = GraphQLObjectType("ProbeRoot", {"probe": GraphQLField(type_)})
    return GraphQLSchema(query=root, types=carried)


def _probe_document(built: BuiltSelection) -> DocumentNode:
    definitions = tuple(
        VariableDefinitionNode(
            variable=VariableNode(name=NameNode(value=generated.name)),
            type=parse_type(str(generated.type_)),
            directives=(),
        )
        for generated in built.variables
    )
    operation = OperationDefinitionNode(
        operation=OperationType.QUERY,
        name=NameNode(value="Probe"),
        variable_definitions=definitions,
        directives=(),
        selection_set=SelectionSetNode(
            selections=(
                FieldNode(
                    name=NameNode(value="probe"),
                    arguments=(),
                    directives=(),
                    selection_set=built.selection_set,
                ),
            )
        ),
    )
    return DocumentNode(definitions=(operation,))


def _build(type_name: str, policy: SelectionPolicy) -> BuiltSelection | None:
    """Build the selection, or return None when the engine refuses.

    A refusal is an outcome the property allows. Letting any other exception
    escape is the point: an unexpected failure fails the example. Each outcome
    is reported as an event so ``--hypothesis-show-statistics`` says how often
    a document was really built, rather than leaving that to be assumed.
    """
    try:
        built = SelectionBuilder(SCHEMA).build(_composite(type_name), policy)
    except SelectionTooLargeError:
        event("refused: too large")
        return None
    except SelectionError:
        event("refused: nothing to select")
        return None
    event("built a document")
    return built


@given(type_name=st.sampled_from(COMPOSITE_TYPE_NAMES), policy=policies())
@settings(
    deadline=timedelta(seconds=10),
    max_examples=500,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_auto_selection_terminates_and_validates(
    type_name: str, policy: SelectionPolicy
) -> None:
    built = _build(type_name, policy)
    if built is None:
        return
    document = _probe_document(built)
    errors = validate(_probe_schema(_composite(type_name)), document)
    assert not errors, (
        f"{type_name} under {policy!r} produced an invalid document:\n"
        f"{print_ast(document)}\n" + "\n".join(error.message for error in errors)
    )


@given(type_name=st.sampled_from(COMPOSITE_TYPE_NAMES), policy=policies())
@settings(
    deadline=timedelta(seconds=10),
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_auto_selection_is_deterministic(
    type_name: str, policy: SelectionPolicy
) -> None:
    """The same type and policy produce the same document every time.

    Two separate builders are used, so this asserts a property of the walk
    and not of one builder's cache.
    """
    type_ = _composite(type_name)

    def once() -> tuple[str, tuple[tuple[str, str, Any], ...]]:
        try:
            built = SelectionBuilder(SCHEMA).build(type_, policy)
        except (SelectionTooLargeError, SelectionError) as error:
            # A refusal is an outcome too, and it has to be the same one.
            return (type(error).__name__, ())
        return (
            print_ast(built.selection_set),
            tuple(
                (generated.name, str(generated.type_), generated.value)
                for generated in built.variables
            ),
        )

    assert once() == once()
