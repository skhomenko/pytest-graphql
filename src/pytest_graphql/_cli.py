"""The ``pytest-graphql`` command (DESIGN 13.7).

``operations`` lists what a schema can be asked, and ``describe <name>`` prints
what a test needs to call one operation. Both read a schema from an SDL file
(``--schema``) or from the endpoint in ``PYTEST_GQL_URL``.

A credential never travels on the command line, because an argument is kept in
shell history and is visible to other processes. So the command has no URL flag
and no header flag. The endpoint comes only from ``PYTEST_GQL_URL`` and the
headers only from ``PYTEST_GQL_HEADERS``, in the format of the ini option
``gql_headers``. No other setting is read: the rest is ``ClientConfig``'s default.
No message quotes the URL or a header value, and argparse's own errors are
reduced to text that holds no argument.

This module imports no pytest. It is outside ``plugin/``, so the purity check
covers it.
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import sys
import warnings
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLSchema,
    is_required_argument,
    print_ast,
)

from pytest_graphql._core.client import ClientConfig, build_client
from pytest_graphql._core.errors import SchemaError, SelectionError, caller_message
from pytest_graphql._core.graphql_compat import default_literal
from pytest_graphql._core.headers import parse_header_lines
from pytest_graphql._core.naming import NameMap, to_snake
from pytest_graphql._core.operation import assemble_operation
from pytest_graphql._core.schema.info import OperationKind, operation_names, root_type
from pytest_graphql._core.schema.source import SDLFileSource
from pytest_graphql._core.selection.builder import SelectionBuilder
from pytest_graphql._core.validation import (
    names_a_reserved_option,
)

#: The value of ``"format"`` in ``describe --json`` output.
FORMAT = "pytest-graphql/describe/1"
#: The value of ``"format"`` in ``operations --json`` output.
OPERATIONS_FORMAT = "pytest-graphql/operations/1"

EXIT_OK = 0
EXIT_UNKNOWN_NAME = 1
EXIT_USAGE = 2
EXIT_SCHEMA = 3

URL_VARIABLE = "PYTEST_GQL_URL"
HEADERS_VARIABLE = "PYTEST_GQL_HEADERS"

KINDS: tuple[OperationKind, ...] = ("query", "mutation", "subscription")

#: The only argparse messages that are printed as they are. Every other one can
#: hold the text of an argument, which may be a URL or a header.
_SAFE_USAGE_MESSAGE = re.compile(
    r"the following arguments are required: [a-z, ]+"
    r"|unrecognized option(: [-a-zA-Z, ]+)?"
    r"|argument [-a-z]+: expected one argument"
    r"|argument [-a-z]+: must be query, mutation or subscription"
)


_FLAG_NAME = re.compile(r"--[A-Za-z][A-Za-z-]*")


class _UsageError(Exception):
    """A refusal that ends in exit status 2. The message holds no argument."""


class _SchemaLoadError(Exception):
    """The schema could not be loaded. The message holds no URL and no header."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        shown = (
            message
            if _SAFE_USAGE_MESSAGE.fullmatch(message)
            else "the command line is not valid"
        )
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {shown}\n")

    def parse_args(  # type: ignore[override]
        self, args: Sequence[str] | None = None, namespace: None = None
    ) -> argparse.Namespace:
        parsed, extra = self.parse_known_args(args, namespace)
        if extra:
            # An unknown option is named by its flag when it is shaped like one.
            # A value after it is never echoed, because it could be a URL or a
            # header.
            flags = sorted(
                {
                    flag
                    for flag in (item.partition("=")[0] for item in extra)
                    if _FLAG_NAME.fullmatch(flag)
                }
            )
            named = f": {', '.join(flags)}" if flags else ""
            self.error(f"unrecognized option{named}")
        return parsed


def _kind_argument(text: str) -> OperationKind:
    for kind in KINDS:
        if text == kind:
            return kind
    raise argparse.ArgumentTypeError("must be query, mutation or subscription")


