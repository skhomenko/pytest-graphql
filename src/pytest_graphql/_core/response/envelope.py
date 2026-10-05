"""``GraphQLResponse`` and ``build_response`` (C5, C9, B18).

``build_response`` is a pure function from what a transport returned to the
object a test reads. It does not decide whether a response is an error: the
three data states and the error list are recorded here, and the raise table
belongs to the client (M5c).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar

from graphql import DocumentNode, GraphQLSchema

from pytest_graphql._core.diagnostics import (
    DiagnosticSnapshot,
    RequestInfo,
    require_safe_rendering,
    text_tools,
)
from pytest_graphql._core.errors import GraphQLTestError
from pytest_graphql._core.response.materialize import (
    Materializer,
    ScalarParsers,
    check_json_depth,
)
from pytest_graphql._core.response.node import MISSING, Node, at_path
from pytest_graphql._core.response.types import GraphQLErrorInfo, HttpInfo
from pytest_graphql._core.transport.base import RawResponse

T = TypeVar("T")

DataState = Literal["absent", "null", "present"]


@dataclass(frozen=True, eq=False)
class GraphQLResponse(Generic[T]):
    """Everything the server returned for one operation, and how it was sent.

    `query()` and `mutation()` return the value of the one field you asked for,
    so you usually do not see this object. You get it from
    `GraphQLClient.execute()`, from a call made with `raw=True`, and from the
    `response` of an error. Use it to read errors next to data, the HTTP status,
    the timing and the untouched JSON.

    The response is immutable. Its `repr()` shows the status, the state of the
    data, the number of errors and the redacted request. It never shows a data
    value or an error message, so a failure report does not leak them.

    The type parameter is the type of `data`. It is `Any` by default. Annotate
    a variable as `GraphQLResponse[MyType]` if you want your own type for it.

    Examples:
        ```python {.exec}
        response = gql.execute('query { user(id: "u1") { name } }')

        assert response.has_data
        assert response.data.user.name == "Ada Lovelace"
        assert response.errors == ()
        assert response.http.status_code == 200
        ```
    """

    data: T
    """The result of the operation.

    When `data_state` is `"present"`, this is a `Node` that holds the top-level
    fields you selected. Otherwise it is `None`.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.data.user.name == "Ada Lovelace"
        ```
    """
    data_state: DataState
    """Whether the server sent data: `"present"`, `"null"` or `"absent"`.

    - `"present"`: `data` is an object.
    - `"null"`: the server sent `data: null`, which happens when an error stopped
      the whole operation.
    - `"absent"`: the response has no `data` entry at all. A server response
      with no `data` entry is a rejected request and raises an error before a
      response exists, so you will see `"present"` and `"null"`.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.data_state == "present"

        failed = gql.mutation(
            "updateUser", id="missing", fields=["id"], raise_on_error=False, raw=True
        )
        assert failed.data_state == "null"
        ```
    """
    errors: tuple[GraphQLErrorInfo, ...]
    """The entries of the response's `errors` list, in the server's order.

    Each entry has a `message` (text from the server), a `path` and `locations`
    (or `None`), an `extensions` mapping, and a `code`, which is
    `extensions["code"]` when that is a string and `None` otherwise. `repr()` of
    an entry shows the path and locations and not the message. The tuple is
    empty when the server returned no errors.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.errors == ()

        failed = gql.mutation(
            "updateUser", id="missing", fields=["id"], raise_on_error=False, raw=True
        )
        assert failed.errors[0].path == ("updateUser",)
        ```
    """
    extensions: Mapping[str, Any]
    """The response's top-level `extensions`, or an empty mapping.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.extensions == {}
        ```
    """
    http: HttpInfo
    """The HTTP exchange: `status_code`, `media_type`, `url` and `headers`.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.http.status_code == 200
        assert response.http.url.startswith("http")
        ```
    """
    request: DiagnosticSnapshot
    """The request that was sent, in its redacted form, for reports.

    Redaction follows the rules in `DiagnosticSnapshot`: it covers the redacted
    headers and variables, and known secrets in free text above a minimum length.
    A header or variable that the settings do not name is not redacted by its name.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.request.kind == "query"
        assert "user" in response.request.document
        ```
    """
    raw: Mapping[str, Any]
    """The response envelope, before any parsing.

    It holds `data`, and holds `errors` and `extensions` only when the server
    sent them. The values are the JSON the transport parsed. Custom scalars
    are not decoded here.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.raw == {"data": {"user": {"name": "Ada Lovelace"}}}
        ```
    """
    duration_ms: float
    """How long the transport call took, in milliseconds.

    Examples:
        ```python {.exec}
        response = gql.execute('{ user(id: "u1") { name } }')
        assert response.duration_ms >= 0
        ```
    """

    @property
    def has_data(self) -> bool:
        """Whether `data` holds an object. `False` when it is `None`.

        Examples:
            ```python {.exec}
            response = gql.execute('{ user(id: "u1") { name } }')
            assert response.has_data
            assert response.data_state == "present"
            ```
        """
        return self.data_state == "present"

    def unwrap(self) -> Any:
        """Return the value of the one top-level field the operation selected.

        This is what `query()` and `mutation()` do for you. It is for a
        document that you sent with `execute()`. The count is of the fields the
        document selects at the top level, whether or not the server returned
        them.

        Returns:
            The value of that field.

        Raises:
            GraphQLTestError: When `data` is not present, or when the document
                selects more or fewer than one top-level field. The message
                names the fields.

        Examples:
            ```python {.exec}
            response = gql.execute('{ user(id: "u1") { name } }')
            assert response.unwrap().name == "Ada Lovelace"
            ```
        """
        if not self.has_data:
            raise GraphQLTestError(
                f"cannot unwrap a response whose data is {self.data_state}."
            )
        node: Node = self.data  # type: ignore[assignment]
        fields = node._plan_keys()
        if len(fields) != 1:
            raise GraphQLTestError(
                f"unwrap() needs exactly one top-level field, found {len(fields)}: "
                f"{', '.join(fields) or 'none'}."
            )
        return node[fields[0]]

    def at(self, path: str, default: Any = MISSING) -> Any:
        """Read a value from `data` by a dotted path.

        A segment is a field name or a list index. Write an index as a number
        (`orders.0.total`) or in brackets (`orders[0].total`). Each name works
        in its exact schema spelling and in snake_case.

        Args:
            path: The path to read.
            default: What to return when a segment does not exist. Without it,
                a missing segment raises.

        Returns:
            The value at the path.

        Raises:
            GraphQLFieldError: When a segment does not exist and no `default`
                is given.
            GraphQLTestError: When `data` is not present and no `default` is
                given.

        Examples:
            ```python {.exec}
            response = gql.execute('{ user(id: "u1") { name team { name } } }')
            assert response.at("user.team.name") == "Core"
            assert response.at("user.nickname", default=None) is None
            ```
        """
        if not self.has_data:
            if default is MISSING:
                raise GraphQLTestError(
                    f"cannot read {path!r} from a response whose data is "
                    f"{self.data_state}."
                )
            return default
        return at_path(self.data, path, default)

    def __repr__(self) -> str:
        # Status, state and counts, plus the redacted request. Never a data
        # value or an error message. The fixed text around the snapshot can
        # complete a secret, so the whole is checked.
        return require_safe_rendering(
            self.request,
            f"GraphQLResponse(status={self.http.status_code}, "
            f"data={self.data_state}, errors={len(self.errors)}, "
            f"request={self.request!r})",
            "repr()",
        )

    __str__ = __repr__


def build_response(
    raw: RawResponse,
    *,
    request: RequestInfo,
    schema: GraphQLSchema,
    document: DocumentNode,
    parsers: ScalarParsers | None = None,
    operation_name: str | None = None,
    duration_ms: float = 0.0,
) -> GraphQLResponse[Any]:
    """Materialize ``raw`` against the operation that produced it.

    Raises ``ResponseShapeError`` when the data contradicts a declared type.
    """
    envelope: dict[str, Any] = {"data": raw.data}
    if raw.errors:
        envelope["errors"] = list(raw.errors)
    if raw.extensions:
        envelope["extensions"] = raw.extensions

    check_json_depth(raw.data, "data")
    for index, item in enumerate(raw.errors):
        check_json_depth(item, f"errors.{index}")
    check_json_depth(raw.extensions, "extensions")

    materializer = Materializer(
        schema, document, parsers, operation_name=operation_name
    )
    data: Any = None
    state: DataState = "null"
    if raw.data is not None:
        materializer.check_data(raw.data)
        data = materializer.wrap_data(raw.data)
        state = "present"

    snapshot = request.redacted()
    sanitize, excerpt = text_tools(request)
    return GraphQLResponse(
        data=data,
        data_state=state,
        errors=tuple(
            GraphQLErrorInfo.from_mapping(
                item,
                scrub=sanitize,
                # A summary only for the errors a report may show: the rest
                # are counted, never rendered (max_recorded_errors).
                excerpt=excerpt if index < request.max_recorded_errors else None,
            )
            for index, item in enumerate(raw.errors)
        ),
        extensions=MappingProxyType(dict(raw.extensions or {})),
        http=HttpInfo(
            status_code=raw.status_code,
            media_type=raw.media_type,
            url=snapshot.url,
            headers=raw.headers,
        ),
        request=snapshot,
        raw=envelope,
        duration_ms=duration_ms,
    )
