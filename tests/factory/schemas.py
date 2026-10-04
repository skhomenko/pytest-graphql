"""One small input-heavy schema for the factory tests and the golden vectors.

The hostile schema in ``tests/schema`` has two flat input types. The factory
rules need the opposite: nested objects, a recursive type, enums, every list
shape, a custom scalar in required and in nullable position, and a snake_case
collision, each on a type small enough to read.
"""

from __future__ import annotations

from graphql import GraphQLSchema, build_schema

from pytest_graphql._core.factory import FakeNamespace, ScalarRegistry, UniqueSource

SDL = """
scalar Money
scalar Stamp
scalar JSON

enum Role { ADMIN EDITOR VIEWER GUEST }

input AddressInput {
  street: String!
  city: String
  zip: String
  country: Role
}

input ProfileInput {
  bio: String
  address: AddressInput!
  previous: AddressInput
  tags: [String!]
  scores: [Int!]!
  matrix: [[Int!]!]
  maybes: [String]
}

input CreateUserInput {
  name: String!
  nickname: String
  age: Int!
  height: Float
  active: Boolean!
  ref: ID!
  role: Role!
  secondaryRole: Role
  profile: ProfileInput
}

input TreeInput {
  label: String!
  parent: TreeInput
  children: [TreeInput!]
  tags: [String!]
}

input PricedInput {
  price: Money!
  name: String!
}

input OptionalPriceInput {
  price: Money
  name: String!
}

input StampedInput {
  at: Stamp!
  items: [Stamp!]
}

input CollisionInput {
  userId: ID
  user_id: ID
}

input TripleCollisionInput {
  userId: ID
  userID: ID
  user_Id: ID
}

input EmptyRequiredInput {
  note: String
}

input LineInput {
  price: Money!
  note: String
  tags: [Stamp!]
}

input OrderInput {
  lines: [LineInput!]!
  total: Money
  meta: JSON
  grid: [[Money!]!]
  role: Role
}

type User {
  id: ID!
  convert(amount: Money, order: OrderInput): String
}

type Query {
  user: User
  price: Money
  total(
    amount: Money!
    many: [Money!]
    order: OrderInput
    plain: String
    stamp: Stamp
  ): String
}

type Mutation {
  createUser(input: CreateUserInput!): User!
  createTree(input: TreeInput!): User!
  pricedUser(input: PricedInput!): User
  stampedUser(input: StampedInput!): User
  placeOrder(input: OrderInput!): String
}
"""

SCHEMA: GraphQLSchema = build_schema(SDL)

RUN_ID = "run-test"
NODE_ID = "tests/test_x.py::test_one"


def namespace(
    *,
    seed: int = 0,
    node_id: str = NODE_ID,
    scalars: ScalarRegistry | None = None,
    worker_id: str = "main",
    run_id: str = RUN_ID,
    schema: GraphQLSchema | None = None,
) -> FakeNamespace:
    return FakeNamespace(
        schema if schema is not None else SCHEMA,
        scalars,
        global_seed=seed,
        node_id=node_id,
        unique_source=UniqueSource(run_id, worker_id),
    )
