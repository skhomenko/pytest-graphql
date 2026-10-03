"""A small schema and response builder for the M6 matching tests.

The hostile schema in ``tests/schema`` is built to stress selection and
materialization. Matching needs the opposite: a schema whose shape is easy to
read, with an interface, a union, a snake-case collision pair and a nested
list of objects, so each matching rule has an obvious fixture.
"""

from __future__ import annotations

from typing import Any

from graphql import GraphQLSchema, build_schema, parse

from pytest_graphql._core.diagnostics import RequestInfo
from pytest_graphql._core.response import GraphQLResponse, build_response
from pytest_graphql._core.transport.base import RawResponse

SDL = """
interface Entity { id: ID! }

type Settings { theme: String locale: String }

type Order implements Entity {
  id: ID!
  total: Int!
  status: String
  note: String
}

type User implements Entity {
  id: ID!
  firstName: String!
  lastName: String!
  email: String
  status: String
  userId: ID
  user_id: ID
  password: String
  settings: Settings
  orders: [Order!]!
  tags: [String!]!
  matrix: [[Int!]!]!
}

type Team { id: ID! name: String! }

union Hit = User | Team

input UserInput { name: String }

enum Role { ADMIN GUEST }

type Query {
  user: User
  users: [User!]!
  hits: [Hit!]!
  orders: [Order!]!
}
"""

SCHEMA: GraphQLSchema = build_schema(SDL)
SECRET = "hunter2-hunter2"

USER_FIELDS = (
    "__typename id firstName lastName email status password "
    "settings { theme locale } orders { __typename id total status note } "
    "tags matrix"
)
ORDER_FIELDS = "__typename id total status note"


def request_for(query: str) -> RequestInfo:
    return RequestInfo(
        operation=None,
        kind="query",
        document=query,
        variables={},
        headers={"Authorization": f"Bearer {SECRET}"},
        url="http://example.test/graphql",
    )


def respond(query: str, data: Any) -> GraphQLResponse[Any]:
    raw = RawResponse(
        status_code=200,
        media_type="application/json",
        data=data,
        errors=(),
        extensions=None,
        headers={"Content-Type": "application/json"},
    )
    return build_response(
        raw, request=request_for(query), schema=SCHEMA, document=parse(query)
    )


def order(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "__typename": "Order",
        "id": "o1",
        "total": 100,
        "status": "PAID",
        "note": None,
    }
    base.update(overrides)
    return base


def user(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "__typename": "User",
        "id": "u1",
        "firstName": "John",
        "lastName": "Doe",
        "email": "john@example.test",
        "status": "ACTIVE",
        "password": None,
        "settings": {"theme": "dark", "locale": "en"},
        "orders": [order()],
        "tags": ["a", "b"],
        "matrix": [[1], [2, 3]],
    }
    base.update(overrides)
    return base


def user_node(**overrides: Any) -> Any:
    query = "{ user { " + USER_FIELDS + " } }"
    return respond(query, {"user": user(**overrides)}).data.user


def users_nodes(*rows: dict[str, Any]) -> Any:
    query = "{ users { " + USER_FIELDS + " } }"
    return respond(query, {"users": list(rows)}).data.users


def orders_nodes(*rows: dict[str, Any]) -> Any:
    query = "{ orders { " + ORDER_FIELDS + " } }"
    return respond(query, {"orders": list(rows)}).data.orders
