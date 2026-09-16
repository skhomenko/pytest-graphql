"""Assembling one operation document (SPEC 8.2; D3, B10, B14, B15, B23).

``assemble_operation`` is the one entry point. It turns an operation kind and
name, the raw keyword mapping a caller passed to ``query()``/``mutation()``/
``execute()`` (SPEC 3.2: options and candidate GraphQL variables arrive in
that one mapping, unseparated), and a selection input into one validated
``DocumentNode`` plus the coerced values to send beside it.

The steps run in this order, and the order is load-bearing:

1. Resolve the operation name against the schema (D3, B10), which is the
   only way to get the ``GraphQLField`` every later step needs.
2. B23: refuse a required argument a plain keyword can never reach, because
   its name collides with a reserved per-call option.
3. Split the raw keywords into candidate GraphQL variables, merge the
   ``variables={...}`` escape hatch in, explicit wins (SPEC 3.2).
4. D3/B10 resolve those candidates against the operation's own arguments,
   applying the B15 auto-wrap rule first.
5. Refuse a required argument nothing supplied (SPEC 8.2 "missing
   argument"), which ``resolve_root_arguments`` cannot see on its own: it
   only knows what a candidate resolved to, not what was never offered.
6. Build the child selection (``normalize.py``, M3) before declaring the
   root argument variables, so a root argument name that happens to collide
   with a generated selection variable is the one renamed, never the other
   way, using the same counter-suffix ``VariableAllocator`` already applies
   to a colliding field-argument name.
7. Assemble the document and run graphql-core's own ``validate`` (SPEC 8.2
   "Document fails schema validation").
8. B14: coerce every variable's value against its declared type, closing the
   gap ``validate`` leaves, since it checks document shape only.

Every step above raises before anything is returned, which is the guarantee
this module exists to give: no invalid document leaves the process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from graphql import (
    ArgumentNode,
    DocumentNode,
    FieldNode,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    ListTypeNode,
    NamedTypeNode,
    NameNode,
    NonNullTypeNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    TypeNode,
    VariableDefinitionNode,
    VariableNode,
    get_named_type,
    is_leaf_type,
)

from pytest_graphql._core.errors import (
    ArgumentError,
    OperationNotFoundError,
    SelectionError,
)
from pytest_graphql._core.naming import NameMap, field_signature
from pytest_graphql._core.schema.info import OperationKind, operation_names, root_type
from pytest_graphql._core.selection.builder import GeneratedVariable, SelectionBuilder
from pytest_graphql._core.selection.model import (
    AUTO,
    SelectionInput,
    check_graphql_name,
)
from pytest_graphql._core.selection.normalize import VariableAllocator, normalize
from pytest_graphql._core.selection.policy import (
    SelectionPolicy,
    missing_required_arguments,
)
from pytest_graphql._core.validation import (
    RESERVED_OPTIONS,
    check_no_reserved_collision,
    coerce_variables,
    validate_document,
)

_OPERATION_TYPE: Mapping[OperationKind, OperationType] = {
    "query": OperationType.QUERY,
    "mutation": OperationType.MUTATION,
    "subscription": OperationType.SUBSCRIPTION,
}


@dataclass(frozen=True)
class AssembledOperation:
    """One validated document, ready for a transport to send (M5)."""

    document: DocumentNode
    variables: Mapping[str, Any]
    #: The exact schema field name written at the document's root, for a
    #: caller unwrapping the response by key.
    field_name: str


def resolve_operation(
    schema: GraphQLSchema, kind: OperationKind, name: str
) -> tuple[str, GraphQLField]:
    """The exact schema field name and definition ``name`` resolves to (D3, B10)."""
    available = operation_names(schema, kind)
    resolved = NameMap.build(available).get(name)
    if resolved is None:
        raise OperationNotFoundError(
            kind=kind, name=name, available=available, total_count=len(available)
        )
    type_ = root_type(schema, kind)
    assert type_ is not None  # a resolved name implies this schema has a root type
    return resolved, type_.fields[resolved]


def _unwrap_non_null(type_: GraphQLInputType) -> GraphQLInputType:
    return type_.of_type if isinstance(type_, GraphQLNonNull) else type_


def _sole_input_object_argument(
    field_def: GraphQLField,
) -> tuple[str, GraphQLInputObjectType] | None:
    """B15 rule 1: the operation's one input-object-typed argument, if exactly one.

    "Unwrapped" strips only non-null, the same reading ``page_size_argument``
    (``selection/policy.py``) already gives that word elsewhere in this
    package. A *list* of input objects cannot be flattened into kwargs, so
    it does not make an operation wrap-eligible.
    """
    candidates = [
        (arg_name, unwrapped)
        for arg_name, argument in field_def.args.items()
        if isinstance(
            unwrapped := _unwrap_non_null(argument.type), GraphQLInputObjectType
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_candidates(
    candidates: Mapping[str, Any],
    name_map: NameMap,
    *,
    kind: str,
    operation_name: str,
    signature: str,
    unknown_candidates: list[str],
    input_type_name: str | None,
    input_fields: list[str] | None,
) -> dict[str, Any]:
    """Resolve every ``candidates`` key against ``name_map``, once each.

    Two different supplied keys resolving to the same schema name (the exact
    and snake spelling of one argument, most likely) is refused rather than
    letting the second silently overwrite the first: B10's lookup can tell
    the two keys apart, but a plain ``dict`` assignment cannot, and a value
    the caller wrote and then loses without any error is worse than a
    document that never got wrapped.
    """
    resolved_args: dict[str, Any] = {}
    resolved_from: dict[str, str] = {}
    for key, value in candidates.items():
        resolved = name_map.get(key)
        if resolved is None:
            raise ArgumentError(
                kind=kind,
                operation_name=operation_name,
                bad_name=key,
                signature=signature,
                candidates=unknown_candidates,
                input_type_name=input_type_name,
                input_fields=input_fields,
            )
        if resolved in resolved_args:
            raise ArgumentError.duplicate_argument(
                kind=kind,
                operation_name=operation_name,
                resolved_name=resolved,
                first_key=resolved_from[resolved],
                second_key=key,
                signature=signature,
            )
        resolved_args[resolved] = value
        resolved_from[resolved] = key
    return resolved_args


def resolve_root_arguments(
    *,
    kind: str,
    operation_name: str,
    field_def: GraphQLField,
    candidates: Mapping[str, Any],
) -> dict[str, Any]:
    """D3/B10 resolution of the operation's own arguments, B15 auto-wrap first.

    Returns a mapping from the operation's own exact schema argument names to
    the Python value to hoist for each. Raises ``ArgumentError`` for any
    ``candidates`` key that resolves against neither the operation's own
    arguments nor, when auto-wrap applies, the wrap target's fields.
    """
    if not candidates:
        return {}
    arg_names = NameMap.build(list(field_def.args))
    signature = field_signature(operation_name, field_def)
    wrap_target = _sole_input_object_argument(field_def)
    # B15 rule 2: wrapping is only a candidate when no supplied kwarg already
    # names one of the operation's own arguments.
    direct_hit = any(arg_names.get(key) is not None for key in candidates)

    if wrap_target is not None and not direct_hit:
        wrap_arg_name, input_type = wrap_target
        field_names = NameMap.build(list(input_type.fields))
        # B15 rule 3: at least one supplied kwarg must land on the input.
        if any(field_names.get(key) is not None for key in candidates):
            payload = _resolve_candidates(
                candidates,
                field_names,
                kind=kind,
                operation_name=operation_name,
                signature=signature,
                unknown_candidates=list(field_def.args),
                input_type_name=input_type.name,
                input_fields=list(input_type.fields),
            )
            return {wrap_arg_name: payload}

    input_type_name = wrap_target[1].name if wrap_target is not None else None
    input_fields = list(wrap_target[1].fields) if wrap_target is not None else None
    return _resolve_candidates(
        candidates,
        arg_names,
        kind=kind,
        operation_name=operation_name,
        signature=signature,
        unknown_candidates=list(field_def.args),
        input_type_name=input_type_name,
        input_fields=input_fields,
    )


def _check_required_arguments(
    *,
    kind: str,
    operation_name: str,
    field_def: GraphQLField,
    resolved: Mapping[str, Any],
) -> None:
    """SPEC 8.2 "missing argument": a required argument nothing supplied.

    ``resolve_root_arguments`` only ever sees what a candidate resolved to,
    never what was never offered at all, so this runs as a separate pass
    over the operation's own declared arguments.
    """
    missing = missing_required_arguments(field_def, supplied=resolved.keys())
    if missing:
        raise ArgumentError.missing_argument(
            kind=kind,
            operation_name=operation_name,
            arg_name=missing[0],
            signature=field_signature(operation_name, field_def),
        )


def type_node(type_: GraphQLInputType) -> TypeNode:
    """A ``GraphQLInputType`` as the AST node ``print_ast`` renders it.

    graphql-core builds a type node by parsing written text; there is no
    built-in reverse direction, because a document is normally hand-written,
    not generated. Every variable this module declares is generated, so the
    reverse conversion is needed here and nowhere else in the package.
    """
    if isinstance(type_, GraphQLNonNull):
        inner = type_node(type_.of_type)
        assert isinstance(inner, (NamedTypeNode, ListTypeNode))
        return NonNullTypeNode(type=inner)
    if isinstance(type_, GraphQLList):
        return ListTypeNode(type=type_node(type_.of_type))
    return NamedTypeNode(name=NameNode(value=get_named_type(type_).name))


def _variable_definitions(
    variables: tuple[GeneratedVariable, ...],
) -> tuple[VariableDefinitionNode, ...]:
    return tuple(
        VariableDefinitionNode(
            variable=VariableNode(name=NameNode(value=variable.name)),
            type=type_node(variable.type_),
            default_value=None,
            directives=(),
        )
        for variable in variables
    )


def assemble_operation(
    *,
    schema: GraphQLSchema,
    kind: OperationKind,
    name: str,
    kwargs: Mapping[str, Any],
    fields: SelectionInput = AUTO,
    policy: SelectionPolicy,
    builder: SelectionBuilder,
    operation_name: str | None = None,
    validate: bool = True,
) -> AssembledOperation:
    """Build, and by default validate, one operation document.

    ``kwargs`` is the raw keyword mapping as ``query()``/``mutation()``/
    ``execute()`` received it (SPEC 3.2): every reserved option name (B23)
    and every candidate GraphQL variable, together, because that is how they
    genuinely arrive. This function is where the two are told apart.
    """
    resolved_field_name, field_def = resolve_operation(schema, kind, name)
    raw_variables = kwargs.get("variables")
    if raw_variables is not None and not isinstance(raw_variables, Mapping):
        raise ArgumentError.invalid_value(
            kind=kind,
            operation_name=resolved_field_name,
            arg_name="variables",
            detail=(
                "the variables= escape hatch must be a mapping, got "
                f"{type(raw_variables).__name__}."
            ),
        )
    explicit_variables: Mapping[str, Any] = raw_variables or {}
    check_no_reserved_collision(
        kind=kind,
        operation_name=resolved_field_name,
        field_def=field_def,
        kwargs=kwargs,
        explicit_variables=explicit_variables,
    )
    candidate_variables: dict[str, Any] = {
        key: value for key, value in kwargs.items() if key not in RESERVED_OPTIONS
    }
    candidate_variables.update(explicit_variables)

    resolved_args = resolve_root_arguments(
        kind=kind,
        operation_name=resolved_field_name,
        field_def=field_def,
        candidates=candidate_variables,
    )
    _check_required_arguments(
        kind=kind,
        operation_name=resolved_field_name,
        field_def=field_def,
        resolved=resolved_args,
    )

    return_type = get_named_type(field_def.type)
    child_selection_set: SelectionSetNode | None
    child_variables: tuple[GeneratedVariable, ...]
    if is_leaf_type(return_type):
        if fields is not AUTO:
            raise SelectionError.on_leaf_type(
                field_name=resolved_field_name, type_name=return_type.name
            )
        child_selection_set = None
        child_variables = ()
    else:
        # A field's return type is never a GraphQLInputObjectType (GraphQL
        # disallows an input type as an output type), so ruling out a leaf
        # type above leaves exactly the three composite kinds.
        assert isinstance(
            return_type, (GraphQLObjectType, GraphQLInterfaceType, GraphQLUnionType)
        )
        built = normalize(
            fields,
            schema=schema,
            parent_type=return_type,
            policy=policy,
            builder=builder,
        )
        child_selection_set = built.selection_set
        child_variables = built.variables

    root_allocator = VariableAllocator(
        reserved=[variable.name for variable in child_variables]
    )
    argument_nodes = tuple(
        ArgumentNode(
            name=NameNode(value=arg_name),
            value=VariableNode(
                name=NameNode(
                    value=root_allocator.allocate(
                        base=arg_name, type_=field_def.args[arg_name].type, value=value
                    )
                )
            ),
        )
        for arg_name, value in resolved_args.items()
    )

    written_name = resolved_field_name if operation_name is None else operation_name
    check_graphql_name(written_name, "operation name")

    root_field = FieldNode(
        alias=None,
        name=NameNode(value=resolved_field_name),
        arguments=argument_nodes,
        directives=(),
        selection_set=child_selection_set,
    )
    all_variables = (*root_allocator.variables, *child_variables)
    document = DocumentNode(
        definitions=(
            OperationDefinitionNode(
                operation=_OPERATION_TYPE[kind],
                name=NameNode(value=written_name),
                variable_definitions=_variable_definitions(all_variables),
                directives=(),
                selection_set=SelectionSetNode(selections=(root_field,)),
            ),
        )
    )

    if validate:
        validate_document(schema, document)

    variable_values = coerce_variables(
        ((variable.name, variable.type_, variable.value) for variable in all_variables),
        kind=kind,
        operation_name=resolved_field_name,
    )
    return AssembledOperation(
        document=document, variables=variable_values, field_name=resolved_field_name
    )
