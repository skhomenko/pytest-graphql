"""``client.expect`` over a real response from the in-process transport."""

from __future__ import annotations

import pytest
from graphql import GraphQLSchema

from pytest_graphql._core.client import GraphQLClient
from pytest_graphql._core.errors import SelectionError
from pytest_graphql._core.matching import contains, matches
from tests.schema.fake_transport import FakeGraphQLTransport
from tests.schema.resolvers import build_schema


@pytest.fixture(scope="module")
def schema() -> GraphQLSchema:
    return build_schema()


@pytest.fixture
def client(schema: GraphQLSchema) -> GraphQLClient:
    return GraphQLClient(transport=FakeGraphQLTransport(schema), schema=schema)


def test_expect_matches_a_real_response(client: GraphQLClient) -> None:
    users = client.query("users")
    first = users[0]
    assert first == client.expect.User(name="Ada Lovelace")
    assert first != client.expect.User(name="Grace Hopper")
    assert users == contains(client.expect.User(id=matches(".")))


def test_where_and_one_work_on_a_real_response(client: GraphQLClient) -> None:
    users = client.query("users")
    only = users.where(name="Ada Lovelace").one()
    assert only == client.expect.User(id=users[0].id)
    assert len(users.where(name="Nobody")) == 0


def test_expect_rejects_a_typo_before_any_comparison(client: GraphQLClient) -> None:
    with pytest.raises(SelectionError, match="no field 'nme' on User"):
        client.expect.User(nme="Ada")


def test_expect_is_built_once_per_client_and_follows_its_schema(
    client: GraphQLClient,
) -> None:
    assert client.expect is client.expect
    assert client.anonymous().expect.User(name="x").type_name == "User"