def _build_parser() -> argparse.ArgumentParser:
    shared = _Parser(add_help=False)
    shared.add_argument(
        "--schema",
        metavar="PATH",
        help=f"an SDL file; without it the endpoint in {URL_VARIABLE} is used",
    )
    shared.add_argument(
        "--json",
        action="store_true",
        help=(
            f'print one JSON object with "format": "{OPERATIONS_FORMAT}" or "{FORMAT}"'
        ),
    )
    parser = _Parser(
        prog="pytest-graphql",
        description="Explore a GraphQL schema the way pytest-graphql calls it.",
        epilog=(
            f"The endpoint is read from {URL_VARIABLE} and the headers from "
            f"{HEADERS_VARIABLE}, never from an argument."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "operations",
        parents=[shared],
        help="list every query, mutation and subscription",
    )
    describe = commands.add_parser(
        "describe",
        parents=[shared],
        help="print what a test needs to call one operation",
    )
    describe.add_argument("name", help="the schema name or the snake_case name")
    describe.add_argument(
        "--kind",
        type=_kind_argument,
        metavar="KIND",
        help="query, mutation or subscription; needed when the name is on two roots",
    )
    return parser


# -- the schema ----------------------------------------------------------------


def _nonblank(name: str, environ: Mapping[str, str]) -> str | None:
    """An environment value, or ``None`` when it is unset or only whitespace."""
    value = environ.get(name, "")
    return value if value.strip() else None


def _headers(environ: Mapping[str, str]) -> dict[str, str]:
    text = _nonblank(HEADERS_VARIABLE, environ)
    if text is None:
        return {}
    try:
        return parse_header_lines(text)
    except ValueError as invalid:
        raise _UsageError(
            caller_message(
                f"invalid value for {HEADERS_VARIABLE}.",
                context=[f"Expected {invalid}.", "The value is not shown."],
                fix=f"change {HEADERS_VARIABLE} to lines of the form Name: value",
            )
        ) from None


def _load_schema(schema_path: str | None, environ: Mapping[str, str]) -> GraphQLSchema:
    # Both variables are checked whether or not --schema hides them, as the
    # plugin checks every source (DESIGN 2).
    headers = _headers(environ)
    url = _nonblank(URL_VARIABLE, environ)
    if schema_path is not None:
        try:
            return SDLFileSource(schema_path).load()
        except SchemaError as error:
            raise _SchemaLoadError(str(error)) from None
    if url is None:
        raise _UsageError(
            caller_message(
                "no schema source.",
                fix=f"pass --schema PATH, or set {URL_VARIABLE} to the endpoint",
            )
        )
    try:
        with build_client(url=url.strip(), headers=headers) as client:
            return client.schema
    except Exception as failure:
        # Only the class is named. The text of a transport error can hold the
        # URL, and the text of a server error can hold a header.
        raise _SchemaLoadError(
            caller_message(
                f"could not load the schema from the endpoint in {URL_VARIABLE}.",
                context=[f"Cause: {type(failure).__name__}."],
                fix=(
                    "check that the endpoint is reachable and allows introspection, "
                    "or pass --schema PATH"
                ),
            )
        ) from None


# -- what is printed -----------------------------------------------------------


def _argument_record(name: str, argument: GraphQLArgument) -> dict[str, Any]:
    return {
        "name": to_snake(name),
        "schema_name": name,
        "type": str(argument.type),
        "required": is_required_argument(argument),
        "default": default_literal(argument),
    }


def _operation_record(
    kind: OperationKind, schema_name: str, field: GraphQLField
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": to_snake(schema_name),
        "schema_name": schema_name,
        "arguments": [_argument_record(name, arg) for name, arg in field.args.items()],
    }


def _all_operations(schema: GraphQLSchema) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for kind in KINDS:
        type_ = root_type(schema, kind)
        if type_ is not None:
            found.extend(
                _operation_record(kind, name, field)
                for name, field in type_.fields.items()
            )
    return found


def _arguments_line(arguments: Sequence[Mapping[str, Any]]) -> str:
    if not arguments:
        return "-"
    return ", ".join(f"{a['schema_name']}: {a['type']}" for a in arguments)


def _render_operations(records: Sequence[Mapping[str, Any]]) -> str:
    if not records:
        return "This schema has no operations.\n"
    rows = [
        (r["kind"], r["name"], r["schema_name"], _arguments_line(r["arguments"]))
        for r in records
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    return "".join(
        f"{kind:<{widths[0]}}  {name:<{widths[1]}}  "
        f"{schema_name:<{widths[2]}}  {args}\n"
        for kind, name, schema_name, args in rows
    )


def _sample_value(type_: Any) -> str:
    """How a test writes the value of one required argument.

    An input object is made by ``gql.fake``. Any other value is ``...``, because
    the caller chooses it and the design gives no generated value for it.
    """
    if isinstance(type_, GraphQLNonNull):
        type_ = type_.of_type
    if isinstance(type_, GraphQLList):
        inner = _sample_value(type_.of_type)
        return f"[{inner}]"
    if isinstance(type_, GraphQLInputObjectType):
        return f"gql.fake.{type_.name}()"
    return "..."


def _resolves_to(names: NameMap, spelling: str, target: str) -> bool:
    """True when the client's lookup turns ``spelling`` into ``target``.

    A sample is only worth printing when the client resolves each name in it
    to the operation or argument it describes. The lookup is the client's own,
    ``NameMap``. Its warning for an exact name that shadows a snake form is
    not wanted here.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return names.get(spelling) == target


def _keyword_for(names: NameMap, schema_name: str) -> str | None:
    """How a call writes this argument as a keyword, or ``None`` if it cannot.

    The snake form is preferred. The exact schema name is the other spelling the
    client accepts. A spelling counts when it is valid Python as a keyword and
    resolves to this argument. An argument whose exact name or snake form is an
    option of the call has none, because the client takes that keyword as the
    option whichever spelling is written (B23).
    """
    if names_a_reserved_option(schema_name):
        return None
    for spelling in (to_snake(schema_name), schema_name):
        if (
            spelling.isidentifier()
            and not keyword.iskeyword(spelling)
            and _resolves_to(names, spelling, schema_name)
        ):
            return spelling
    return None


def _sample_call(
    schema: GraphQLSchema, kind: OperationKind, schema_name: str, field: GraphQLField
) -> str:
    """One call as a test writes it, using only spellings the client resolves.

    The operation is named by its snake form when the root's lookup resolves
    that form to this operation, else by its exact name. The arguments are
    keywords when every argument the call must supply has a spelling that is
    valid Python and resolves to it. Otherwise all of them go in the
    ``variables=`` escape hatch under their exact names, which always resolve.
    The call supplies every required argument and nothing else.
    """
    root_names = NameMap.build(operation_names(schema, kind))
    snake = to_snake(schema_name)
    written = snake if _resolves_to(root_names, snake, schema_name) else schema_name
    required = {
        name: arg for name, arg in field.args.items() if is_required_argument(arg)
    }
    if not required:
        return f'gql.{kind}("{written}")'
    argument_names = NameMap.build(list(field.args))
    keywords = {name: _keyword_for(argument_names, name) for name in required}
    if all(keywords.values()):
        pairs = ", ".join(
            f"{keywords[name]}={_sample_value(arg.type)}"
            for name, arg in required.items()
        )
        return f'gql.{kind}("{written}", {pairs})'
    pairs = ", ".join(
        f'"{name}": {_sample_value(arg.type)}' for name, arg in required.items()
    )
    return f'gql.{kind}("{written}", variables={{{pairs}}})'


def _document(
    schema: GraphQLSchema, kind: OperationKind, schema_name: str
) -> tuple[str | None, str | None]:
    """The document auto-selection builds with the default policy, or why none.

    The call supplies every required argument and nothing else, so the document
    declares those variables. It goes through ``assemble_operation``, the one
    path the client builds a document with.
    """
    try:
        assembled = assemble_operation(
            schema=schema,
            kind=kind,
            name=schema_name,
            kwargs={},
            policy=ClientConfig().selection_policy(),
            builder=SelectionBuilder(schema),
            declare_only=True,
        )
    except SelectionError as error:
        # The message is built by ``caller_message`` (DESIGN 13.5): the problem,
        # any suggestion and the fix, with names and paths and never a value.
        return None, str(error)
    return print_ast(assembled.document), None


def _describe_record(
    schema: GraphQLSchema, kind: OperationKind, schema_name: str
) -> dict[str, Any]:
    root = root_type(schema, kind)
    assert root is not None  # the name was found on this root
    field = root.fields[schema_name]
    document, note = _document(schema, kind, schema_name)
    return {
        "format": FORMAT,
        **_operation_record(kind, schema_name, field),
        "returns": str(field.type),
        "document": document,
        "document_error": note,
        "sample": (
            None
            if kind == "subscription"
            else _sample_call(schema, kind, schema_name, field)
        ),
    }


def _render_describe(record: Mapping[str, Any]) -> str:
    lines = [
        f"{record['kind']} {record['name']}",
        f"  schema name: {record['schema_name']}",
        "Arguments:",
    ]
    arguments = record["arguments"]
    if not arguments:
        lines.append("  (none)")
    for a in arguments:
        needed = "required" if a["required"] else "optional"
        default = f", default {a['default']}" if a["default"] is not None else ""
        lines.append(
            f"  {a['name']} (schema name {a['schema_name']}): {a['type']}, "
            f"{needed}{default}"
        )
    lines.append(f"Returns: {record['returns']}")
    lines.append("Document:")
    if record["document"] is None:
        note = str(record["document_error"]).splitlines()
        lines.append(f"  not available: {note[0]}")
        lines.extend(note[1:])
    else:
        lines.extend(f"  {line}" for line in record["document"].splitlines())
    lines.append("Sample call:")
    if record["sample"] is None:
        lines.append(
            "  not available: a subscription is not called through "
            "gql.query() or gql.mutation()"
        )
    else:
        lines.append(f"  {record['sample']}")
        if "..." in record["sample"]:
            lines.append("  (... stands for a value you replace.)")
    return "\n".join(lines) + "\n"


# -- finding the operation -----------------------------------------------------


def _find(
    schema: GraphQLSchema, name: str, kind: OperationKind | None
) -> tuple[OperationKind, str]:
    """The root and schema name ``name`` is on.

    Raises ``_UsageError`` for a name on more than one root, and ``LookupError``
    with the message of DESIGN 13.5 for a name on none.
    """
    kinds = (kind,) if kind is not None else KINDS
    hits = [
        (found_kind, resolved)
        for found_kind in kinds
        if (resolved := NameMap.build(operation_names(schema, found_kind)).get(name))
        is not None
    ]
    if len(hits) > 1:
        roots = ", ".join(found for found, _ in hits)
        raise _UsageError(
            caller_message(
                f"{name!r} is on more than one root: {roots}.",
                fix="pass --kind query, --kind mutation or --kind subscription",
            )
        )
    if hits:
        return hits[0]
    names = [n for each in kinds for n in operation_names(schema, each)]
    available = list(dict.fromkeys(s for n in names for s in (n, to_snake(n))))
    what = f"{kind} " if kind is not None else "operation "
    plural = f"{kind}s" if kind is not None else "operations"
    raise LookupError(
        caller_message(
            f"no {what}named {name!r}.",
            context=[f"This schema has {len(names)} {plural}."],
            wrong=name,
            known=available,
            fix="pass a name that pytest-graphql operations lists",
        )
    )


# -- entry point ---------------------------------------------------------------


def _run(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[int, str]:
    schema = _load_schema(args.schema, environ)
    if args.command == "operations":
        records = _all_operations(schema)
        if args.json:
            return EXIT_OK, json.dumps(
                {"format": OPERATIONS_FORMAT, "operations": records}
            ) + "\n"
        return EXIT_OK, _render_operations(records)
    kind, schema_name = _find(schema, args.name, args.kind)
    record = _describe_record(schema, kind, schema_name)
    if args.json:
        return EXIT_OK, json.dumps(record) + "\n"
    return EXIT_OK, _render_describe(record)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``pytest-graphql`` command and return its exit status.

    The status is 0 on success, 1 for an unknown name, 2 for a usage error and
    3 when the schema cannot be loaded.
    """
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_OK
    try:
        status, text = _run(args, os.environ)
    except _UsageError as error:
        sys.stderr.write(f"pytest-graphql: {error}\n")
        return EXIT_USAGE
    except LookupError as error:
        sys.stderr.write(f"pytest-graphql: {error}\n")
        return EXIT_UNKNOWN_NAME
    except _SchemaLoadError as error:
        sys.stderr.write(f"pytest-graphql: {error}\n")
        return EXIT_SCHEMA
    sys.stdout.write(text)
    return status
