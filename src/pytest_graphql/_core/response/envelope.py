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

from pytest_graphql._core.diagnostics import DiagnosticSnapshot, RequestInfo
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
    """One executed operation's result (SPEC 3.4).

    ``data`` is ``None`` unless ``data_state`` is ``"present"``, in which case
    it is a ``Node`` over the operation's root type. ``raw`` holds the
    envelope as the transport parsed it and is never modified.
    """

    data: T
    data_state: DataState
    errors: tuple[GraphQLErrorInfo, ...]
    extensions: Mapping[str, Any]
    http: HttpInfo
    request: DiagnosticSnapshot
    raw: Mapping[str, Any]
    duration_ms: float

    @property
    def has_data(self) -> bool:
        """True only when ``data`` is present and not null (C5)."""
        return self.data_state == "present"

    def unwrap(self) -> Any:
        """The value of the single top-level field (B18)."""
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
        """Read a dotted path from ``data``."""
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
        # value or an error message.
        return (
            f"GraphQLResponse(status={self.http.status_code}, "
            f"data={self.data_state}, errors={len(self.errors)}, "
            f"request={self.request!r})"
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
    return GraphQLResponse(
        data=data,
        data_state=state,
        errors=tuple(GraphQLErrorInfo.from_mapping(item) for item in raw.errors),
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
