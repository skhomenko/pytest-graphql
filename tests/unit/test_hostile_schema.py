"""Exercise the hostile test schema itself (SPEC 11.1).

Two things are checked. First, that every operation the schema exposes
actually executes against the fixture resolvers, including the traversal
shapes (self-reference, cycle, diamond reuse, interface, union, connection)
and the naming/typing hazards (reserved words, snake_case collision, an enum
of Python keywords, a 300-field type). Second, that introspecting the schema
and rebuilding it from that introspection reproduces the original SDL byte
for byte. That round trip is the regression test for the introspection
truncation bug the deeply wrapped type `[[String!]!]!` is designed to catch
(PLAN.md M3).
"""

from __future__ import annotations

from graphql import (
    GraphQLSchema,
    build_client_schema,
    get_introspection_query,
    graphql_sync,
    print_schema,
)

from tests.schema.fake_transport import FakeTransport
from tests.schema.resolvers import build_schema

WIDE_FIELD_SELECTION = " ".join(f"field{i}" for i in range(300))


def _schema() -> GraphQLSchema:
    return build_schema()


def test_every_field_on_the_300_field_type_is_reachable() -> None:
    schema = _schema()
    assert len(schema.type_map["WideType"].fields) == 300


def test_query_operations_execute() -> None:
    transport = FakeTransport(_schema())
    query = """
    query Everything {
      byId: user(id: "u1") {
        id
        name
        userId
        user_id
        oldName
        legacyManager { id }
        manager { id }
        team { id name captain { id } }
        posts(first: 1) { id title }
        friends { id }
        friendsCustomLimit: friends(limit: 1) { id }
        looseFriends { id }
        strictFriends { id }
        postsConnection(first: 5) {
          edges { cursor node { id title author { id } editor { id } } }
          pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
        }
        joinedAt
        externalId
        preferences
        avatar
        balance
      }
      users { id }
      team(id: "t1") { id name members { id } }
      teams { id }
      post(id: "p1") { id title author { id } editor { id } }
      settings { theme locale }
      reservedWordFields { id from class type keys items values to_dict }
      search(term: "a") {
        __typename
        ... on User { id name }
        ... on Team { id name }
        ... on Attachment { filename sizeBytes }
      }
      wide {
        __WIDE_FIELDS__
      }
      matrix
      pingScalar
      nullableList
      nonNullListOfNullables
      pyKeyword
      pyKeywordEcho(value: class)
      diamond: post(id: "p1") { author { id } editor { id } }
    }
    """.replace("__WIDE_FIELDS__", WIDE_FIELD_SELECTION)

    response = transport.execute(query)

    assert response.get("errors") is None, response.get("errors")
    data = response["data"]
    assert data["byId"]["name"] == "Ada Lovelace"
    assert data["byId"]["userId"] == "u1"
    assert data["byId"]["user_id"] == "u1-snake"
    assert data["byId"]["manager"] is None
    assert data["search"]
    assert data["matrix"] == [["a", "b"], ["c"]]
    assert data["nullableList"] is None
    assert data["nonNullListOfNullables"] == ["x", None, "y"]
    assert data["pyKeyword"] == "class"
    assert data["pyKeywordEcho"] == "class"
    assert data["wide"]["field299"] == "value299"


def test_diamond_reuse_of_the_same_type_is_not_treated_as_a_cycle() -> None:
    """Post.author and Post.editor both resolve to User in one selection."""
    transport = FakeTransport(_schema())
    response = transport.execute(
        """
        query {
          post(id: "p1") {
            author { id name }
            editor { id name }
          }
        }
        """
    )
    assert response.get("errors") is None, response.get("errors")
    post = response["data"]["post"]
    assert post["author"]["id"] == "u1"
    assert post["editor"]["id"] == "u2"


def test_indirect_cycle_through_team_terminates() -> None:
    """User -> Team -> User is a real cycle and must still terminate here."""
    transport = FakeTransport(_schema())
    response = transport.execute(
        """
        query {
          user(id: "u1") {
            team {
              captain {
                team { id }
              }
            }
          }
        }
        """
    )
    assert response.get("errors") is None, response.get("errors")
    assert response["data"]["user"]["team"]["captain"]["team"]["id"] == "t1"


def test_node_interface_dispatches_to_all_three_implementations() -> None:
    transport = FakeTransport(_schema())
    response = transport.execute(
        """
        query {
          user: node(id: "u1") { __typename ... on User { name } }
          team: node(id: "t1") { __typename ... on Team { name } }
          post: node(id: "p1") { __typename ... on Post { title } }
        }
        """
    )
    assert response.get("errors") is None, response.get("errors")
    data = response["data"]
    assert data["user"]["__typename"] == "User"
    assert data["team"]["__typename"] == "Team"
    assert data["post"]["__typename"] == "Post"


def test_every_mutation_shape_executes() -> None:
    transport = FakeTransport(_schema())
    response = transport.execute(
        """
        mutation {
          flatArguments: updateUser(id: "u1", name: "Ada Byron") { id name }
          singleInputObject: createPost(input: { title: "New", authorId: "u1" }) {
            id
            title
          }
          mixedArguments: movePost(id: "p1", data: { teamId: "t2" }) { id }
        }
        """
    )
    assert response.get("errors") is None, response.get("errors")
    data = response["data"]
    assert data["flatArguments"]["name"] == "Ada Byron"
    assert data["singleInputObject"]["title"] == "New"
    assert data["mixedArguments"]["id"] == "p1"


def test_upload_scalar_serializes_a_validated_mapping_not_identity() -> None:
    """The registered Upload scalar must behave differently from Money, the
    one custom scalar the checklist asks to leave unregistered."""
    transport = FakeTransport(_schema())
    response = transport.execute(
        """
        query {
          user(id: "u2") { avatar }
        }
        """
    )
    assert response.get("errors") is None, response.get("errors")
    assert response["data"]["user"]["avatar"] == {
        "filename": "u2.png",
        "contentType": "image/png",
    }


def test_introspection_round_trip_reproduces_the_sdl_built_schema() -> None:
    """Catches the introspection truncation bug PLAN.md M3 names as the risk.

    Deeply wrapped types such as ``[[String!]!]!`` are exactly what a naive
    introspection reader can flatten or truncate, so the printed SDL of the
    rebuilt schema must match the original byte for byte, wrapper depth
    included.
    """
    original = _schema()

    introspection_result = graphql_sync(original, get_introspection_query())
    assert introspection_result.errors is None
    assert introspection_result.data is not None

    rebuilt = build_client_schema(introspection_result.data)

    assert print_schema(rebuilt) == print_schema(original)
    assert "[[String!]!]!" in print_schema(original)
